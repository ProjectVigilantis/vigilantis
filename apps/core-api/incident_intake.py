# ==============================================================================
# [파일 설명]
# Detection 결과를 Incident 1건으로 만드는 업무 흐름 계층입니다. (Issue #254)
# Rule Engine(자산 판정)·Risk Evaluator(위협 판정)와 Incident 저장소 사이에 서며,
# 진입 타입은 packages/schemas/intake.py의 IncidentIntake 하나입니다.
#
# 계층 경계 — workflows.py와 같은 3층 분리를 따릅니다(Router → Workflow → Repository).
# 같은 계층이지만 파일을 나눈 것은, 그쪽이 **이미 만들어진 인시던트의 조치**를
# 접수·실행·확정하는 자리이고 이쪽은 **그 인시던트를 만드는** 자리이기 때문입니다.
# 실패 처리가 그래서 다릅니다 — 접수는 요청 1건에 대한 오류를 호출자에게 돌려주면
# 끝이고, 수집 주기가 부르는 이 흐름은 실패해도 다음 주기가 다시 만듭니다.
#   - commit은 여기서만 합니다. Repository는 commit하지 않습니다.
#   - 판정 자체를 하지 않습니다. 어떤 자산·위협이 Incident가 되는지는 판정 계층이
#     이미 정했고(services/rule_engine.py · security/risk_evaluator.py), 그 결과가
#     계약을 통과하는지는 IncidentIntake가 봅니다. 이 계층은 저장 순서와 중복만 봅니다.
#   - INCIDENT_CREATED 발행은 여기서 하지 않습니다. commit 이후 호출부가 발행합니다
#     (routers/incidents.py가 종료 처리에서 하는 것과 같은 경계 — 이 계층이 앱 상태
#     app.state.realtime을 알면 3층 분리가 깨집니다).
#
# 한 건이 저장되는 순서는 아래 하나입니다. 트랜잭션 1개에 담습니다 — 중간에서 끊기면
# 근거 없는 Incident나 Incident 없는 위협 이벤트가 남습니다.
#   SECOPS: ThreatEvent 저장 → Incident 생성(title·initial_risk_level·response_mode·
#           사유 코드) → THREAT 근거 1건
#   FINOPS: Incident 생성(위험 대응 축 전부 null) → RULE 근거 1건 → ASSET 근거 1건
#
# **두 근거의 content로 저장하는 값은 intake가 들고 온 객체 그 자체입니다.**
#   - RULE ← intake.rule_evaluation. 그래프 입력 빌더가 최상위 rule_evaluation을 이
#     근거 행에서 읽어야 두 값이 같은 객체에서 나온다는 불변식이 성립합니다
#     (agent_dispatcher.py 헤더 · Issue #243). 어긋난 조합은 FinOpsGraphInput 계약이
#     거절합니다(Issue #265).
#   - ASSET ← intake.asset_snapshot. 자산 행은 회차마다 덮어써지므로
#     (db/repositories/assets.py upsert_asset) 이 근거가 그 회차 자산의 유일한
#     사본이고, 빌더의 자산 문맥이 여기서 나옵니다 (Issue #265).
#
# 중복은 계약이 막지 못해 여기서 막습니다.
#   - FINOPS: 같은 subject_arn의 미종료 Incident가 있으면 만들지 않습니다. 수집
#     주기마다 같은 저활성 자산이 다시 판정되므로, 막지 않으면 한 자산에 카드가
#     주기 수만큼 쌓입니다. '미종료'의 기준은 INCIDENT_OPEN_STATUSES입니다
#     (schemas/incidents.py — RESOLVED만 빠집니다. FAILED는 관제자가 닫아야 풀립니다).
#   - SECOPS: deduplication_key로 막습니다(db/repositories/incidents.py
#     get_threat_event_by_dedup_key · insert_threat_event의 IntegrityError).
#   - 어느 쪽이든 기존 Incident를 그대로 돌려주고 created=False로 알립니다. 중복은
#     오류가 아니라 정상 경로입니다.
#   - FINOPS 판정은 읽고 나서 쓰는 형태라 **동시 호출이 없다는 전제**에 기댑니다
#     (수집 잡 max_instances=1 · worker 1개 — dispatcher.py와 같은 전제). SECOPS는
#     deduplication_key 유니크 제약이 DB에서 한 번 더 막지만 FINOPS에는 그런 제약이
#     없으므로, 다중 worker로 갈 때 이 자리를 함께 봐야 합니다.
#
# [남은 작업]
# 1. 판정 계층에서 이 진입점을 부르는 자리 — services/scheduler.py의 수집 파이프라인
#    (담당: 김세혁·김승철)과 위협 주입 경로. 위협 주입 방식은 ADR-0006이 별도 결정
#    대상으로 남겼습니다. 이때 DB 행에서 AssetItem을 만드는 자리도 함께 정합니다 —
#    지금 그 변환은 routers/assets.py의 private 함수 하나뿐인데, 이 계층이 Router
#    내부를 import하면 위 3층 분리가 깨집니다.
# 2. Incident를 만든 뒤 AI 호출로 넘기는 자리 — agent_dispatcher.py 본문.
#    이 계층은 Incident와 근거를 남기는 데까지고, 그 뒤를 그쪽이 잇습니다.
# ==============================================================================

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from schemas.api.incidents import IncidentCategory
from schemas.evidence import EvidenceItem, EvidenceType, RuleEvidence, ThreatEvidence
from schemas.intake import FinOpsIncidentIntake, IncidentIntake, SecOpsIncidentIntake

from db import mappers, models
from db.repositories import incidents as incidents_repo

logger = logging.getLogger("vigilantis.incident_intake")


