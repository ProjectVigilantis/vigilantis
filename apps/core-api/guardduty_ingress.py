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

커서(guardduty_finding_cursors, 고객사 × 읽은 리전)
  - 목록 조회는 「갱신 시각 ≥ updated_since」를 오름차순으로 훑는다(순회). 처음 읽는 리전은 INITIAL_LOOKBACK
    만큼만 거슬러 읽는다 — 이미 GuardDuty 를 쓰던 고객의 90일치 탐지가 한꺼번에 인시던트가 되지 않게.
  - 한 회차는 READ_LIMIT 건까지 읽는다. 끊기면 남은 쪽 토큰을 커서에 두고, 다음 회차가 같은 조건으로 이어 읽는다.
    같은 시각에 갱신된 탐지가 한도보다 많아도 순회가 앞으로 간다. 토큰이 거절되면(만료 등) 순회를 처음부터 다시
    한다 — 다시 읽은 것은 중복으로 접힌다.
  - updated_since 는 순회가 끝나야 옮긴다. 옮기는 자리는 「빠짐없이 훑었다고 확인된 범위」의 끝이다 — 처리한
    탐지 가운데 갱신 시각이 순회 시작 시각 - SETTLE_MARGIN 보다 이른 것의 가장 늦은 값(watermark). 목록 조회와
    상세 조회 사이(또는 이어 읽는 회차 사이)에 갱신된 탐지는 상세의 갱신 시각이 순회 뒤로 가 있어 진척으로 치지
    않는다 — 치면 아직 읽지 않은 탐지를 건너뛴다. 순회가 시작된 뒤 갱신된 탐지는 다음 순회가 다시 읽는다.
    SETTLE_MARGIN 은 우리 시계와 GuardDuty 시계의 차이, 갱신이 목록에 보이기까지의 지연을 덮는다.
  - 저장에 실패한 탐지가 있으면 그 갱신 시각(hold) 너머로 옮기지 않는다 — 다음 순회가 거기서 다시 읽고 이미
    남은 것은 중복으로 접힌다. hold 는 상한일 뿐 진척을 만들지 않는다 — watermark 가 없으면 커서를 그대로 둔다.
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
from db.repositories.guardduty import PassProgress
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
#: 순회 시작 직전의 이 구간에 갱신된 탐지는 진척으로 치지 않는다(머리말 「커서」).
SETTLE_MARGIN = timedelta(minutes=5)
#: 이어 읽던 토큰이 거절될 때의 오류 코드 — 순회를 처음부터 다시 한다.
_STALE_TOKEN = "BadRequestException"
_TITLE_MAX = 256  # incidents.title


def _title(finding_type: str) -> str:
    return f"GuardDuty 탐지 · {finding_type}"[:_TITLE_MAX]


def _updated_at(finding: dict) -> datetime | None:
    try:
        return guardduty_finding_updated_at(finding)
    except (ValueError, TypeError):
        return None


def _latest(a: datetime | None, b: datetime) -> datetime:
    return b if a is None else max(a, b)


def _earliest(a: datetime | None, b: datetime) -> datetime:
    return b if a is None else min(a, b)


