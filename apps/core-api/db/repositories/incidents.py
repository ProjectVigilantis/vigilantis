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

from collections.abc import Sequence
from datetime import datetime
from typing import Optional

from sqlalchemy import and_, or_, select, update
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
from schemas.runbooks import TriggerSource
from tenancy import ALL_TENANTS, TenantScope

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


def get_threat_events_by_ids(
    db: Session, threat_event_ids: Sequence[str],
) -> dict[str, models.ThreatEvent]:
    """목록의 위협 문맥을 한 번에 조회한다. 연결이 없으면 DB 조회도 생략한다."""
    if not threat_event_ids:
        return {}
    rows = db.scalars(
        select(models.ThreatEvent).where(
            models.ThreatEvent.threat_event_id.in_(set(threat_event_ids))
        )
    )
    return {row.threat_event_id: row for row in rows}


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
    tenant_id: Optional[str] = None,
) -> models.Incident:
    """SECOPS/FINOPS 형태 불변식은 DB CheckConstraint가 flush 시점에 거절한다.

    tenant_id 를 주지 않으면 소속 없는 MVP 기록(NULL)이다. 소속은 만들 때 정하고 옮기지 않는다."""
    incident = models.Incident(
        tenant_id=tenant_id,
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
    scope: TenantScope = ALL_TENANTS,
) -> list[models.Incident]:
    """공개 목록 계약과 동일하게 created_at 내림차순 전체 반환(페이지네이션 Post-MVP).

    전체 범위(명시 CENTRAL_ADMIN · 서버 내부)만 소속 없는 MVP 기록(NULL)을 본다. 그 밖의 범위는 그 테넌트들의
    기록뿐이다 — NULL 은 IN 비교에 걸리지 않아 빠지고, 빈 범위(배정 없는 담당자)는 아무것도 보지 않는다."""
    stmt = select(models.Incident).order_by(models.Incident.created_at.desc())
    if not scope.is_platform:
        stmt = stmt.where(models.Incident.tenant_id.in_(scope.tenant_ids))
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
        values |= {"resolution": None, "resolved_at": None, "resolution_note": None}
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
    resolution_note: str | None = None,
) -> bool:
    """expected 상태에서만 RESOLVED로 옮기고 관제자 판단을 함께 남긴다.

    상태와 판단을 한 UPDATE로 쓴다 — 나눠 쓰면 그 사이에 조회가 들어와 판단 없는
    RESOLVED를 보게 되고, DB 제약(resolution_with_resolved_status)도 거절한다.
    종료 시각은 여기서 찍는다(touch_incident와 같은 자리) — updated_at은 자식 상태
    변경으로도 올라가 종료 시점을 가리키지 못한다. 허용 출발 상태 판단은 Workflow
    몫이다(3층 분리).

    끝난 승인 대기 시각도 지워 이전 기한이 이후 대기 판단에 쓰이지 않게 한다.
    새 대기가 필요하면 set_agent_wait로 설정해야 하며, 재개만으로 시작되지는 않는다.
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
            resolution_note=resolution_note,
            resolved_at=models._utcnow(),
            agent_wait_started_at=None,
            response_deadline_at=None,
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

    High 선차단이 있었던 SecOps는 분석 전에 상태가 바뀔 수 있다. 선차단이 실패로 끝나
    FAILED가 된 건도 넣는다 — 빼면 호출 상태가 PENDING에 남고, 분석이 끝나지 않은 SecOps는
    종료할 수 없어(workflows.resolve_incident) 그 Incident가 닫히지 않는다. (Issue #447)
    """
    return [
        (row.incident_id, row.category)
        for row in db.execute(
            select(models.Incident.incident_id, models.Incident.category)
            .where(
                or_(
                    models.Incident.status == IncidentStatus.ANALYZING,
                    and_(
                        models.Incident.category == IncidentCategory.SECOPS,
                        models.Incident.status.in_([
                            IncidentStatus.ACTION_IN_PROGRESS,
                            IncidentStatus.AWAITING_CLOSURE,
                            IncidentStatus.FAILED,
                        ]),
                        select(models.ActionExecution.execution_id).where(
                            models.ActionExecution.incident_id == models.Incident.incident_id,
                            models.ActionExecution.trigger_source == TriggerSource.PRE_MITIGATION_0_5S,
                        ).exists(),
                    ),
                ),
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


# --- 승인 대기 만료 (Issue #447) -------------------------------------------------

# EC2 인스턴스 ARN의 사전 필터. services/aws/executor.parse_arn이 받는 형식
# (arn:<partition>:ec2:<region>:<account>:instance/<id>)을 빠짐없이 포함하는 상위집합이다 —
# 판정은 Workflow가 잠근 뒤 그 파서로 다시 한다.
_EC2_INSTANCE_ARN_LIKE = "arn:%:ec2:%:%:instance/_%"


def _user_approval_recorded():
    """그 Incident에 관제자 승인으로 접수된 실행이 있다 — 승인 대기에 응답한 기록.

    승인 대기는 Incident당 한 번이다. 기한은 set_agent_wait가 대기 미시작 상태에서만 쓰고,
    그것을 부르는 분석 저장은 Incident마다 한 번이다(Terminal 호출 상태는 다시 Claim되지
    않는다). 그래서 Incident의 USER_APPROVAL 실행이 곧 그 대기에 대한 응답이며, 서로 다른
    경로가 찍은 시각(대기 시작·실행 생성)을 비교하지 않는다.
    """
    return (
        select(models.ActionExecution.execution_id)
        .where(
            models.ActionExecution.incident_id == models.Incident.incident_id,
            models.ActionExecution.trigger_source == TriggerSource.USER_APPROVAL,
        )
        .exists()
    )


def user_approval_recorded(db: Session, incident_id: str) -> bool:
    """commit된 관제자 승인 실행이 있는가. 현재 Incident 상태와 무관한 기록이다."""
    return db.execute(
        select(models.Incident.incident_id).where(
            models.Incident.incident_id == incident_id,
            _user_approval_recorded(),
        )
    ).first() is not None


def list_expired_agent_waits(db: Session, *, now: datetime) -> list[str]:
    """응답 기한이 지난 Medium 승인 대기의 Incident 식별자 — 타임아웃 감시 1회의 대상.

    식별자만 돌려준다 — 감시는 건마다 잠그고 커밋하며 도는데, 들고 있던 행은 그 사이에
    낡는다(list_pending_agent_analysis와 같은 이유). 발동 여부는 Workflow가 Incident를
    잠근 뒤 다시 판정한다(workflows.reserve_auto_isolation). 이 조회는 대상을 좁힐 뿐이다.

    기한은 승인 대기 시작 때 저장된 response_deadline_at을 그대로 비교한다. 다시 늘리지
    않으므로 재시작 뒤에도 지난 기한은 그대로 잡힌다.
      - 초기 위험도 MEDIUM·AGENT_WAIT·AWAITING_APPROVAL만 — Low는 기한이 저장돼 있어도 뺀다.
        타임아웃이 접수되면 response_mode가 바뀌어 다음 주기부터 빠진다.
      - EC2 인스턴스 대상만 — 자동 격리는 EC2_ISOLATE 하나다. 다른 대상(OPEN_IP의 SG)을
        넣으면 관제자가 처리할 때까지 주기마다 다시 잡혀 잠금·거절만 반복한다.
      - 관제자 승인 실행이 있었던 건은 뺀다. 승인한 실행이 끝나고 다른 제안이 남으면 상태는
        다시 AWAITING_APPROVAL이지만 그 대기는 이미 응답됐다.
      - 테넌트가 자동 조치를 껐거나, 그 기한이 꺼진 동안(마지막으로 켠 시각 이전) 지났으면 뺀다(T-6 ·
        #514). 넣으면 사람이 응답할 때까지 주기마다 잠그고 건너뛰기만 반복한다. 판정의 원천은
        workflows.automatic_trigger_block 이고 이 조건은 그 거울이다. 테넌트 없음(NULL)은 지금 그대로다.
    """
    tenant = models.Tenant
    return list(
        db.execute(
            select(models.Incident.incident_id)
            .outerjoin(tenant, tenant.tenant_id == models.Incident.tenant_id)
            .where(
                models.Incident.initial_risk_level == RiskLevel.MEDIUM,
                models.Incident.response_mode == ResponseMode.AGENT_WAIT,
                models.Incident.status == IncidentStatus.AWAITING_APPROVAL,
                models.Incident.response_deadline_at <= now,
                models.Incident.subject_arn.like(_EC2_INSTANCE_ARN_LIKE),
                ~_user_approval_recorded(),
                or_(
                    models.Incident.tenant_id.is_(None),
                    and_(
                        tenant.auto_action_enabled.is_(True),
                        or_(
                            tenant.auto_action_resumed_at.is_(None),
                            models.Incident.response_deadline_at > tenant.auto_action_resumed_at,
                        ),
                    ),
                ),
            )
            .order_by(models.Incident.response_deadline_at, models.Incident.incident_id)
        ).scalars()
    )


def update_response_mode(
    db: Session,
    incident_id: str,
    *,
    expected: ResponseMode,
    next_mode: ResponseMode,
) -> bool:
    """현재 대응 경로를 expected일 때만 바꾼다. 승인 대기 시각은 건드리지 않는다."""
    result = db.execute(
        update(models.Incident)
        .where(
            models.Incident.incident_id == incident_id,
            models.Incident.response_mode == expected,
        )
        .values(response_mode=next_mode)
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
