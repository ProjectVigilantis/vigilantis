# ==============================================================================
# [파일 설명]
# 위협·인시던트·근거·후보 저장소 — ThreatEvent·Incident·Evidence·RunbookCandidate.
# (Issue #60)
#
#   - AI 호출 Claim(PENDING→IN_PROGRESS)은 조건부 UPDATE 1건으로 원자적으로
#     수행한다 — 프로세스 수와 무관하게 중복 호출을 막는 근거(ADR-0005 결정 2).
#   - Incident 상태 전이는 expected→next 조건부 갱신만 제공한다. 허용 전이의
#     계산은 Workflow 몫이다(3층 분리).
# ==============================================================================

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from schemas.api.incidents import (
    IncidentCategory,
    IncidentStatus,
    ResolutionJudgement,
    ResponseMode,
    RiskLevel,
)
from schemas.candidates import CandidateStatus, RunbookCandidateData
from schemas.events import NormalizedThreatEvent
from schemas.evidence import EvidenceItem
from schemas.incidents import (
    AGENT_TERMINAL_STATUSES,
    INCIDENT_OPEN_STATUSES,
    AgentInvocationStatus,
    AgentWaitSchedule,
)

from .. import mappers, models

# --- ThreatEvent ---------------------------------------------------------------


def insert_threat_event(
    db: Session, contract: NormalizedThreatEvent
) -> models.ThreatEvent:
    """중복 키 충돌(IntegrityError)은 호출부가 중복 이벤트로 해석한다."""
    row = mappers.new_threat_event(contract)
    db.add(row)
    db.flush()
    return row


def get_threat_event_by_dedup_key(
    db: Session, deduplication_key: str
) -> Optional[models.ThreatEvent]:
    return db.execute(
        select(models.ThreatEvent).where(
            models.ThreatEvent.deduplication_key == deduplication_key
        )
    ).scalar_one_or_none()


# --- Incident ------------------------------------------------------------------


def create_incident(
    db: Session,
    *,
    subject_arn: str,
    category: IncidentCategory,
    title: Optional[str] = None,
    threat_event_id: Optional[str] = None,
    initial_risk_level: Optional[RiskLevel] = None,
    response_mode: Optional[ResponseMode] = None,
    initial_risk_reason_codes: Sequence[str] = (),
) -> models.Incident:
    """SECOPS/FINOPS 형태 불변식은 DB CheckConstraint가 flush 시점에 거절한다."""
    incident = models.Incident(
        subject_arn=subject_arn,
        category=category,
        title=title,
        threat_event_id=threat_event_id,
        initial_risk_level=initial_risk_level,
        response_mode=response_mode,
        initial_risk_reason_codes=list(initial_risk_reason_codes),
    )
    db.add(incident)
    db.flush()
    return incident


def get_incident(db: Session, incident_id: str) -> Optional[models.Incident]:
    return db.execute(
        select(models.Incident).where(models.Incident.incident_id == incident_id)
    ).scalar_one_or_none()