@dataclass
class IntakeOutcome:
    """Intake 1건 처리 결과 — 로그·테스트와 발행 여부를 정하는 호출부가 읽는 값이다."""

    incident_id: str
    created: bool          # False = 중복이라 기존 Incident를 그대로 돌려줬다
    occurred_at: datetime  # 저장된 Incident.updated_at — WS 봉투의 occurred_at


def _existing(incident: models.Incident) -> IntakeOutcome:
    return IntakeOutcome(
        incident_id=incident.incident_id, created=False, occurred_at=incident.updated_at
    )


def _add_evidence(
    db: Session,
    *,
    incident_id: str,
    evidence_type: EvidenceType,
    source_type: str,
    source_id: str,
    content,
    occurred_at: datetime,
) -> None:
    """근거 1건 적재. evidence_id는 서버가 발급한다(EvidenceItem 계약이 요구)."""
    incidents_repo.add_evidence(
        db,
        EvidenceItem(
            evidence_id=str(uuid.uuid4()),
            incident_id=incident_id,
            evidence_type=evidence_type,
            source_type=source_type,
            source_id=source_id,
            content=content,
            occurred_at=occurred_at,
            collected_at=datetime.now(timezone.utc),
        ),
    )


def _create_finops(db: Session, intake: FinOpsIncidentIntake) -> IntakeOutcome:
    open_incident = incidents_repo.find_open_by_subject_arn(
        db, subject_arn=intake.subject_arn, category=IncidentCategory.FINOPS
    )
    if open_incident is not None:
        return _existing(open_incident)

    incident = incidents_repo.create_incident(
        db, subject_arn=intake.subject_arn, category=IncidentCategory.FINOPS
    )
    _add_evidence(
        db,
        incident_id=incident.incident_id,
        evidence_type=EvidenceType.RULE,
        source_type="rule_evaluation",
        source_id=intake.rule_evaluation.collection_run_id,
        content=RuleEvidence(evaluation=intake.rule_evaluation),
        occurred_at=intake.rule_evaluation.evaluated_at,
    )
    _add_evidence(
        db,
        incident_id=incident.incident_id,
        evidence_type=EvidenceType.ASSET,
        source_type="asset",
        source_id=intake.asset_snapshot.asset.arn,
        content=intake.asset_snapshot,
        occurred_at=intake.asset_snapshot.asset.collected_at,
    )
    db.commit()
    return IntakeOutcome(
        incident_id=incident.incident_id, created=True, occurred_at=incident.updated_at
    )


def _create_secops(db: Session, intake: SecOpsIncidentIntake) -> IntakeOutcome:
    event = intake.threat_event
    seen = incidents_repo.get_threat_event_by_dedup_key(db, event.deduplication_key)
    if seen is not None:
        existing = incidents_repo.get_incident_by_threat_event_id(db, seen.threat_event_id)
        if existing is not None:
            return _existing(existing)
        # 이벤트만 남고 Incident가 없는 조합은 이 계층이 트랜잭션 1개로 저장하는 한
        # 생기지 않는다. 남아 있다면 앞선 저장이 중간에서 끊긴 것이므로 **저장된
        # 이벤트를 기준으로** 이어서 만든다 — 새로 들어온 event의 식별자를 쓰면 있지도
        # 않은 행에 FK를 건다.
        logger.warning("threat_event_without_incident", extra={
            "threat_event_id": seen.threat_event_id,
        })
        event = mappers.to_threat_event(seen)
    else:
        try:
            # SAVEPOINT — 충돌 시 이 INSERT만 되감는다. 세션은 호출부 소유라
            # db.rollback()으로 세션 전체를 되감으면 호출부가 아직 commit하지 않은
            # 일감까지 사라진다(수집 파이프라인이 한 세션을 물고 돈다 —
            # services/scheduler.py run_pipeline). dispatcher.py의 rollback은 세션을
            # 소유한 최상위 루프가 작업 1건을 되감는 것이라 이 자리와 다르다.
            with db.begin_nested():
                incidents_repo.insert_threat_event(db, event)
        except IntegrityError:
            # 같은 키가 동시에 들어온 경우 — 먼저 넣은 쪽의 Incident를 돌려준다
            seen = incidents_repo.get_threat_event_by_dedup_key(db, event.deduplication_key)
            existing = (
                incidents_repo.get_incident_by_threat_event_id(db, seen.threat_event_id)
                if seen is not None
                else None
            )
            if existing is None:
                raise
            return _existing(existing)

    incident = incidents_repo.create_incident(
        db,
        subject_arn=intake.subject_arn,
        category=IncidentCategory.SECOPS,
        title=intake.title,
        threat_event_id=event.threat_event_id,
        initial_risk_level=intake.initial_risk.initial_risk_level,
        response_mode=intake.initial_risk.response_mode,
        initial_risk_reason_codes=[c.value for c in intake.initial_risk.reason_codes],
    )
    _add_evidence(
        db,
        incident_id=incident.incident_id,
        evidence_type=EvidenceType.THREAT,
        source_type="threat_event",
        source_id=event.threat_event_id,
        content=ThreatEvidence(event=event),
        occurred_at=event.occurred_at,
    )
    db.commit()
    return IntakeOutcome(
        incident_id=incident.incident_id, created=True, occurred_at=incident.updated_at
    )


def create_incident_from_intake(db: Session, intake: IncidentIntake) -> IntakeOutcome:
    """Intake 1건 → Incident 1건. **세션 수명은 호출부가 소유한다.**

    created=True인 결과에 대해서만 호출부가 INCIDENT_CREATED를 발행한다.
    """
    if intake.category == IncidentCategory.SECOPS:
        return _create_secops(db, intake)
    return _create_finops(db, intake)
