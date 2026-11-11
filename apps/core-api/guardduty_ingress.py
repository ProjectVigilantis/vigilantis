"""고객 계정 GuardDuty 탐지 → 위협 이벤트 · 인시던트 (#588 C).

테넌트 수집 주기(services/collector.py _collect_store_tenant)가 리전마다 tenant_scope 안에서 부른다.
읽기(services/guardduty.read_findings) → 정형화(security/threat_normalizer.normalize_guardduty_finding) →
초기 판정(security/risk_evaluator.evaluate_threat) → 저장. 모의 위협 접수(threat_ingress.py)와 같은 정형화 ·
판정 · Intake 를 쓰고, 다른 것은 셋이다.

  - 위협 이벤트는 전부 남기고 인시던트는 MEDIUM 이상만 만든다(#588 Q3). LOW 는 이벤트만 저장한다.
  - 자동 선차단 · 격리를 부르지 않는다(#588 Q2). threat_ingress 는 High 에 trigger_isolation 을 부르지만 여기는
    부르지 않고, 분석 스캔 · 타임아웃 감시가 다시 거는 발동은 발동 관문이 위협 유형으로 막는다
    (workflows.AUTO_ISOLATION_EXCLUDED_EVENT_TYPES).
  - 같은 탐지(같은 ID)가 갱신돼 다시 와도 한 번만 남는다 — 이미 있는 키는 Intake 에 넘기지 않고 건너뛴다.
    넘기면 LOW 로 남은 이벤트에 Intake 가 「인시던트 없는 이벤트」 복구 경로로 인시던트를 붙인다.

커서(guardduty_finding_cursors): 처음 읽는 리전은 INITIAL_LOOKBACK 만큼만 거슬러 읽는다 — 이미 GuardDuty 를 쓰던
고객의 90일치 탐지가 한꺼번에 인시던트가 되지 않게. 회차가 끝나면 읽은 탐지의 가장 늦은 갱신 시각으로 옮기고,
저장에 실패한 탐지가 있으면 그 갱신 시각에 멈춘다(다음 회차가 거기서 다시 읽고 이미 남은 것은 중복으로 접힌다).
형식이 어긋나 거절한 탐지는 커서를 막지 않는다 — 다시 읽어도 같은 이유로 거절된다.

탐지 1건의 실패는 그 건만 되감고 다음 건으로 간다. AWS 오류(권한 없음 · 엔드포인트 없음)는 그 리전의 읽기만
FAILED 로 남기고 자산 수집 결과를 바꾸지 않는다 — 템플릿을 아직 갱신하지 않은 고객 역할에는 ListFindings 가 없다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import IntegrityError

from db.repositories import guardduty as guardduty_repo
from db.repositories import incidents as incidents_repo
from incident_intake import create_incident_from_intake
from realtime import incident_event
from schemas.api.incidents import RiskLevel
from schemas.api.ws import WsEvent, WsEventType
from schemas.intake import SecOpsIncidentIntake
from security.risk_evaluator import evaluate_threat
from security.threat_normalizer import guardduty_finding_updated_at, normalize_guardduty_finding
from services import guardduty
from services.aws.errors import aws_error_code

logger = logging.getLogger("vigilantis.guardduty_ingress")

#: 처음 읽는 리전이 거슬러 읽는 기간.
INITIAL_LOOKBACK = timedelta(days=1)
_TITLE_MAX = 256  # incidents.title


def _title(finding_type: str) -> str:
    return f"GuardDuty 탐지 · {finding_type}"[:_TITLE_MAX]


def _updated_at(finding: dict) -> datetime | None:
    try:
        return guardduty_finding_updated_at(finding)
    except (ValueError, TypeError):
        return None


def ingest_region(
    session_factory,
    *,
    tenant_id: str,
    account_id: str,
    region: str,
    publish: Callable[[WsEvent], None] | None = None,
    now: datetime | None = None,
) -> dict:
    """한 테넌트 · 한 리전의 새 탐지를 저장한다. 호출부가 그 테넌트의 tenant_scope 를 세워 둔다.

    돌려주는 값은 회차 요약이다 — status 는 OK · NO_DETECTOR · FAILED."""
    now = now or datetime.now(timezone.utc)
    db = session_factory()
    try:
        since = guardduty_repo.get_cursor(db, tenant_id=tenant_id, region=region) or now - INITIAL_LOOKBACK
        db.rollback()  # 읽기 트랜잭션을 AWS 호출 동안 열어 두지 않는다
        try:
            findings = guardduty.read_findings(region, account_id=account_id, updated_since=since)
        except (ClientError, BotoCoreError) as exc:
            code = aws_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
            logger.warning("guardduty_read_failed", extra={
                "tenant_id": tenant_id, "region": region, "error": code,
            })
            return {"status": "FAILED", "error": code}
        if findings is None:
            return {"status": "NO_DETECTOR"}
        return _store(db, findings, tenant_id=tenant_id, account_id=account_id, region=region,
                      since=since, publish=publish)
    finally:
        db.close()


def _store(db, findings, *, tenant_id, account_id, region, since, publish) -> dict:
    summary = {"status": "OK", "read": len(findings), "events_created": 0, "incidents_created": 0,
               "duplicates": 0, "rejected": 0, "failed": 0}
    ordered = sorted(findings, key=lambda f: (_updated_at(f) or since, str(f.get("Id", ""))))
    first_failed: datetime | None = None
    latest = since
    for finding in ordered:
        updated_at = _updated_at(finding) or since
        try:
            event = normalize_guardduty_finding(finding, account_id=account_id, region=region)
            initial_risk = evaluate_threat(event)
        except ValueError:
            # 원문은 남기지 않는다(자원 · IP 를 담는다). 식별자만 남긴다
            summary["rejected"] += 1
            logger.warning("guardduty_finding_rejected", extra={
                "tenant_id": tenant_id, "region": region, "finding_id": finding.get("Id"),
            })
            latest = max(latest, updated_at)
            continue
        try:
            if incidents_repo.get_threat_event_by_dedup_key(db, event.deduplication_key) is not None:
                summary["duplicates"] += 1
            elif initial_risk.initial_risk_level is RiskLevel.LOW:
                try:
                    with db.begin_nested():
                        incidents_repo.insert_threat_event(db, event, tenant_id=tenant_id)
                    db.commit()
                    summary["events_created"] += 1
                except IntegrityError:
                    db.rollback()
                    summary["duplicates"] += 1
            else:
                outcome = create_incident_from_intake(db, SecOpsIncidentIntake(
                    title=_title(event.payload.finding_type), threat_event=event, initial_risk=initial_risk,
                ))
                if outcome.created:
                    summary["events_created"] += 1
                    summary["incidents_created"] += 1
                    _publish_created(publish, outcome)
                else:
                    summary["duplicates"] += 1
        except Exception:  # noqa: BLE001 — 한 건의 저장 실패가 같은 회차의 다른 탐지를 막지 않는다
            db.rollback()
            summary["failed"] += 1
            first_failed = updated_at if first_failed is None else min(first_failed, updated_at)
            logger.exception("guardduty_finding_store_failed", extra={
                "tenant_id": tenant_id, "region": region, "finding_id": finding.get("Id"),
            })
            continue
        latest = max(latest, updated_at)
    cursor = first_failed if first_failed is not None else latest
    guardduty_repo.set_cursor(db, tenant_id=tenant_id, region=region, updated_since=cursor)
    db.commit()
    return summary


def _publish_created(publish, outcome) -> None:
    if publish is None:
        return
    try:
        publish(incident_event(
            WsEventType.INCIDENT_CREATED,
            incident_id=outcome.incident_id,
            occurred_at=outcome.occurred_at,
            category=outcome.category,
        ))
    except Exception:  # noqa: BLE001 — 저장 성공 뒤의 알림 실패는 재접수 사유가 아니다
        logger.exception("guardduty_incident_publish_failed", extra={"incident_id": outcome.incident_id})
