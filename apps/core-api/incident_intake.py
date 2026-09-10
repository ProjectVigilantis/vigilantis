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
#           → METRIC 근거 0–1건(그 회차 관측이 있을 때만)
#
# **RULE·ASSET 근거의 content로 저장하는 값은 intake가 들고 온 객체 그 자체입니다.**
#   - RULE ← intake.rule_evaluation. 그래프 입력 빌더가 최상위 rule_evaluation을 이
#     근거 행에서 읽어야 두 값이 같은 객체에서 나온다는 불변식이 성립합니다
#     (agent_dispatcher.py 헤더 · Issue #243). 어긋난 조합은 FinOpsGraphInput 계약이
#     거절합니다(Issue #265).
#   - ASSET ← intake.asset_snapshot. 자산 행은 회차마다 덮어써지므로
#     (db/repositories/assets.py upsert_asset) 이 근거가 그 회차 자산의 유일한
#     사본이고, 빌더의 자산 문맥이 여기서 나옵니다 (Issue #265).
#
# **METRIC 근거만 intake가 아니라 DB에서 읽습니다.** 계약으로 나르지 않는 이유는
# 대조할 수가 없기 때문입니다 — MetricEvidence에는 collection_run_id가 없어
# (schemas/evidence.py), 계약에 실으면 "같은 회차인가"를 아무도 못 봅니다. 반면
# (asset_arn, collection_run_id)로 읽으면 같은 회차임이 조회 자체로 보장됩니다
# (db/models.py MetricSummary의 (asset_id, collection_run_id) 유니크).
#
# **이 근거가 없으면 AI는 사용률을 못 봅니다.** rule_evaluation.reason은
# "EC2 rule evaluation: verdict=COST_CANDIDATE" 한 줄이라 수치가 없고, ASSET 근거의
# AssetItem도 판정 표기만 담습니다(asset_mapping.py to_asset_item). 그래서 METRIC이 비면
# 그래프 입력에 CPU 평균·최대·데이터포인트 수가 **한 군데도** 들어가지 않습니다 —
# 2026-09-10 게이트 예비 실행에서 같은 자산·같은 입력이 NO_PROPOSAL과 SUCCEEDED로
# 갈린 원인이 이것입니다(모델이 요약 3줄에 "입력에 없다"를 직접 적었습니다).
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
# [호출 경로]
# FINOPS는 services/scheduler.py가 판정과 같은 회차의 자산 스냅샷을 조립해 부릅니다
# (#306). AssetItem 변환은 asset_mapping.py를 목록 API와 공유합니다.
# 생성 뒤 AI 호출은 agent_dispatcher.py가 맡습니다(#285).
# 남은 것은 SECOPS 위협 주입 경로입니다 — #306 범위 밖이며 ADR-0006의 별도 결정 대상입니다.
# ==============================================================================

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from schemas.api.incidents import IncidentCategory
from schemas.assets import MetricName
from schemas.assets import MetricSummary as MetricSummaryContract
from schemas.evidence import (
    EvidenceItem,
    EvidenceType,
    MetricEvidence,
    RuleEvidence,
    ThreatEvidence,
)
from schemas.intake import FinOpsIncidentIntake, IncidentIntake, SecOpsIncidentIntake

from db import mappers, models
from db.repositories import assets as assets_repo
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


def _metric_evidence(
    db: Session, *, asset_arn: str, collection_run_id: str
) -> Optional[MetricEvidence]:
    """그 회차 관측 요약 1건 → METRIC 근거. 행이 없으면 None.

    metric_name을 CPU_UTILIZATION으로 고정하는 것은 MetricEvidence가 이름 1개를
    받는데 요약 자체는 CPU·Network를 함께 담기 때문이다(schemas/evidence.py ·
    schemas/assets.py MetricSummary). 판정 축이 CPU라(services/rule_engine.py
    IDLE_CPU_AVG) 대표 이름을 그쪽으로 두고, 값은 요약째로 보존한다.
    """
    row = assets_repo.get_metric_summary_for_run(
        db, asset_arn=asset_arn, collection_run_id=collection_run_id
    )
    if row is None:
        return None
    return MetricEvidence(
        metric_name=MetricName.CPU_UTILIZATION,
        window_start=row.window_start,
        window_end=row.window_end,
        summary=MetricSummaryContract(
            cpu_datapoints=row.cpu_datapoints,
            cpu_avg=row.cpu_avg,
            cpu_max=row.cpu_max,
            net_in_avg=row.net_in_avg,
            net_out_avg=row.net_out_avg,
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
    metric = _metric_evidence(
        db,
        asset_arn=intake.asset_snapshot.asset.arn,
        collection_run_id=intake.asset_snapshot.collection_run_id,
    )
    if metric is not None:
        _add_evidence(
            db,
            incident_id=incident.incident_id,
            evidence_type=EvidenceType.METRIC,
            source_type="metric_summary",
            source_id=intake.asset_snapshot.collection_run_id,
            content=metric,
            occurred_at=metric.window_end,
        )
    else:
        # 없어도 Incident는 만든다 — 관측이 없는 판정(UNUSED SG·미부착 EBS)이 정상이기
        # 때문이다. 다만 CPU로 판정된 건에서 비면 AI가 사용률을 못 보므로 남긴다.
        logger.warning(
            "intake_metric_summary_missing",
            extra={
                "subject_arn": intake.subject_arn,
                "collection_run_id": intake.asset_snapshot.collection_run_id,
                "verdict": intake.rule_evaluation.verdict.value,
            },
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