def _read(region, *, account_id, since, progress):
    """이어 읽을 토큰이 거절되면 순회를 새로 시작해 한 번 더 읽는다. (쪽, 이 회차가 이어 갈 순회 진척)."""
    if progress is not None:
        try:
            return guardduty.read_findings(
                region, account_id=account_id, updated_since=since, next_token=progress.next_token,
            ), progress
        except ClientError as exc:
            if aws_error_code(exc) != _STALE_TOKEN:
                raise
            logger.warning("guardduty_pass_restarted", extra={"region": region, "error": _STALE_TOKEN})
    return guardduty.read_findings(region, account_id=account_id, updated_since=since), None


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

    돌려주는 값은 회차 요약이다 — status 는 OK · NO_DETECTOR · FAILED. OK 의 more 는 순회가 한도에서 끊겨
    다음 회차가 이어 읽는다는 뜻이다."""
    now = now or datetime.now(timezone.utc)
    db = session_factory()
    try:
        cursor = guardduty_repo.get_cursor(db, tenant_id=tenant_id, region=region)
        since = cursor.updated_since if cursor is not None else now - INITIAL_LOOKBACK
        progress = None if cursor is None else cursor.progress
        db.rollback()  # 읽기 트랜잭션을 AWS 호출 동안 열어 두지 않는다
        try:
            page, progress = _read(region, account_id=account_id, since=since, progress=progress)
        except (ClientError, BotoCoreError) as exc:
            code = aws_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
            logger.warning("guardduty_read_failed", extra={
                "tenant_id": tenant_id, "region": region, "error": code,
            })
            return {"status": "FAILED", "error": code}
        if page is None:
            return {"status": "NO_DETECTOR"}
        return _store(db, page, tenant_id=tenant_id, account_id=account_id, region=region,
                      since=since, progress=progress, now=now, publish=publish)
    finally:
        db.close()


def _store(db, page, *, tenant_id, account_id, region, since, progress, now, publish) -> dict:
    findings = page.findings
    summary = {"status": "OK", "read": len(findings), "events_created": 0, "incidents_created": 0,
               "duplicates": 0, "rejected": 0, "failed": 0, "more": page.next_token is not None}
    started_at = now if progress is None else progress.started_at
    settled_before = started_at - SETTLE_MARGIN
    watermark = None if progress is None else progress.watermark
    hold = None if progress is None else progress.hold
    ordered = sorted(findings, key=lambda f: (_updated_at(f) or since, str(f.get("Id", ""))))
    for finding in ordered:
        updated_at = _updated_at(finding) or since
        if _store_one(db, finding, summary, tenant_id=tenant_id, account_id=account_id, region=region,
                      publish=publish):
            if updated_at < settled_before:
                watermark = _latest(watermark, updated_at)
        else:
            hold = _earliest(hold, updated_at)
    if page.next_token is not None:
        guardduty_repo.save_cursor(
            db, tenant_id=tenant_id, region=region, updated_since=since,
            progress=PassProgress(page.next_token, started_at, watermark, hold),
        )
    else:
        guardduty_repo.save_cursor(
            db, tenant_id=tenant_id, region=region, updated_since=_reached(since, watermark, hold),
        )
    db.commit()
    return summary


def _reached(since: datetime, watermark: datetime | None, hold: datetime | None) -> datetime:
    """끝난 순회가 커서를 옮길 자리. 진척은 watermark 만 만들고 hold 는 그 상한이다.

    watermark 가 없으면 빠짐없이 훑었다고 확인된 범위가 없으니 그대로 둔다 — hold 는 실패한 탐지의 상세
    갱신 시각이라 순회 뒤로 가 있을 수 있고, 거기까지 옮기면 그사이에 생긴 탐지를 건너뛴다."""
    if watermark is None:
        return since
    return watermark if hold is None else min(watermark, hold)


def _store_one(db, finding, summary, *, tenant_id, account_id, region, publish) -> bool:
    """탐지 1건을 저장한다. 다시 읽을 필요가 없으면 True(저장 · 중복 · 형식 거절), 저장에 실패하면 False."""
    try:
        event = normalize_guardduty_finding(finding, account_id=account_id, region=region)
        initial_risk = evaluate_threat(event)
    except ValueError:
        # 원문은 남기지 않는다(자원 · IP 를 담는다). 식별자만 남긴다
        summary["rejected"] += 1
        logger.warning("guardduty_finding_rejected", extra={
            "tenant_id": tenant_id, "region": region, "finding_id": finding.get("Id"),
        })
        return True
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
        logger.exception("guardduty_finding_store_failed", extra={
            "tenant_id": tenant_id, "region": region, "finding_id": finding.get("Id"),
        })
        return False
    return True


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