def lock_incident(db: Session, incident_id: str) -> Optional[models.Incident]:
    """행 잠금 후 최신 상태로 다시 읽는다. 접수 시 상태 전이가 현재 상태를 전제로
    하므로, 잠그지 않으면 동시 접수가 서로의 전이를 덮어쓴다 (Issue #126)."""
    return db.execute(
        select(models.Incident)
        .where(models.Incident.incident_id == incident_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def find_open_by_subject_arn(
    db: Session, *, subject_arn: str, category: IncidentCategory
) -> Optional[models.Incident]:
    """같은 대상의 미종료 Incident 1건. 중복 생성 판정에 쓴다 (Issue #265).

    '미종료'의 기준은 INCIDENT_OPEN_STATUSES다(schemas/incidents.py). 여러 건이면
    가장 최근 것을 돌려준다 — 이 계층은 집합을 판정하지 않고 존재만 알린다.
    """
    return db.execute(
        select(models.Incident)
        .where(
            models.Incident.subject_arn == subject_arn,
            models.Incident.category == category,
            models.Incident.status.in_(INCIDENT_OPEN_STATUSES),
        )
        .order_by(models.Incident.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_incident_by_threat_event_id(
    db: Session, threat_event_id: str
) -> Optional[models.Incident]:
    """위협 이벤트에서 생긴 Incident. 중복 위협 재수신 시 기존 건을 찾는 데 쓴다."""
    return db.execute(
        select(models.Incident).where(models.Incident.threat_event_id == threat_event_id)
    ).scalar_one_or_none()


def list_incidents(
    db: Session,
    *,
    status: Optional[IncidentStatus] = None,
    category: Optional[IncidentCategory] = None,
) -> list[models.Incident]:
    """공개 목록 계약과 동일하게 created_at 내림차순 전체 반환(페이지네이션 Post-MVP)."""
    stmt = select(models.Incident).order_by(models.Incident.created_at.desc())
    if status is not None:
        stmt = stmt.where(models.Incident.status == status)
    if category is not None:
        stmt = stmt.where(models.Incident.category == category)
    return list(db.execute(stmt).scalars())


def update_incident_status(
    db: Session,
    incident_id: str,
    *,
    expected: IncidentStatus,
    next_status: IncidentStatus,
    clear_resolution: bool = False,
) -> bool:
    """clear_resolution은 RESOLVED에서 나오는 전이에 쓴다 — 종료 판단은 그 종료를
    설명하는 값이라 재개되면 남겨 둘 수 없고, DB 제약(resolution_with_resolved_status)이
    RESOLVED 밖의 판단을 거절한다. 전이와 한 UPDATE로 묶어 중간 상태를 만들지 않는다."""
    values: dict = {"status": next_status}
    if clear_resolution:
        values |= {"resolution": None, "resolved_at": None}
    result = db.execute(
        update(models.Incident)
        .where(
            models.Incident.incident_id == incident_id,
            models.Incident.status == expected,
        )
        .values(**values)
    )
    return result.rowcount == 1


def resolve_incident(
    db: Session,
    incident_id: str,
    *,
    expected: IncidentStatus,
    resolution: ResolutionJudgement,
) -> bool:
    """expected 상태에서만 RESOLVED로 옮기고 관제자 판단을 함께 남긴다.

    상태와 판단을 한 UPDATE로 쓴다 — 나눠 쓰면 그 사이에 조회가 들어와 판단 없는
    RESOLVED를 보게 되고, DB 제약(resolution_with_resolved_status)도 거절한다.
    종료 시각은 여기서 찍는다(touch_incident와 같은 자리) — updated_at은 자식 상태
    변경으로도 올라가 종료 시점을 가리키지 못한다. 허용 출발 상태 판단은 Workflow
    몫이다(3층 분리).
    """
    result = db.execute(
        update(models.Incident)
        .where(
            models.Incident.incident_id == incident_id,
            models.Incident.status == expected,
        )
        .values(
            status=IncidentStatus.RESOLVED,
            resolution=resolution,
            resolved_at=models._utcnow(),
        )
    )
    return result.rowcount == 1


def touch_incident(db: Session, incident_id: str) -> bool:
    """상세 응답에 포함되는 자식 상태가 바뀔 때 부모 updated_at을 함께 올린다."""
    result = db.execute(
        update(models.Incident)
        .where(models.Incident.incident_id == incident_id)
        .values(updated_at=models._utcnow())
    )
    return result.rowcount == 1


# --- AI 호출 상태 --------------------------------------------------------------


def list_pending_agent_analysis(
    db: Session,
) -> list[tuple[str, IncidentCategory]]:
    """AI 분석을 기다리는 Incident의 (식별자, 분류). 스캔 1회의 대상이다 (Issue #285).

    행이 아니라 식별자만 돌려준다 — 스캔은 건마다 커밋하며 도는데, 들고 있던 행 상태는
    그 사이에 낡아 선점 재확인이 무의미해진다(agent_dispatcher.py · dispatcher.py와 같다).

    분류를 함께 싣는 것은 **선점하기 전에** 그래프가 있는 분류인지 갈라야 하기 때문이다.
    분류로 걸러 버리면 스캔이 넘기지 못한 건수를 셀 수 없고, 선점한 뒤에 갈라내면 그래프가
    없는 Incident가 주기마다 선점·해제를 반복한다.
    """
    return [
        (row.incident_id, row.category)
        for row in db.execute(
            select(models.Incident.incident_id, models.Incident.category)
            .where(
                models.Incident.status == IncidentStatus.ANALYZING,
                models.Incident.agent_invocation_status == AgentInvocationStatus.PENDING,
            )
            .order_by(models.Incident.created_at)
        )
    ]


def list_stale_agent_claims(db: Session, *, started_before: datetime) -> list[str]:
    """상한을 넘긴 IN_PROGRESS Claim의 Incident 식별자. 회수 대상 판단은 Workflow 몫이다.

    started_at이 NULL인 IN_PROGRESS도 함께 돌려준다. claim_agent_invocation은 상태와
    시각을 한 UPDATE로 쓰고 reset은 둘을 함께 되돌리므로 그 조합은 정상 경로에서 생기지
    않지만, 생겼다면 시각 비교가 NULL을 걸러내 **영원히 회수되지 않는다** — 회수만이
    그 행을 푸는 유일한 길이라 여기서 받는다.
    """
    return list(
        db.execute(
            select(models.Incident.incident_id)
            .where(
                models.Incident.agent_invocation_status
                == AgentInvocationStatus.IN_PROGRESS,
                or_(
                    models.Incident.agent_invocation_started_at < started_before,
                    models.Incident.agent_invocation_started_at.is_(None),
                ),
            )
            .order_by(models.Incident.created_at)
        ).scalars()
    )


def claim_agent_invocation(
    db: Session, incident_id: str, *, started_at: datetime
) -> bool:
    """PENDING인 경우에만 IN_PROGRESS로 선점한다 — 성공한 호출자만 그래프를 부른다."""
    result = db.execute(
        update(models.Incident)
        .where(
            models.Incident.incident_id == incident_id,
            models.Incident.agent_invocation_status == AgentInvocationStatus.PENDING,
        )
        .values(
            agent_invocation_status=AgentInvocationStatus.IN_PROGRESS,
            agent_invocation_started_at=started_at,
        )
    )
    return result.rowcount == 1


def finish_agent_invocation(
    db: Session,
    incident_id: str,
    terminal_status: AgentInvocationStatus,
    *,
    summary_lines: Optional[Sequence[str]] = None,
    reviewed_risk_level: Optional[RiskLevel] = None,
) -> bool:
    """IN_PROGRESS → Terminal 3종만 허용한다. Terminal은 다시 Claim되지 않는다."""
    if terminal_status not in AGENT_TERMINAL_STATUSES:
        raise ValueError(f"Terminal 상태가 아닙니다: {terminal_status}")
    values: dict = {"agent_invocation_status": terminal_status}
    if summary_lines is not None:
        values["summary_lines"] = list(summary_lines)
    if reviewed_risk_level is not None:
        values["reviewed_risk_level"] = reviewed_risk_level
    result = db.execute(
        update(models.Incident)
        .where(
            models.Incident.incident_id == incident_id,
            models.Incident.agent_invocation_status == AgentInvocationStatus.IN_PROGRESS,
        )
        .values(**values)
    )
    return result.rowcount == 1


def reset_agent_invocation(db: Session, incident_id: str) -> bool:
    """중단된 IN_PROGRESS Claim 회수(IN_PROGRESS→PENDING). 회수 대상 판단은 Workflow 몫."""
    result = db.execute(
        update(models.Incident)
        .where(
            models.Incident.incident_id == incident_id,
            models.Incident.agent_invocation_status == AgentInvocationStatus.IN_PROGRESS,
        )
        .values(
            agent_invocation_status=AgentInvocationStatus.PENDING,
            agent_invocation_started_at=None,
        )
    )
    return result.rowcount == 1


def set_agent_wait(db: Session, schedule: AgentWaitSchedule) -> bool:
    """승인 대기 시작 — 대기 미시작 상태에서만 설정한다(재설정 금지)."""
    result = db.execute(
        update(models.Incident)
        .where(
            models.Incident.incident_id == schedule.incident_id,
            models.Incident.agent_wait_started_at.is_(None),
        )
        .values(
            agent_wait_started_at=schedule.started_at,
            response_deadline_at=schedule.response_deadline_at,
        )
    )
    return result.rowcount == 1


# --- Evidence ------------------------------------------------------------------


def add_evidence(db: Session, contract: EvidenceItem) -> models.Evidence:
    row = mappers.new_evidence(contract)
    db.add(row)
    db.flush()
    return row


def list_evidence(db: Session, incident_id: str) -> list[models.Evidence]:
    return list(
        db.execute(
            select(models.Evidence)
            .where(models.Evidence.incident_id == incident_id)
            .order_by(models.Evidence.occurred_at)
        ).scalars()
    )


# --- RunbookCandidate ----------------------------------------------------------


def add_candidate(
    db: Session, contract: RunbookCandidateData
) -> models.RunbookCandidate:
    """활성 (incident, runbook) 중복은 부분 유니크 인덱스가 flush 시점에 거절한다."""
    row = mappers.new_candidate(contract)
    db.add(row)
    db.flush()
    return row


def get_candidate(db: Session, candidate_id: str) -> Optional[models.RunbookCandidate]:
    return db.execute(
        select(models.RunbookCandidate).where(
            models.RunbookCandidate.candidate_id == candidate_id
        )
    ).scalar_one_or_none()


def list_candidates(
    db: Session, incident_id: str, *, status: Optional[CandidateStatus] = None
) -> list[models.RunbookCandidate]:
    stmt = select(models.RunbookCandidate).where(
        models.RunbookCandidate.incident_id == incident_id
    )
    if status is not None:
        stmt = stmt.where(models.RunbookCandidate.status == status)
    return list(db.execute(stmt).scalars())


def update_candidate_status(
    db: Session,
    candidate_id: str,
    *,
    expected: CandidateStatus,
    next_status: CandidateStatus,
) -> bool:
    """PENDING_VALIDATION→EXECUTABLE|REJECTED, EXECUTABLE→CLAIMED|INVALIDATED 같은
    허용 전이 판단은 Workflow가 한다 — 여기는 expected 일치 시에만 갱신한다."""
    result = db.execute(
        update(models.RunbookCandidate)
        .where(
            models.RunbookCandidate.candidate_id == candidate_id,
            models.RunbookCandidate.status == expected,
        )
        .values(status=next_status)
    )
    return result.rowcount == 1
