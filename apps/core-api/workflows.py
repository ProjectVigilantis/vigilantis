# ==============================================================================
# [파일 설명]
# 업무 흐름 계층 — Router와 Repository 사이에서 처리 순서·상태 전이·트랜잭션
# 경계를 소유한다(3층 분리: Router → Workflow → Repository). (Issue #116)
#
#   - commit은 여기서만 한다. Repository는 commit하지 않는다.
#   - **AI 후보 경로는** 4단계 가드레일을 실행 시점에 다시 부르지 않는다. 가드레일은
#     AI 제안 생성 직후 1회 수행되고 통과한 제안이 EXECUTABLE이 되므로, 여기서는 그
#     상태를 확인만 한다 (Issue #113 §2, packages/schemas/candidates.py). 단 저장된
#     행이 현행 후보 계약(typed parameters, #154)대로인지는 접수 시점에 재확인한다 —
#     가드레일 재실행이 아니라 저장소 무결성 확인이다.
#   - **원복 경로에는 그 1회가 없다.** 롤백 3종은 후보가 될 수 없어(ADR-0004 정책 ②)
#     제안 생성 시점 자체가 없으므로, 4단계를 실행 직전에 부르는 것이 유일한 자리다
#     (run_revert_size_execution → _run_rollback_guardrails). 두 경로가 다른 것은
#     **부르는 시점**이지 통과해야 하는 단계 수가 아니다 — 정책 ①은 롤백도 네 단계를
#     본편과 동일하게 전부 지날 것을 요구한다. (Issue #241)
#   - 접수(reserve_execution)와 실행(run_rightsizing_execution)은 갈라져 있다. 예약은
#     IN_PROGRESS 레코드를 남기는 데까지고, 그 예약을 실행으로 넘기는 디스패치는
#     dispatcher.py 몫이다. 실행 직전 대상 자산 재확인과 후보 INVALIDATED 전이는
#     아직 붙지 않았다.
#   - 실행(run_rightsizing_execution)은 단계 기록까지만 커밋하고 **종료 상태는
#     확정하지 않는다.** 확정은 close_execution 하나가 하며, 실행 종료와 Incident
#     전이를 한 트랜잭션에 넣는다 — 실행만 먼저 옮기면 ACTION_IN_PROGRESS인데
#     진행 중 실행이 없는 조합이 생겨 상세 조회가 500이 된다(api/incidents.py
#     응답 계약). 그 둘을 언제 부를지 고르는 것은 dispatcher.py다. (Issue #232)
#   - 조치 직전 스펙 JSON 백업은 여기서 커밋한다(store_instance_spec_backup).
#     캡처는 services/aws/backup.py가, 저장·결속·커밋 순서는 이 계층이 소유한다 —
#     "AWS 변경보다 먼저 커밋"이 트랜잭션 경계의 문제이기 때문이다.
#   - 접수 경로는 둘이다. 본편 7종은 Guardrail PASS 후보(EXECUTABLE)에서, 롤백
#     3종은 복구를 열어 준 원본 실행에서 접수한다 — 롤백은 후보가 될 수 없다
#     (ADR-0004 정책 ②, packages/schemas/candidates.py). (Issue #126)
# ==============================================================================

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from schemas.agents import AgentGraphOutput
from schemas.api.actions import (
    ExecuteActionRequest,
    ExecuteActionResponse,
    ExecutionStatus,
)
from schemas.api.errors import ErrorCode
from schemas.api.incidents import IncidentStatus, ResolutionJudgement
from schemas.backups import InstanceSpecBackup
from schemas.candidates import CandidateStatus, RunbookCandidateData
from schemas.executions import (
    ASSET_MAY_HAVE_CHANGED_EFFECTS,
    EXECUTION_NON_TERMINAL_STATUSES,
    EXECUTION_SETTLED_STATUSES,
    EXECUTION_TERMINAL_STATUSES,
    EXECUTION_RECOVERABLE_STATUSES,
    ExecutionEffect,
    ExecutionStepResult,
    ExecutionStepStatus,
)
from schemas.guardrails import (
    GuardrailDecision,
    GuardrailValidationContext,
    GuardrailValidationRequest,
    GuardrailValidationResult,
)
from schemas.incidents import INCIDENT_RESOLVABLE_STATUSES
from schemas.precheck import (
    PrecheckOutcome,
    PrecheckReasonCode,
    VerificationMethod,
    build_verification_summary,
)
from schemas.runbook_parameters import build_precheck_parameters
from schemas.runbooks import (
    ROLLBACK_RUNBOOK_BY_MAIN_ID,
    ROLLBACK_RUNBOOK_IDS,
    RunbookId,
    TriggerSource,
)

from ai import guardrails
from db import mappers, models
from db.repositories import assets as assets_repo
from db.repositories import executions as executions_repo
from db.repositories import guardrails as guardrails_repo
from db.repositories import incidents as incidents_repo
from exceptions import ApiError
from identifiers import canonical_id
from services.aws import backup, executor, rollback
from services.aws.executor import parse_arn

logger = logging.getLogger("vigilantis.workflow")


def _candidate_meets_contract(candidate: models.RunbookCandidate) -> bool:
    """저장된 후보가 현행 후보 계약(#154 typed parameters)에 맞는가.

    후보의 정상 경로는 계약(RunbookCandidateData) 검증을 거쳐 저장되지만, DB에는
    그 경로를 우회한 행이 있을 수 있다 — typed 계약 이전에 저장된 행과 마이그레이션
    backfill(빈 parameters)이 그렇다. EXECUTABLE 상태만 믿으면 ① Schema Check가
    세운 계약이 실행 접수에서 무너진다.

    접수만 막고 상세의 recommendations 노출은 거르지 않는다 — 응답 계약이
    AWAITING_APPROVAL에 제안 1개 이상을 요구해(api/incidents.py) 노출 필터는 그
    자리를 500으로 만든다. 이런 행의 실행 시도는 여기서 409로 떨어지는데, 그것은
    후보 선점 경합과 같은 코드라 FE가 이미 처리하는 경로다.
    """
    try:
        mappers.to_candidate_data(candidate)
    except ValidationError:
        logger.warning(
            "candidate_contract_invalid",
            extra={
                "candidate_id": candidate.candidate_id,
                "runbook_id": candidate.runbook_id.value,
            },
        )
        return False
    return True


@dataclass(frozen=True)
class ExecutionReservation:
    """created=True면 새 예약(202 Accepted), False면 같은 Key 재요청(200 OK)."""

    response: ExecuteActionResponse
    created: bool


def _to_response(row: models.ActionExecution) -> ExecuteActionResponse:
    return ExecuteActionResponse.model_validate(
        {
            "execution_id": row.execution_id,
            "status": row.status,
            "updated_at": row.updated_at,
        }
    )


def _replay_or_conflict(
    existing: models.ActionExecution, request: ExecuteActionRequest
) -> ExecutionReservation:
    """같은 Key가 같은 조치를 가리킬 때만 재요청이다 — 다른 조치면 409."""
    if (
        existing.incident_id != canonical_id(request.incident_id)
        or existing.runbook_id != request.runbook_id
    ):
        raise ApiError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
    return ExecutionReservation(response=_to_response(existing), created=False)


def _executable_candidate(
    db: Session, request: ExecuteActionRequest
) -> models.RunbookCandidate:
    """요청한 Runbook과 일치하는 EXECUTABLE 후보. 없으면 409로 거절한다."""
    incident_id = canonical_id(request.incident_id)
    if incident_id is None or incidents_repo.get_incident(db, incident_id) is None:
        raise ApiError(ErrorCode.INCIDENT_NOT_FOUND)

    executable = incidents_repo.list_candidates(
        db, incident_id, status=CandidateStatus.EXECUTABLE
    )
    for candidate in sorted(executable, key=lambda row: row.created_at):
        if candidate.runbook_id == request.runbook_id:
            # 계약 위반 저장분은 실행할 수 없는 제안이다 — 상태가 EXECUTABLE이어도
            # 접수하지 않는다(_candidate_meets_contract 참조)
            if not _candidate_meets_contract(candidate):
                break
            return candidate
    raise ApiError(ErrorCode.PROPOSAL_NOT_EXECUTABLE)


def _recoverable_origin(
    db: Session, request: ExecuteActionRequest
) -> models.ActionExecution:
    """요청한 롤백을 열어 준 원본 실행. 없으면 409로 거절한다.

    "열어 준다"의 기준은 상세 응답의 available_recovery_runbook_ids와 같다 —
    짝(ROLLBACK_RUNBOOK_BY_MAIN_ID)이 맞고, 원본이 복구 가능 상태이며, 아직
    복구가 접수되지 않은 실행이다. 한 원본에 두 번째 복구는 오지 않는다.
    """
    incident_id = canonical_id(request.incident_id)
    if incident_id is None or incidents_repo.get_incident(db, incident_id) is None:
        raise ApiError(ErrorCode.INCIDENT_NOT_FOUND)

    requested = request.runbook_id.value
    for row in executions_repo.list_by_incident(db, incident_id):
        if ROLLBACK_RUNBOOK_BY_MAIN_ID.get(row.runbook_id.value) != requested:
            continue
        if row.status not in EXECUTION_RECOVERABLE_STATUSES:
            continue
        # 잠근 뒤 다시 확인한다 — 동시에 들어온 다른 복구 접수가 이미 자식을
        # 남겼거나 원본 상태를 옮겼을 수 있다. 잠금은 commit까지 유지된다.
        origin = executions_repo.lock_execution(db, row.execution_id)
        if origin is None or origin.status not in EXECUTION_RECOVERABLE_STATUSES:
            continue
        if executions_repo.list_rollback_children(db, origin.execution_id):
            continue
        return origin
    raise ApiError(ErrorCode.PROPOSAL_NOT_EXECUTABLE)


def _recovery_backup_record_id(
    db: Session, origin: models.ActionExecution
) -> Optional[str]:
    """관제자 복구 접수가 자식에 결속할 백업 레코드 id — 원본 실행에서만 조회한다. (Issue #241)

    **요청은 원복 값의 출처를 나르지 않는다.** ExecuteActionRequest에 backup_record_id가
    없는 것이 계약이며(정책 ③, packages/schemas/api/actions.py), 서버가 원본 행에 결속된
    레코드를 찾아 자식에 박는다. 자동 발동(initiate_auto_rollback)이 하는 것과 같은 일을
    같은 근거로 한다 — 두 경로가 다른 출처를 쓰면 "원천이 하나"라는 정책이 접수 주체에
    따라 달라진다. 결속을 접수 시점에 하지 않으면 실행은 자기 행에서 출처를 찾지 못하고
    (run_revert_size_execution은 요청도 후보도 보지 않는다) 정상 접수된 원복이 실행
    단계에서 실패한다.

    **없다고 접수를 거절하지는 않는다.** 거절하려면 화면의 복구 목록도 같은 조건이어야
    하는데(routers/incidents._recovery_ids), 그 목록은 GET /api/v1/incidents/{id}의 공개
    계약이라 여기서 함께 바꿀 자리가 아니다. 실제로 롤백 3종의 원본은 모두 backup_action이
    있는 런북이므로, 결속이 비어 있는 원본은 운영 경로에서 나오지 않는다 — 그때의 실행
    실패("원복 근거 없음")는 오히려 사실대로다.

    남의 실행이 만든 레코드는 받지 않는다. 결속(bind_backup_record)이 실행 자신이 만든
    레코드만 걸도록 돼 있어도, 그 불변식을 읽는 쪽에서 한 번 더 확인해야 잘못된 결속이
    조용히 원복 값으로 쓰이지 않는다.
    """
    record = (
        executions_repo.get_backup_record(db, origin.backup_record_id)
        if origin.backup_record_id is not None
        else None
    )
    if record is None or record.execution_id != origin.execution_id:
        return None
    return record.backup_record_id


def _move_incident_to_in_progress(db: Session, incident_id: str) -> None:
    """접수한 실행이 진행 중인 동안 Incident는 반드시 ACTION_IN_PROGRESS다.

    상세 응답 계약(api/incidents.py)이 상태와 자식 목록의 정합을 강제하므로,
    전이를 빠뜨리면 접수 직후 조회가 500이 된다. 출발 상태를 AWAITING_APPROVAL로
    전제하지 않고 실제 상태를 잠근 뒤 옮긴다 — 종료 상태(RESOLVED·FAILED)에서
    오는 관제자 복구 접수도 정규 경로이기 때문이다. RESOLVED는 "더 진행할
    제안·실행 없음"이지 자산이 원복됐다는 뜻이 아니다 (ADR-0004, Issue #126).

    RESOLVED에서 나올 때는 종료 판단(resolution·resolved_at)을 함께 지운다. 그
    값은 "지금 이 인시던트가 종료된 이유"를 말하므로 재개되면 거짓이 되고, 남겨
    두면 DB 제약이 전이 자체를 거절한다. 지워진 판단은 복원되지 않는다 — 남는
    것은 복구 실행 레코드가 가리키는 "재개됐다"는 사실뿐이므로, 판단 이력 자체가
    필요해지면 별도 이력 모델이 맞다 (Issue #199).
    """
    incident = incidents_repo.lock_incident(db, incident_id)
    if incident is None:
        raise ApiError(ErrorCode.INCIDENT_NOT_FOUND)
    if incident.status is IncidentStatus.ACTION_IN_PROGRESS:
        # 상태는 그대로여도 상세 응답의 자식 목록이 바뀌었으므로 updated_at은 올린다
        incidents_repo.touch_incident(db, incident_id)
        return
    incidents_repo.update_incident_status(
        db,
        incident_id,
        expected=incident.status,
        next_status=IncidentStatus.ACTION_IN_PROGRESS,
        clear_resolution=incident.status is IncidentStatus.RESOLVED,
    )


def reserve_execution(
    db: Session, request: ExecuteActionRequest
) -> ExecutionReservation:
    """조치 실행 예약 — 멱등 확인 → Incident 확인 → 접수 근거 확인 → 예약 → 상태 전이.

    멱등 확인이 맨 앞이어야 한다. 재요청 시점에는 후보가 이미 CLAIMED라, 제안
    확인을 먼저 하면 정상 재요청이 200이 아니라 409로 떨어진다.

    접수 근거는 Runbook 종류가 가른다 — 본편 7종은 EXECUTABLE 후보, 롤백 3종은
    복구를 열어 준 원본 실행이다.
    """
    existing = executions_repo.get_by_idempotency_key(db, request.idempotency_key)
    if existing is not None:
        return _replay_or_conflict(existing, request)

    is_rollback = request.runbook_id.value in ROLLBACK_RUNBOOK_IDS
    try:
        source = (
            _recoverable_origin(db, request)
            if is_rollback
            else _executable_candidate(db, request)
        )
        # 되돌릴 값의 출처는 **접수 시점에** 정한다. 실행은 자기 행에 결속된 것만 읽으므로
        # (run_revert_size_execution) 여기서 박아 두지 않으면 원복이 근거를 찾지 못한다.
        backup_record_id = (
            _recovery_backup_record_id(db, source) if is_rollback else None
        )
    except ApiError as exc:
        if exc.code is ErrorCode.PROPOSAL_NOT_EXECUTABLE:
            # 최초 멱등 조회와 접수 근거 확인 사이에 같은 Key의 앞선 요청이
            # commit되면 후보는 이미 CLAIMED고 롤백은 원본에 자식이 붙어 있다 —
            # 같은 Key 재요청을 409로 오판하지 않도록 키를 한 번 더 확인한다.
            # 이 재확인은 INSERT 이전에만 둔다(이후에는 같은 세션의 자기 미커밋
            # 행이 잡혀 커밋 안 된 실행을 재생하게 된다).
            raced = executions_repo.get_by_idempotency_key(db, request.idempotency_key)
            if raced is not None:
                return _replay_or_conflict(raced, request)
        raise

    try:
        # 실행 INSERT가 후보 선점보다 먼저다 — 동시 요청의 관문은 idempotency_key
        # 유니크 제약이어야 한다. 선점을 먼저 하면 뒤엣것이 409 PROPOSAL_NOT_EXECUTABLE로
        # 떨어져 같은 Key 재요청이 200을 받지 못한다.
        # SAVEPOINT로 감싸는 이유: 제약 위반이 바깥 트랜잭션까지 되돌리면 재조회를
        # 이어서 할 수 없다.
        with db.begin_nested():
            execution = executions_repo.create_execution(
                db,
                incident_id=source.incident_id,
                runbook_id=request.runbook_id,
                target_arn=source.target_arn,
                trigger_source=TriggerSource.USER_APPROVAL,
                candidate_id=None if is_rollback else source.candidate_id,
                parent_execution_id=source.execution_id if is_rollback else None,
                # 자동 발동과 같은 자리에 같은 값을 박는다(ADR-0008 §4 보강) — 원천이
                # 하나라는 정책은 어느 레코드에서 왔는지가 자식 행에 남을 때만 검증된다
                backup_record_id=backup_record_id,
                idempotency_key=request.idempotency_key,
            )
    except IntegrityError:
        # 같은 Key의 동시 요청 — 앞선 요청이 이미 예약했다
        # (db/repositories/executions.py 헤더 규정)
        raced = executions_repo.get_by_idempotency_key(db, request.idempotency_key)
        if raced is None:
            raise  # 멱등 키가 아닌 다른 제약 위반이다 — 500으로 보고한다
        return _replay_or_conflict(raced, request)

    if not is_rollback:
        claimed = incidents_repo.update_candidate_status(
            db,
            source.candidate_id,
            expected=CandidateStatus.EXECUTABLE,
            next_status=CandidateStatus.CLAIMED,
        )
        if not claimed:
            # 조회 이후 다른 요청이 후보를 선점했다. commit하지 않으므로 예약도 남지 않는다
            raise ApiError(ErrorCode.PROPOSAL_NOT_EXECUTABLE)

    _move_incident_to_in_progress(db, source.incident_id)

    reserved = _to_response(execution)
    db.commit()
    return ExecutionReservation(response=reserved, created=True)


def resolve_incident(
    db: Session, incident_id: str, resolution: ResolutionJudgement
) -> bool:
    """관제자 종료 처리 — 상태 확인 → 잔여 제안 무효화 → RESOLVED 전이.

    True면 이번 요청이 종료를 확정한 것, False면 이미 종료돼 있던 건이다. 후자는
    처음 저장된 판단을 유지한다 — Idempotency Key 없이 재요청이 안전한 이유이고,
    나중 요청이 판단을 덮어쓰지 않는 것은 먼저 내린 판단이 기록이기 때문이다.

    출발 상태를 AWAITING_APPROVAL로 전제하지 않고 잠근 뒤 실제 상태를 본다 —
    허용 집합은 INCIDENT_RESOLVABLE_STATUSES(schemas/incidents.py)다.
    """
    incident = incidents_repo.lock_incident(db, incident_id)
    if incident is None:
        raise ApiError(ErrorCode.INCIDENT_NOT_FOUND)
    if incident.status is IncidentStatus.RESOLVED:
        return False
    if incident.status not in INCIDENT_RESOLVABLE_STATUSES:
        raise ApiError(ErrorCode.INCIDENT_NOT_RESOLVABLE)

    # 남은 제안을 함께 정리한다 — 상세 응답 계약이 RESOLVED에 빈 제안 목록을
    # 요구하므로(api/incidents.py), 두고 가면 종료 직후 조회가 500이 된다
    for candidate in incidents_repo.list_candidates(
        db, incident_id, status=CandidateStatus.EXECUTABLE
    ):
        incidents_repo.update_candidate_status(
            db,
            candidate.candidate_id,
            expected=CandidateStatus.EXECUTABLE,
            next_status=CandidateStatus.INVALIDATED,
        )

    moved = incidents_repo.resolve_incident(
        db, incident_id, expected=incident.status, resolution=resolution
    )
    if not moved:
        # 행을 잠그고 들어왔으므로 여기까지 와서 전이가 실패할 이유가 없다. 그래도
        # 통과시키지 않는다 — 제안만 무효화된 채 상태가 남는다. commit 없이 예외를
        # 던지므로 세션 정리에서 되돌아간다(여기서 rollback하지 않는 이유다)
        raise ApiError(ErrorCode.INCIDENT_NOT_RESOLVABLE)

    db.commit()
    db.refresh(incident)  # UPDATE가 바꾼 상태·판단을 응답 조립 전에 되읽는다
    return True


# --- 스펙 JSON 백업 -------------------------------------------------------------


@dataclass(frozen=True)
class BackupOutcome:
    """record가 있으면 백업이 확보된 것 — AWS 변경 호출은 그 이후에만 시작한다."""

    record: Optional[models.BackupRecord] = None
    created: bool = False           # False = 이미 결속돼 있던 백업을 그대로 쓴 것
    reason_code: Optional[PrecheckReasonCode] = None
    detail: Optional[str] = None

    @property
    def stored(self) -> bool:
        return self.record is not None


def _backup_failed(code: PrecheckReasonCode, detail: str) -> BackupOutcome:
    return BackupOutcome(reason_code=code, detail=detail)


def store_instance_spec_backup(db: Session, execution_id: str) -> BackupOutcome:
    """RIGHTSIZING 조치 직전 스펙 JSON 백업 — 캡처 → 저장 → 실행 결속 → commit.

    **AWS 변경 호출 이전에 commit까지 끝나야 한다.** 변경과 백업 기록 사이에서
    프로세스가 죽으면 인스턴스는 바뀐 채로 남고 되돌릴 값은 어디에도 없다 —
    REVERT_SIZE는 원복 타입을 백업 레코드에서만 로드하기 때문이다(ADR-0004
    롤백 공통 정책 ③). 그래서 이 함수는 호출부의 트랜잭션에 얹히지 않고
    스스로 커밋한다.

    실패를 ApiError로 던지지 않는다. 예약 이후의 실패는 HTTP 오류가 아니라
    Execution 상태로 전달하는 것이 공개 계약이다(schemas/api/errors.py) —
    호출부가 reason_code를 error_summary에 실어 실행을 FAILED로 끝낸다.

    같은 실행에 두 번 불러도 백업은 하나다. 재시도가 새 레코드를 만들면 "조치
    직전"이 아니라 "이미 바뀐 뒤"의 스펙이 원복 값이 되어, 원복이 아무것도
    되돌리지 못한다.
    """
    execution = executions_repo.lock_execution(db, execution_id)
    if execution is None:
        return _backup_failed(
            PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND, "실행 레코드를 찾을 수 없습니다"
        )
    if execution.runbook_id is not RunbookId.RUNBOOK_EC2_RIGHTSIZING:
        # 배선 오류다 — 스펙 JSON 백업을 쓰는 런북은 RIGHTSIZING 하나뿐이다
        # (schemas.backups.BackupType — ADR-0004 롤백 공통 정책 ③). 판정으로 삼키면
        # 다른 런북이 엉뚱한 백업 종류를 달고 조용히 진행된다.
        raise ValueError(
            f"스펙 JSON 백업 대상 런북이 아닙니다: {execution.runbook_id.value}"
        )
    if execution.backup_record_id is not None:
        return BackupOutcome(
            record=executions_repo.get_backup_record(db, execution.backup_record_id)
        )

    target = parse_arn(execution.target_arn)
    if target is None or target.resource_type != "instance":
        return _backup_failed(
            PrecheckReasonCode.PRECHECK_PARAM_INVALID,
            f"인스턴스 ARN이 아닙니다: {execution.target_arn}",
        )

    capture = backup.capture_instance_spec(target.resource_id, target.region)
    if not capture.captured:
        return _backup_failed(capture.reason_code, capture.detail or "")

    record = executions_repo.create_backup_record(
        db,
        execution_id=execution.execution_id,
        target_arn=execution.target_arn,
        backup_type=capture.backup_type,
        payload=capture.payload,
    )
    if not executions_repo.bind_backup_record(
        db, execution.execution_id, record.backup_record_id
    ):
        # 행을 잠그고 들어왔으므로 여기까지 오면 결속이 실패할 이유가 없다.
        # 그래도 통과시키지 않는다 — 결속되지 않은 백업은 원복이 찾지 못한다.
        db.rollback()
        return _backup_failed(
            PrecheckReasonCode.PRECHECK_INVALID_STATE, "백업 레코드 결속 실패"
        )

    db.commit()
    return BackupOutcome(record=record, created=True)


# --- 실행 (Issue #211) ---------------------------------------------------------


@dataclass(frozen=True)
class ExecutionRunOutcome:
    """실행 1건의 결과. **종료 상태는 아직 DB에 없다.**

    실행 행은 IN_PROGRESS로 남아 있고, 여기 담긴 것은 종료 확정에 필요한 재료다 —
    성공 여부, 실패 분류(reason_code), 사람이 읽을 사유(error_summary), 그리고
    자산이 어디까지 바뀌었는지 말하는 단계 결과(steps).

    확정 여부는 succeeded 하나가 아니라 **succeeded와 단계 effect 둘**이 가른다.
    dispatcher.py가 여기서 바로 FAILED로 확정하는 것은 **변경 없이 실패한 경우뿐**
    이다(모든 단계가 NOT_APPLIED이거나 단계가 없음, close_execution). 단계 effect에
    APPLIED·PARTIAL·UNKNOWN이 하나라도 있으면 자산이 바뀐 채 끝난 실행이라 확정하지
    않는다 — FAILED는 계약상 "변경 없이 실패"라(schemas/executions.py 복구 가능 상태
    주석) 확정하면 관제자 복구 목록이 닫힌다.

    **성공도 확정하지 않는다** — 기동 요청 접수는 성공의 경계가 아니고 2/2 Status
    Check 결과가 SUCCESS와 ROLLBACK_INITIATED를 가르기 때문이다
    (services/aws/rollback.py, run_rightsizing_execution 참조). 확정하지 않은 두 갈래
    모두 실행은 IN_PROGRESS로 남고 그 판정은 rollback.py 몫이다. (Issue #232)

    deferred는 세 번째 갈래다 — **판정·대조를 못 해 자산을 만지지 않았다.** 원복
    실행이 상태 대조(ADR-0008 §3-2)에 필요한 조회에 실패한 경우이며, 실패로 확정하면
    되돌릴 것이 그대로 남은 자산에 "원복 실패"가 기록된다. 단계가 없으므로 다음
    주기가 처음부터 다시 시도한다. 재시도 상한은 Issue #249다. (Issue #241)
    """

    succeeded: bool
    reason_code: Optional[PrecheckReasonCode] = None
    error_summary: Optional[str] = None
    steps: tuple[ExecutionStepResult, ...] = ()
    deferred: bool = False

    def __post_init__(self) -> None:
        if self.succeeded == (self.reason_code is not None):
            raise ValueError("실패에만 reason_code를 채웁니다")
        if self.deferred:
            if self.succeeded:
                raise ValueError("성공한 실행은 보류가 아닙니다")
            if self.steps:
                # 자산을 만졌으면 보류가 아니다 — 되돌릴 것이 남은 실패다
                raise ValueError("보류 결과에는 단계 기록이 없어야 합니다")


def _step_recorder(db: Session, execution_id: str):
    """executor가 넘기는 단계를 그 자리에서 저장하고 commit한다.

    단계마다 commit하는 이유는 프로세스가 중간에 사라져도 "어디까지 갔는가"가
    남아야 하기 때문이다 — 남지 않으면 회수(dispatcher.py)가 이미 바뀐 자산을
    바뀌지 않은 것으로 본다.
    """

    def record(step: ExecutionStepResult) -> None:
        if step.status is ExecutionStepStatus.IN_PROGRESS:
            executions_repo.add_step(db, step, execution_id=execution_id)
        elif not executions_repo.update_step_result(db, step, execution_id=execution_id):
            # AWS 호출 직전에 같은 sequence의 IN_PROGRESS 행이 반드시 먼저 저장된다.
            # 없다면 기록 순서가 깨진 것이므로 조용히 넘기지 않는다.
            logger.warning(
                "execution_step_missing",
                extra={"execution_id": execution_id, "sequence": step.sequence},
            )
        db.commit()

    return record


def _rightsizing_target_type(
    db: Session, execution: models.ActionExecution
) -> Optional[str]:
    """조치가 적용할 인스턴스 타입.

    Guardrail PASS의 불변 실행 명령(validated_command)이 채워지면 그것이 원천이다.
    아직 배선되지 않은 동안에는 후보의 typed 파라미터에서 읽는다 — 계약 검증을
    거쳐 읽으므로(#154) 옛 계약으로 저장된 행이 실행으로 새지 않는다.
    """
    command = execution.validated_command or {}
    from_command = command.get("target_instance_type")
    if isinstance(from_command, str) and from_command.strip():
        return from_command

    if execution.candidate_id is None:
        return None
    candidate = incidents_repo.get_candidate(db, execution.candidate_id)
    if candidate is None:
        return None
    try:
        data = mappers.to_candidate_data(candidate)
    except ValidationError:
        logger.warning(
            "candidate_contract_invalid",
            extra={"candidate_id": candidate.candidate_id, "runbook_id": candidate.runbook_id.value},
        )
        return None
    return getattr(data.parameters, "target_instance_type", None)


def _run_failed(
    reason_code: PrecheckReasonCode,
    error_summary: str,
    steps: tuple[ExecutionStepResult, ...] = (),
) -> ExecutionRunOutcome:
    """실패 결과 1건. 상태는 기록하지 않는다 — 확정은 dispatcher.py 몫이다."""
    return ExecutionRunOutcome(
        succeeded=False,
        reason_code=reason_code,
        error_summary=error_summary[:1024],
        steps=steps,
    )


def run_rightsizing_execution(db: Session, execution_id: str) -> ExecutionRunOutcome:
    """`RUNBOOK_EC2_RIGHTSIZING` 실행 — 백업 확보 → 정지·타입 변경·기동 → 결과 반환.

    순서가 계약이다. **스펙 JSON 백업이 commit된 뒤에만 AWS 변경이 시작된다**
    (store_instance_spec_backup) — 변경과 백업 사이에서 프로세스가 죽으면 되돌릴
    값이 어디에도 남지 않는다(ADR-0004 롤백 공통 정책 ③). 백업에 실패하면 조치를
    시작하지 않고 실패 결과로 돌아간다.

    실행 실패는 ApiError로 던지지 않는다. 예약 이후의 실패는 HTTP 오류가 아니라
    Execution 상태로 전달하는 것이 공개 계약이다(schemas/api/errors.py) —
    store_instance_spec_backup이 실패를 판정으로 돌려주는 것과 같은 이유다.

    **종료 상태도 Incident 전이도 여기서 하지 않는다.** 실행 행은 IN_PROGRESS로
    남기고 확정은 dispatcher.py가 close_execution으로 둘을 **한 트랜잭션에서**
    처리한다. 실행만 먼저 종료 상태로 옮기면 "Incident는
    ACTION_IN_PROGRESS인데 진행 중인 실행이 없는" 조합이 생기는데, 상세 응답 계약
    (api/incidents.py)이 그 조합을 거절하므로 상세 조회가 500이 된다.

    최종 상태를 아는 자리가 여기가 아니기도 하다 — 기동 요청 접수는 성공의 경계가
    아니고, 2/2 Status Check 결과가 SUCCESS와 ROLLBACK_INITIATED를 가른다
    (services/aws/rollback.py).
    """
    execution = executions_repo.get_execution(db, execution_id)
    if execution is None:
        raise ValueError(f"실행 레코드를 찾을 수 없습니다: {execution_id}")
    if execution.runbook_id is not RunbookId.RUNBOOK_EC2_RIGHTSIZING:
        # 배선 오류다 — 런북마다 단계와 백업 종류가 다르다
        raise ValueError(f"RIGHTSIZING 실행이 아닙니다: {execution.runbook_id.value}")
    if execution.status is not ExecutionStatus.IN_PROGRESS:
        # 끝난 실행을 다시 돌리면 백업 없는 두 번째 변경이 된다. 동시 접수를 막는
        # 선점(lock_execution)은 호출부(dispatcher.py) 몫이라 여기서는 상태만 본다.
        raise ValueError(f"진행 중인 실행이 아닙니다: {execution.status.value}")

    target_arn = execution.target_arn
    target_instance_type = _rightsizing_target_type(db, execution)
    if target_instance_type is None:
        return _run_failed(
            PrecheckReasonCode.PRECHECK_PARAM_INVALID,
            "실행 파라미터에서 target_instance_type을 찾지 못했습니다",
        )

    stored = store_instance_spec_backup(db, execution_id)
    if not stored.stored:
        return _run_failed(
            stored.reason_code, f"스펙 JSON 백업 실패: {stored.detail or ''}".strip()
        )

    outcome = executor.execute_rightsizing(
        target_arn,
        target_instance_type=target_instance_type,
        record_step=_step_recorder(db, execution_id),
    )
    if not outcome.succeeded:
        return _run_failed(outcome.reason_code, outcome.error_summary, outcome.steps)
    return ExecutionRunOutcome(succeeded=True, steps=outcome.steps)


# --- 2/2 Status Check 판정 (Issue #240) -----------------------------------------


@dataclass(frozen=True)
class ExecutionJudgement:
    """종료 판정 1건 — 확정할 목적 상태와 사유. **상태는 아직 DB에 없다.**

    "성공의 경계"는 런북마다 다르다. RIGHTSIZING은 2/2 Status Check가 가르고
    (judge_rightsizing_boot), REVERT_SIZE는 실자산 타입이 백업 값으로 돌아왔는지가
    가른다(judge_revert_size) — 그래서 이 타입은 판정의 종류가 아니라 **판정 결과의
    모양**만 정한다.

    run_rightsizing_execution이 ExecutionRunOutcome를 돌려주는 것과 같은 자리다.
    확정은 close_execution 하나가 하고, 언제 부를지는 dispatcher.py가 고른다.

    next_status가 None이면 **판정 보류**다 — 확정하지 않고 IN_PROGRESS로 남겨 다음
    주기가 다시 묻는다. AWS에 물어보지 못해 자산 상태를 본 적이 없는 경우이며, 그때
    ROLLBACK_INITIATED로 닫으면 검증기의 실패가 자산의 실패로 저장되어 자동 원복
    (#241)의 입력과 구분되지 않는다 (PR #244 리뷰).

    **보류 사유는 저장하지 않는다**(defer_reason은 로그 몫이다). 판정 불가를 어떤
    typed 상태로 남기고 재시도를 몇 번까지 허용할지가 Issue #249의 계약이며, 그
    계약이 서기 전에 error_summary 문자열이 판정 근거로 읽히면 안 된다.
    """

    next_status: Optional[ExecutionStatus] = None
    error_summary: Optional[str] = None
    verdict: Optional[rollback.StatusCheckVerdict] = None
    defer_reason: Optional[str] = None

    @property
    def deferred(self) -> bool:
        return self.next_status is None

    def __post_init__(self) -> None:
        if self.deferred:
            if self.error_summary is not None:
                raise ValueError("보류 판정은 error_summary를 저장하지 않습니다")
            if self.defer_reason is None:
                raise ValueError("보류 판정에는 사유가 필요합니다")
            return
        if self.defer_reason is not None:
            raise ValueError("확정 판정에는 defer_reason을 두지 않습니다")
        if (self.next_status is ExecutionStatus.SUCCESS) != (self.error_summary is None):
            raise ValueError("성공이 아닌 판정에만 error_summary를 채웁니다")


def _steps_changed_the_asset(steps: list[models.ExecutionStep]) -> bool:
    """단계 기록이 "자산이 만져졌을 수 있다"고 말하는가.

    IN_PROGRESS로 남은 단계는 AWS 호출 결과를 받지 못한 것이라 effect가 없다 —
    그 호출이 적용됐는지 알 수 없으므로 바뀌었을 수 있다는 쪽으로 센다.
    """
    return any(
        step.status is ExecutionStepStatus.IN_PROGRESS
        or step.effect in ASSET_MAY_HAVE_CHANGED_EFFECTS
        for step in steps
    )


def _boot_failure_summary(outcome: rollback.StatusCheckOutcome) -> str:
    """판정 사유 한 줄 — 분류를 앞에 둬 로그·DB에서 사유별로 모인다(보류 사유도 같은 꼴)."""
    code = (
        outcome.reason_code.value
        if outcome.reason_code is not None
        else outcome.verdict.value
    )
    return f"{code}: {outcome.summary}"[:1024]


def judge_rightsizing_boot(db: Session, execution_id: str) -> ExecutionJudgement:
    """AWS 변경이 시작된 RIGHTSIZING 실행 1건의 종료 판정. (Issue #240)

    **기동 요청 접수는 성공의 경계가 아니다.** 2/2 Status Check가 SUCCESS와
    ROLLBACK_INITIATED를 가르므로, 단계가 남은 채 IN_PROGRESS인 실행은 재실행이
    아니라 이 판정으로 온다(dispatcher.py 회수 규약).

    판정 순서가 계약이다.
      ① 끝나지 않은·실패한 단계가 있으면 부팅을 물을 필요가 없다 — 조치 자체가
         제 갈 데까지 가지 못했다. 자산이 바뀌었을 수 있으면 원복이 남고
         (ROLLBACK_INITIATED), 확실히 안 바뀌었으면 그냥 실패다(FAILED).
      ② 조치 직전 stopped였던 인스턴스는 기동하지 않는다(executor.execute_rightsizing
         ③단계 NOT_APPLIED). 켜지 않은 인스턴스에 2/2를 물으면 영원히 오지 않으므로
         타임아웃 뒤 멀쩡한 자산을 되돌리게 된다 — 그 전에 성공으로 확정한다.
      ③ 그 밖에는 waiter에 묻는다 — 2/2면 성공이고, 관측된 실패·타임아웃이면 원복이
         남는다. **AWS에 물어보지 못했으면 어느 쪽도 아니라 보류다**(next_status가
         없는 판정) — 검증기의 실패를 자산의 실패로 저장하면 자동 원복이 멀쩡한
         인스턴스를 되돌린다 (Issue #249).

    **종료 상태도 Incident 전이도 여기서 하지 않는다** — run_rightsizing_execution과
    같은 이유다(확정은 close_execution 한 트랜잭션).
    """
    execution = executions_repo.get_execution(db, execution_id)
    if execution is None:
        raise ValueError(f"실행 레코드를 찾을 수 없습니다: {execution_id}")
    if execution.runbook_id is not RunbookId.RUNBOOK_EC2_RIGHTSIZING:
        # 배선 오류다 — 런북마다 성공의 경계가 다르다
        raise ValueError(f"RIGHTSIZING 실행이 아닙니다: {execution.runbook_id.value}")
    if execution.status is not ExecutionStatus.IN_PROGRESS:
        raise ValueError(f"진행 중인 실행이 아닙니다: {execution.status.value}")

    steps = executions_repo.list_steps(db, execution_id)
    if not steps:
        # 단계가 없으면 자산이 아직 안 만져진 것이라 재실행 대상이다(dispatcher._claim).
        # 여기로 왔다면 회수 분기가 어긋난 것이므로 판정으로 삼키지 않는다
        raise ValueError(f"단계 기록이 없는 실행입니다: {execution_id}")

    unsettled = [
        step
        for step in steps
        if step.status is not ExecutionStepStatus.SUCCESS
    ]
    if unsettled:
        detail = (unsettled[-1].error_summary or "").strip() or (
            f"단계 {unsettled[-1].sequence}({unsettled[-1].step_type})가 끝나지 않았습니다"
        )
        if _steps_changed_the_asset(steps):
            return ExecutionJudgement(
                next_status=ExecutionStatus.ROLLBACK_INITIATED,
                error_summary=f"조치 미완(자산 변경됨): {detail}"[:1024],
            )
        return ExecutionJudgement(
            next_status=ExecutionStatus.FAILED,
            error_summary=f"조치 미완(자산 변경 없음): {detail}"[:1024],
        )

    started = next(
        (
            step
            for step in reversed(steps)
            if step.step_type == executor.STEP_START_INSTANCE
        ),
        None,
    )
    if started is None:
        # 타입 변경까지 갔는데 기동 단계가 없다 — 자산은 바뀐 채 멈춰 있다
        return ExecutionJudgement(
            next_status=ExecutionStatus.ROLLBACK_INITIATED,
            error_summary="조치 미완: 기동 단계가 기록되지 않았습니다",
        )
    if started.effect is ExecutionEffect.NOT_APPLIED:
        return ExecutionJudgement(next_status=ExecutionStatus.SUCCESS)

    outcome = rollback.wait_for_status_check(execution.target_arn)
    if outcome.booted:
        return ExecutionJudgement(
            next_status=ExecutionStatus.SUCCESS, verdict=outcome.verdict
        )
    if outcome.probe_failed:
        # AWS에 물어보지 못해 결론이 없다 — 자산이 실패했다는 근거가 아니다.
        # 여기서 ROLLBACK_INITIATED로 닫으면 검증기의 실패가 자산의 실패로 저장되고,
        # 자동 원복(#241)이 멀쩡한 인스턴스를 되돌린다 (PR #244 리뷰 / Issue #249)
        return ExecutionJudgement(
            defer_reason=_boot_failure_summary(outcome), verdict=outcome.verdict
        )
    return ExecutionJudgement(
        next_status=ExecutionStatus.ROLLBACK_INITIATED,
        error_summary=_boot_failure_summary(outcome),
        verdict=outcome.verdict,
    )


# --- 실행 종료 확정 (Issue #232) -------------------------------------------------


@dataclass(frozen=True)
class ExecutionClosure:
    """종료 확정 1건의 결과 — commit 이후 발행에 쓸 재료다.

    발행은 여기서 하지 않는다. 이 계층이 앱 상태(RealtimeManager)를 알게 되면
    업무 흐름이 전송 채널에 묶이므로, 라우터와 같은 경계를 쓴다 — commit 이후
    발행은 호출부 몫이다(routers/incidents.py 헤더).
    """

    incident_id: str
    execution_id: str
    execution_status: ExecutionStatus
    execution_updated_at: datetime
    incident_status: IncidentStatus
    incident_updated_at: datetime


def _incident_status_after(
    db: Session,
    incident_id: str,
    *,
    closed_execution_id: str,
    closed_status: ExecutionStatus,
    also_closed: tuple[str, ...] = (),
) -> IncidentStatus:
    """실행 하나가 확정된 뒤 Incident가 있어야 할 상태.

    목적 상태는 실행의 성패가 아니라 **그 인시던트에 남은 것**이 정한다. 상세 응답
    계약(api/incidents.py)이 상태와 자식 목록의 정합을 강제하기 때문이다 —
    ACTION_IN_PROGRESS는 진행 중 실행 1개 이상을, AWAITING_APPROVAL은 제안 1개
    이상과 진행 중 실행 없음을, FAILED는 빈 제안 목록을 요구한다. 성패로 정하면
    "실패했는데 다른 제안이 남은" 건이 그 계약을 깬다.

    남은 것이 없을 때만 **방금 확정된 실행의 결과**가 목적 상태를 가른다. 조치가 제
    갈 데까지 갔으면(EXECUTION_SETTLED_STATUSES) 관제자 종료 판단만 남은 것이므로
    AWAITING_CLOSURE, 그렇지 않으면 흐름을 더 진행할 수 없으므로 FAILED다. 성공을
    FAILED로 접으면 계약은 통과하지만 화면이 성공한 조치를 '진행 불가'로 그린다
    (PR #236 리뷰 §2-②가 남긴 미정 분기 — Issue #240에서 확정).

    also_closed는 **같은 트랜잭션에서 함께 종료 상태로 옮긴 다른 실행**이다 — 원복
    자식이 끝나면 원본도 같이 확정되므로(close_execution), 그 원본을 세션이 든 옛
    상태(ROLLBACK_INITIATED)로 다시 세면 영원히 "진행 중 실행이 있다"가 된다.

    RESOLVED로는 옮기지 않는다. DB 제약은 판단 없는 RESOLVED를 허용하지만
    (db/models.py resolution_with_resolved_status), 시스템이 먼저 옮기면 관제자 종료
    API가 멱등 경로로 떨어져(resolve_incident) 종료 판단이 영구히 비어 있는 채로
    남는다 (Issue #199). ANALYZING도 AI 분석 미완을 뜻해 실행이 끝난 뒤 갈 자리가
    아니다.
    """
    settled_now = {closed_execution_id, *(also_closed or ())}
    still_running = closed_status in EXECUTION_NON_TERMINAL_STATUSES or any(
        row.status in EXECUTION_NON_TERMINAL_STATUSES
        # 방금 옮긴 행은 세션이 옛 상태를 들고 있을 수 있어 인자로 받은 값을 쓴다
        for row in executions_repo.list_by_incident(db, incident_id)
        if row.execution_id not in settled_now
    )
    if still_running:
        return IncidentStatus.ACTION_IN_PROGRESS
    if incidents_repo.list_candidates(
        db, incident_id, status=CandidateStatus.EXECUTABLE
    ):
        return IncidentStatus.AWAITING_APPROVAL
    if closed_status in EXECUTION_SETTLED_STATUSES:
        return IncidentStatus.AWAITING_CLOSURE
    return IncidentStatus.FAILED


# 자식 실행의 결과가 원본에 남기는 확정 상태. 자식이 갖는 종료 상태는 DB CheckConstraint
# (rollback_child_status)가 SUCCESS·FAILED로 묶으므로 표도 두 줄이다.
_ORIGIN_STATUS_AFTER_ROLLBACK: dict[ExecutionStatus, ExecutionStatus] = {
    ExecutionStatus.SUCCESS: ExecutionStatus.ROLLED_BACK,
    ExecutionStatus.FAILED: ExecutionStatus.ROLLBACK_FAILED,
}


def _lock_rollback_origin(
    db: Session, execution: models.ActionExecution
) -> Optional[models.ActionExecution]:
    """이 실행이 되돌리려는 원본. 롤백 자식이 아니면 None.

    자식을 잠근 뒤에 잠근다 — 잠금 방향을 자식 → 원본으로 고정하지 않으면 관제자
    복구 접수(_recoverable_origin은 원본만 잠근다)와 엇갈릴 수 있다.
    """
    if execution.parent_execution_id is None:
        return None
    origin = executions_repo.lock_execution(db, execution.parent_execution_id)
    if origin is None:
        raise ValueError(
            f"롤백 자식이 가리키는 원본이 없습니다: {execution.parent_execution_id}"
        )
    return origin


def _settle_rollback_origin(
    db: Session,
    origin: Optional[models.ActionExecution],
    *,
    child_status: ExecutionStatus,
) -> tuple[str, ...]:
    """원복 결과를 원본에 확정한다. 함께 옮긴 실행 ID를 돌려준다.

    원본이 이미 종료 상태면 옮기지 않는다 — 관제자가 두 번째 복구를 접수할 수 없는
    구조라(_recoverable_origin) 정상 경로에서는 오지 않지만, 확정이 두 번 오면
    나중 것이 먼저 내린 판단을 덮어쓴다.

    자식이 아직 비종료면 원본도 그대로 둔다. "되돌리는 중"인 원본의 상태가
    ROLLBACK_INITIATED이며, 그것이 곧 자동 원복이 개시됐다는 기록이다.
    """
    if origin is None:
        return ()
    next_status = _ORIGIN_STATUS_AFTER_ROLLBACK.get(child_status)
    if next_status is None:
        return ()
    if origin.status not in EXECUTION_RECOVERABLE_STATUSES:
        # 복구가 열려 있던 상태(SUCCESS·ROLLBACK_INITIATED)에서만 옮긴다. 이미
        # ROLLED_BACK·ROLLBACK_FAILED로 확정된 원본의 판단을 덮어쓰지 않는다.
        return ()
    if not executions_repo.update_execution_status(
        db,
        origin.execution_id,
        expected=origin.status,
        next_status=next_status,
        # 성공으로 끝났던 원본의 종료 시각은 그대로 둔다 — 그 조치가 끝난 시각이고,
        # 되돌린 시각은 자식 실행 행이 갖는다
        finished_at=(
            None if origin.finished_at is not None else datetime.now(timezone.utc)
        ),
    ):
        raise ValueError(f"원본 실행 상태 전이 실패: {origin.execution_id}")
    logger.info(
        "rollback_origin_settled",
        extra={
            "execution_id": origin.execution_id,
            "next_status": next_status.value,
            "child_status": child_status.value,
        },
    )
    return (origin.execution_id,)


def close_execution(
    db: Session,
    execution_id: str,
    *,
    next_status: ExecutionStatus,
    error_summary: Optional[str] = None,
) -> Optional[ExecutionClosure]:
    """실행 상태 확정과 Incident 전이를 **한 트랜잭션**으로 커밋한다. (Issue #232)

    나눠 커밋하면 그 사이의 조회가 "ACTION_IN_PROGRESS인데 진행 중인 실행이 없는"
    인시던트를 보게 되고, 상세 응답 계약(api/incidents.py)이 그 조합을 거절해 상세
    조회가 500이 된다.

    None은 실패가 아니라 **이미 다른 주체가 확정한 실행**이라는 뜻이다 — 잠근 뒤
    상태를 다시 보므로 여기까지 두 번 들어와도 확정은 한 번이다.

    **롤백 자식이 끝나면 원본도 같은 트랜잭션에서 확정한다** — 자식 SUCCESS면 원본은
    ROLLED_BACK, 자식 FAILED면 ROLLBACK_FAILED다(Issue #241). 나눠 커밋하면 그 사이의
    조회가 "되돌리기는 끝났는데 원본은 아직 원복 중"인 인시던트를 보고, 상세 응답의
    자식 목록과 상태가 어긋난다. 자동 발동이든 관제자 요청이든 같다 — 확정의 근거는
    발동 주체가 아니라 자식의 결과다.

    잠금 순서는 실행(자식 → 원본) → Incident로 고정한다(reserve_execution과 같은
    방향). 엇갈리면 두 경로가 서로를 기다린다.
    """
    execution = executions_repo.lock_execution(db, execution_id)
    if execution is None:
        raise ValueError(f"실행 레코드를 찾을 수 없습니다: {execution_id}")
    if execution.status not in EXECUTION_NON_TERMINAL_STATUSES:
        return None

    origin = _lock_rollback_origin(db, execution)
    incident_id = execution.incident_id
    incident = incidents_repo.lock_incident(db, incident_id)
    if incident is None:
        raise ValueError(f"실행이 가리키는 인시던트가 없습니다: {incident_id}")

    moved = executions_repo.update_execution_status(
        db,
        execution_id,
        expected=execution.status,
        next_status=next_status,
        error_summary=error_summary,
        finished_at=(
            datetime.now(timezone.utc)
            if next_status in EXECUTION_TERMINAL_STATUSES
            else None
        ),
    )
    if not moved:
        # 행을 잠그고 들어왔으므로 여기까지 와서 실패할 이유가 없다. 그래도
        # 통과시키지 않는다 — 실행은 그대로인데 Incident만 옮겨 가면 상세 응답이
        # 깨진다. commit 없이 예외를 던지므로 세션 정리에서 되돌아간다
        raise ValueError(f"실행 상태 전이 실패: {execution_id}")

    settled_origin = _settle_rollback_origin(db, origin, child_status=next_status)
    target = _incident_status_after(
        db,
        incident_id,
        closed_execution_id=execution_id,
        closed_status=next_status,
        also_closed=settled_origin,
    )
    status_changed = incident.status is not target
    if status_changed:
        if not incidents_repo.update_incident_status(
            db,
            incident_id,
            expected=incident.status,
            next_status=target,
            clear_resolution=incident.status is IncidentStatus.RESOLVED,
        ):
            raise ValueError(f"Incident 상태 전이 실패: {incident_id}")
    else:
        # 상태는 그대로여도 상세 응답의 자식 목록이 바뀌었으므로 updated_at은 올린다
        incidents_repo.touch_incident(db, incident_id)

    db.commit()
    # Core UPDATE는 세션이 든 객체를 갱신하지 않는다 — 발행에 실을 상태·시각을
    # 되읽는다(resolve_incident와 같은 자리)
    db.refresh(execution)
    db.refresh(incident)
    return ExecutionClosure(
        incident_id=incident_id,
        execution_id=execution_id,
        execution_status=execution.status,
        execution_updated_at=execution.updated_at,
        incident_status=incident.status,
        incident_updated_at=incident.updated_at,
    )


# --- 자동 원복 (Issue #241) -------------------------------------------------------
#
# 자산 트랙의 마지막 칸이다. 2/2 Status Check가 실패·타임아웃으로 갈린 실행은
# ROLLBACK_INITIATED로 남는데(judge_rightsizing_boot), 그 상태는 "되돌려야 한다"는
# 표시일 뿐 되돌리는 주체가 아니었다. 여기가 그 주체다.
#
# 경로를 접수와 실행으로 가르는 것은 본편과 같다(reserve_execution ↔
# run_rightsizing_execution). 접수(initiate_auto_rollback)는 자식 실행 행을 남기는
# 데까지고, 가드레일 4단계와 AWS 변경은 실행(run_revert_size_execution)이 한다.
# 그렇게 나누는 이유가 **멱등**이다 — 자식 행의 존재 자체가 "이 원본의 자동 원복은
# 이미 시작됐다"는 관문이라(list_rollback_children), 가드레일이 거절해 자식이
# FAILED로 끝나도 다음 주기가 다시 발동하지 않는다(ADR-0004 정책 ④ 무재시도).


class _DbBackupRecordLoader:
    """executor.BackupRecordLoader의 DB 구현 — 세션 1개를 감싼 읽기 전용 조회.

    executor가 백업 조회를 주입받는 이유는 그쪽이 DB를 모르기 때문이고(ADR-0007 §1),
    그래서 배선은 이 계층 몫이다. 미배선은 FAIL이 아니라 RuntimeError라, 백업이
    필요한 런북의 precheck를 부르는 자리는 반드시 이것을 넘겨야 한다.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def get(self, backup_record_id: str) -> Optional[executor.BackupRecordView]:
        record = executions_repo.get_backup_record(self._db, backup_record_id)
        if record is None:
            return None
        return executor.BackupRecordView(
            backup_record_id=record.backup_record_id,
            target_arn=record.target_arn,
            backup_type=record.backup_type,
            payload=record.payload or {},
        )

    def latest_for_target(
        self,
        target_arn: str,
        backup_type: str,
        payload_match: Optional[dict] = None,
    ) -> Optional[executor.BackupRecordView]:
        # backup_record_id를 파라미터로 받지 않는 런북은 NACL_RESTORE 하나이고
        # (executor.RUNBOOK_SPECS), 그 실행 경로는 아직 없다. 지금 조용히 None을
        # 돌려주면 "백업 레코드 없음" 거절이 되어 미구현이 판정으로 둔갑한다.
        raise NotImplementedError(
            "대상 기준 백업 조회는 NACL 실행 경로와 함께 붙인다 (ADR-0008 §Consequences)"
        )


@dataclass(frozen=True)
class RollbackInitiation:
    """자동 원복 접수 1건의 결과.

    execution_id가 있으면 이번 호출이 자식을 만들었다는 뜻이다. skipped_reason은
    만들지 않은 이유이며, closure가 함께 있으면 그 자리에서 원본을 ROLLBACK_FAILED로
    확정했다는 뜻이다 — 되돌릴 근거가 없어 자동 원복을 시작조차 못 한 경우다.
    호출부는 그 closure로 발행한다(commit 이후 발행은 호출부 몫, realtime.py 규약).
    """

    execution_id: Optional[str] = None
    skipped_reason: Optional[str] = None
    closure: Optional["ExecutionClosure"] = None


def _rollback_evidence_ids(
    db: Session, origin: models.ActionExecution
) -> list[str]:
    """원복 명령이 실을 근거 ID. 원본 조치가 선 근거를 그대로 잇는다.

    원복은 후보가 아니라 근거를 새로 만들지 않는다(ADR-0004 정책 ②). 그래서 "왜
    되돌리는가"의 좌표는 원본 조치가 딛고 선 근거이며, 없으면 인시던트에 고정된
    근거에서 가장 먼저 수집된 것을 쓴다.
    """
    if origin.candidate_id is not None:
        candidate = incidents_repo.get_candidate(db, origin.candidate_id)
        if candidate is not None:
            try:
                return list(mappers.to_candidate_data(candidate).evidence_ids)
            except ValidationError:
                logger.warning(
                    "candidate_contract_invalid",
                    extra={"candidate_id": origin.candidate_id},
                )
    fixed = incidents_repo.list_evidence(db, origin.incident_id)
    return [row.evidence_id for row in fixed[:1]]


def initiate_auto_rollback(db: Session, origin_execution_id: str) -> RollbackInitiation:
    """ROLLBACK_INITIATED 원본에 자동 원복 자식을 접수한다. (Issue #241)

    **원본당 1회다.** 관문은 자식의 존재이며(list_rollback_children), 관제자 복구
    접수가 쓰는 것과 같은 관문이다(_recoverable_origin) — 두 경로가 같은 원본에
    두 개의 원복을 만들지 않는다.

    되돌릴 근거가 없으면 자식을 만들지 않고 원본을 ROLLBACK_FAILED로 확정한다.
    백업 레코드가 없다는 것은 원복 값이 어디에도 없다는 뜻이라(ADR-0008 §1 ④
    "백업이 없으면 원복을 시작하지 않는다" — 현물 조회로 값을 추정하지 않는다),
    다시 시도해도 답이 같다. ROLLBACK_INITIATED로 남겨 두면 매 주기 같은 자리에서
    실패하면서 인시던트가 영원히 진행 중으로 남는다.

    **가드레일은 여기서 부르지 않는다.** 접수는 실행 행을 남기는 데까지이고, 4단계
    통과는 AWS 변경 직전인 run_revert_size_execution이 한다 — 그 순서라야 자식 행이
    멱등 관문 노릇을 해서 거절된 원복이 다음 주기에 다시 발동하지 않는다.
    """
    origin = executions_repo.lock_execution(db, origin_execution_id)
    if origin is None:
        raise ValueError(f"실행 레코드를 찾을 수 없습니다: {origin_execution_id}")
    if origin.status is not ExecutionStatus.ROLLBACK_INITIATED:
        return RollbackInitiation(skipped_reason="자동 원복 대상 상태가 아닙니다")
    if executions_repo.list_rollback_children(db, origin.execution_id):
        return RollbackInitiation(skipped_reason="복구가 이미 접수돼 있습니다")

    rollback_id = ROLLBACK_RUNBOOK_BY_MAIN_ID.get(origin.runbook_id.value)
    if rollback_id is None:
        # 등록 롤백이 없는 런북이다 — 미구현을 실패로 바꾸지 않는 규약대로 남긴다
        return RollbackInitiation(skipped_reason="등록된 롤백 런북이 없습니다")

    record = (
        executions_repo.get_backup_record(db, origin.backup_record_id)
        if origin.backup_record_id is not None
        else None
    )
    if record is None:
        return _abandon_auto_rollback(
            db, origin, detail="자동 원복 불가: 백업 레코드가 없어 되돌릴 값이 없습니다"
        )
    evidence_ids = _rollback_evidence_ids(db, origin)
    if not evidence_ids:
        return _abandon_auto_rollback(
            db, origin, detail="자동 원복 불가: 원복 명령에 실을 근거 ID가 없습니다"
        )

    child = executions_repo.create_execution(
        db,
        incident_id=origin.incident_id,
        runbook_id=RunbookId(rollback_id),
        target_arn=origin.target_arn,
        trigger_source=TriggerSource.AUTO_ON_FAILURE,
        parent_execution_id=origin.execution_id,
        # 실제로 로드한 백업을 자기 행에 결속한다(ADR-0008 §4 보강) — 원천이 하나라는
        # 정책은 어느 레코드에서 왔는지가 기록에 남을 때만 사후에 검증된다
        backup_record_id=record.backup_record_id,
    )
    db.commit()
    logger.info(
        "auto_rollback_initiated",
        extra={
            "execution_id": child.execution_id,
            "parent_execution_id": origin.execution_id,
            "runbook_id": rollback_id,
        },
    )
    return RollbackInitiation(execution_id=child.execution_id)


def _abandon_auto_rollback(
    db: Session, origin: models.ActionExecution, *, detail: str
) -> RollbackInitiation:
    """자동 원복을 시작조차 못 한다 — 원본을 ROLLBACK_FAILED로 확정하고 CRITICAL.

    Incident 전이까지 close_execution에 맡긴다. 실행만 옮기면 "ACTION_IN_PROGRESS인데
    진행 중 실행이 없는" 조합이 생겨 상세 조회가 500이 된다.
    """
    logger.critical(
        "auto_rollback_abandoned",
        extra={"execution_id": origin.execution_id, "detail": detail},
    )
    closure = close_execution(
        db,
        origin.execution_id,
        next_status=ExecutionStatus.ROLLBACK_FAILED,
        error_summary=detail[:1024],
    )
    return RollbackInitiation(skipped_reason=detail, closure=closure)


def _revert_command_payload(
    execution: models.ActionExecution,
    record: models.BackupRecord,
    evidence_ids: list[str],
    instance_id: str,
) -> dict:
    """가드레일 ①에 넘길 원복 실행 명령.

    **원복 값은 여기 실리지 않는다.** 되돌릴 타입은 백업 레코드가 갖고 있고 명령이
    나르는 것은 그 레코드를 가리키는 backup_record_id뿐이다 — 파라미터에 원복 값이
    실려 오면 요청 페이로드가 제2의 원천이 된다(ADR-0008 §4). ④ precheck가
    backup_record_id로 레코드를 다시 읽어 종류·대상까지 대조한다.
    """
    return {
        "runbook_id": execution.runbook_id.value,
        "target_arn": execution.target_arn,
        "parameters": {
            "instance_id": instance_id,
            "backup_record_id": record.backup_record_id,
            "evidence_id": evidence_ids[0],
        },
        "evidence_ids": evidence_ids,
    }


def _run_rollback_guardrails(
    db: Session, execution: models.ActionExecution, command_payload: dict
) -> guardrails.GuardrailOutcome:
    """원복 실행 명령을 4단계 가드레일에 통과시키고 그 판정을 저장한다.

    롤백도 본편과 동일하게 네 단계를 전부 지난다(ADR-0004 정책 ①). 시스템이 시작한
    실행이라 payload에 LLM 저작 문자열이 없더라도 우회하지 않는다 — "AWS를 건드리는
    모든 실행은 가드레일을 통과한다"가 예외를 갖는 순간 그 문장을 근거로 쓸 수 없다.

    판정 결과는 저장한다. 거절이 로그로만 남으면 관제 화면에서 "왜 원복이 멈췄는가"를
    답할 자리가 없다.
    """
    request = GuardrailValidationRequest(
        validation_context=GuardrailValidationContext.ROLLBACK_EXECUTION,
        execution_id=execution.execution_id,
        command_payload=command_payload,
    )
    loader = _DbBackupRecordLoader(db)
    outcome = guardrails.run_guardrail_validation(
        request,
        is_managed_arn=lambda arn: assets_repo.get_asset_by_arn(db, arn) is not None,
        # 롤백 3종은 전부 백업 레코드를 읽는다 — 미배선이면 FAIL이 아니라 RuntimeError다
        precheck=lambda command: executor.precheck(
            command.runbook_id,
            command.target_arn,
            command.parameters,
            backup_loader=loader,
        ),
    )
    guardrails_repo.add_evaluation(
        db,
        validation_context=GuardrailValidationContext.ROLLBACK_EXECUTION,
        result=outcome.result,
        execution_id=execution.execution_id,
        validated_command=command_payload if outcome.command is not None else None,
    )
    db.commit()
    return outcome


def _guardrail_rejection_summary(result: GuardrailValidationResult) -> str:
    """거절 한 줄 — 어느 단계에서 어떤 사유로 막혔는지를 앞에 세운다."""
    failed = next((s for s in result.steps if s.step == result.failed_step), None)
    code = (
        failed.reason_code.value
        if failed is not None and failed.reason_code is not None
        else "UNKNOWN"
    )
    step = result.failed_step.value if result.failed_step is not None else "UNKNOWN"
    return f"가드레일 거절({step}/{code})"


def run_revert_size_execution(db: Session, execution_id: str) -> ExecutionRunOutcome:
    """`RUNBOOK_EC2_REVERT_SIZE` 실행 — 백업 로드 → 가드레일 4단계 → 원복. (Issue #241)

    순서가 계약이다. **가드레일 4단계가 AWS 변경보다 먼저 끝난다**(ADR-0004 정책 ①).
    거절이면 자산을 만지지 않고 실패로 돌아가며 **자동 재시도는 없다**(정책 ④) —
    자식 실행 행이 이미 있어 다음 주기의 발동이 멱등 관문에 걸리기 때문이다
    (initiate_auto_rollback). 남는 처분은 CRITICAL 알림과 수동 개입이다.

    **원복 값은 백업 레코드에서만 온다**(ADR-0004 정책 ③). 이 함수가 읽는 것은 자기
    행에 결속된 backup_record_id 하나이고 요청 페이로드도 후보도 보지 않는다. 원본
    실행에서 읽는 값은 하나뿐인데, 그것은 되돌릴 값이 아니라 제3자 변경을 가리기 위한
    대조 축이다(ADR-0008 §3-2).

    종료 상태도 Incident 전이도 여기서 하지 않는다 — run_rightsizing_execution과 같은
    이유다(확정은 close_execution 한 트랜잭션).
    """
    execution = executions_repo.get_execution(db, execution_id)
    if execution is None:
        raise ValueError(f"실행 레코드를 찾을 수 없습니다: {execution_id}")
    if execution.runbook_id is not RunbookId.RUNBOOK_EC2_REVERT_SIZE:
        # 배선 오류다 — 런북마다 단계와 되돌릴 축이 다르다
        raise ValueError(f"REVERT_SIZE 실행이 아닙니다: {execution.runbook_id.value}")
    if execution.status is not ExecutionStatus.IN_PROGRESS:
        raise ValueError(f"진행 중인 실행이 아닙니다: {execution.status.value}")
    if execution.parent_execution_id is None:
        # 원복은 언제나 되돌릴 원본을 가리킨다 — 원본 없이는 대조 축을 알 수 없다
        raise ValueError(f"원본을 가리키지 않는 원복 실행입니다: {execution_id}")

    target = parse_arn(execution.target_arn)
    if target is None or target.resource_type != "instance":
        return _run_failed(
            PrecheckReasonCode.PRECHECK_PARAM_INVALID,
            f"인스턴스 ARN이 아닙니다: {execution.target_arn}",
        )

    record = (
        executions_repo.get_backup_record(db, execution.backup_record_id)
        if execution.backup_record_id is not None
        else None
    )
    if record is None:
        return _run_failed(
            PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND,
            "원복 근거 없음: 결속된 백업 레코드를 찾을 수 없습니다",
        )

    origin = executions_repo.get_execution(db, execution.parent_execution_id)
    if origin is None:
        raise ValueError(f"원본 실행이 없습니다: {execution.parent_execution_id}")
    applied_type = _rightsizing_target_type(db, origin)
    if applied_type is None:
        # 원본이 무엇으로 바꿨는지 모르면 §3-2의 ②와 ③을 가를 수 없다. 모르는 채로
        # 되돌리면 제3자 변경을 덮어쓸 수 있으므로 시작하지 않는다.
        return _run_failed(
            PrecheckReasonCode.PRECHECK_PARAM_INVALID,
            "상태 대조 불가: 원본 조치가 적용한 instance_type을 알 수 없습니다",
        )

    evidence_ids = _rollback_evidence_ids(db, origin)
    if not evidence_ids:
        return _run_failed(
            PrecheckReasonCode.PRECHECK_PARAM_INVALID,
            "원복 명령에 실을 근거 ID가 없습니다",
        )

    payload = _revert_command_payload(
        execution, record, evidence_ids, target.resource_id
    )
    guardrail = _run_rollback_guardrails(db, execution, payload)
    if guardrail.command is None:
        detail = _guardrail_rejection_summary(guardrail.result)
        logger.critical(
            "auto_rollback_guardrail_rejected",
            extra={
                "execution_id": execution_id,
                "parent_execution_id": origin.execution_id,
                "failed_step": (
                    guardrail.result.failed_step.value
                    if guardrail.result.failed_step is not None
                    else None
                ),
            },
        )
        return _run_failed(PrecheckReasonCode.PRECHECK_INVALID_STATE, detail)

    spec = InstanceSpecBackup.model_validate(record.payload or {})
    outcome = executor.execute_revert_size(
        execution.target_arn,
        restore_instance_type=spec.instance_type,
        applied_instance_type=applied_type,
        restore_state=spec.state,
        record_step=_step_recorder(db, execution_id),
    )
    if outcome.deferred:
        return ExecutionRunOutcome(
            succeeded=False,
            reason_code=outcome.reason_code,
            error_summary=outcome.error_summary,
            deferred=True,
        )
    if not outcome.succeeded:
        return _run_failed(outcome.reason_code, outcome.error_summary, outcome.steps)
    return ExecutionRunOutcome(succeeded=True, steps=outcome.steps)


# 기동 요청이 접수된 뒤의 EC2 state. **원복 성공의 경계는 기동 "요청"이지 2/2 Status
# Check가 아니다**(ADR-0008 §6) — 되돌린 인스턴스가 또 부팅에 실패해도 원복의 원복은
# 없어 판정이 바뀌지 않는다. 그래서 pending도 성공에 넣는다: 빼면 방금 켠 인스턴스가
# 다음 주기에 "미완"으로 확정된다.
_REVERT_STARTED_STATES: frozenset[str] = frozenset({"running", "pending"})


def judge_revert_size(db: Session, execution_id: str) -> ExecutionJudgement:
    """단계를 남긴 채 IN_PROGRESS인 원복 실행 1건의 종료 판정. (Issue #241, ADR-0008 §6)

    여기로 오는 것은 **실행 도중 프로세스가 끊긴 원복**뿐이다. 정상 경로는 실행이
    끝난 그 주기에 dispatcher가 확정한다. 자동 재시도를 하지 않는다는 것이 종료
    판정을 하지 않는다는 뜻은 아니므로(ADR-0008 §6) 판정 주체를 runner와 짝으로 둔다 —
    짝이 어긋나면 중단된 자식이 재실행도 종료도 되지 않고 IN_PROGRESS에 남는다.

    **성공의 경계는 실자산이다.** 원복이 끝났는지는 단계 기록이 아니라 지금 인스턴스
    타입이 백업 값인지가 답한다 — 끊긴 지점이 어디든 그 답은 같다. 2/2 Status Check는
    여기서 묻지 않는다: 부팅이 또 실패해도 원복의 원복은 없어 판정을 바꾸지 못한다.

    조회하지 못하면 확정하지 않고 보류한다 — judge_rightsizing_boot이 probe_failed를
    다루는 것과 같은 이유이며, 재시도 상한은 Issue #249다.
    """
    execution = executions_repo.get_execution(db, execution_id)
    if execution is None:
        raise ValueError(f"실행 레코드를 찾을 수 없습니다: {execution_id}")
    if execution.runbook_id is not RunbookId.RUNBOOK_EC2_REVERT_SIZE:
        raise ValueError(f"REVERT_SIZE 실행이 아닙니다: {execution.runbook_id.value}")
    if execution.status is not ExecutionStatus.IN_PROGRESS:
        raise ValueError(f"진행 중인 실행이 아닙니다: {execution.status.value}")

    record = (
        executions_repo.get_backup_record(db, execution.backup_record_id)
        if execution.backup_record_id is not None
        else None
    )
    if record is None:
        return ExecutionJudgement(
            next_status=ExecutionStatus.FAILED,
            error_summary="원복 판정 불가: 결속된 백업 레코드를 찾을 수 없습니다",
        )
    target = parse_arn(execution.target_arn)
    if target is None or target.resource_type != "instance":
        return ExecutionJudgement(
            next_status=ExecutionStatus.FAILED,
            error_summary=f"인스턴스 ARN이 아닙니다: {execution.target_arn}",
        )

    spec = InstanceSpecBackup.model_validate(record.payload or {})
    restored = spec.instance_type
    current, state, code = executor.current_instance_type_and_state(
        target.resource_id, target.region
    )
    if code is not None and code is not PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND:
        # 자산 상태를 본 적이 없다 — 확정하면 검증기의 실패가 원복의 실패로 저장된다
        return ExecutionJudgement(
            defer_reason=f"{code.value}: 원복 대상 상태 조회 실패로 판정 보류"
        )
    if current == restored:
        # 타입은 되돌아왔다. 그것만으로 성공이라 하면 **정지 → 타입 원복 → [중단]**
        # 으로 끊긴 원복이 성공으로 확정된다 — 실행 절차의 마지막 칸이 기동이므로
        # (executor.execute_revert_size의 STEP_START_INSTANCE) 그 앞에서 끊기면
        # 인스턴스는 멈춘 채 남는다. 자식이 SUCCESS면 원본까지 ROLLED_BACK으로 닫혀
        # 인시던트가 내려가고, 멈춘 자산을 다시 볼 자리가 사라진다.
        #
        # 되돌려야 할 상태는 백업 레코드의 state다(ADR-0008 §4) — 조치 이전에 멈춰
        # 있던 인스턴스는 멈춰 있는 것이 원복의 완료다.
        if spec.state != "running" or state in _REVERT_STARTED_STATES:
            return ExecutionJudgement(next_status=ExecutionStatus.SUCCESS)
        detail = (
            f"원복 미완 — 타입은 {restored}로 되돌렸으나 조치 이전 running이던"
            f" 인스턴스가 {state or '알 수 없음'} 상태입니다."
            " 자동 재시도 없이 수동 개입으로 전환합니다"
        )
        logger.critical(
            "revert_size_not_restarted",
            extra={
                "execution_id": execution_id,
                "instance_state": state,
                "restore_state": spec.state,
                "restore_instance_type": restored,
            },
        )
        return ExecutionJudgement(
            next_status=ExecutionStatus.FAILED, error_summary=detail[:1024]
        )

    detail = (
        f"원복 미완 — 현재 {current or '알 수 없음'}, 백업 {restored}."
        " 자동 재시도 없이 수동 개입으로 전환합니다"
    )
    logger.critical(
        "revert_size_incomplete",
        extra={
            "execution_id": execution_id,
            "current_instance_type": current,
            "restore_instance_type": restored,
        },
    )
    return ExecutionJudgement(
        next_status=ExecutionStatus.FAILED, error_summary=detail[:1024]
    )


# --- AI 분석 결과 저장·전이 (Issue #285) ------------------------------------------
#
# agent_dispatcher가 그래프를 부르고 계약 검증까지 마친 출력 1건을 여기서 저장한다.
# 그쪽에 두지 않은 것은, 이 계층이 상태 전이·트랜잭션 경계를 소유하고(파일 헤더)
# **가드레일을 부르고 그 판정을 저장하는 자리가 이미 여기이기 때문이다**
# (_run_rollback_guardrails). 배선이 두 곳으로 갈리면 같은 4단계가 두 규약을 갖는다.
# dispatcher.py가 실행 전이를 이 계층에 내리는 것과 같은 경계다.
#
# **순서가 계약이다. 가드레일이 저장보다 먼저다.**
#   1. 관리 자산 조회(읽기) → 읽기 트랜잭션 종료
#   2. 후보마다 가드레일 4단계 1회 — **트랜잭션 밖**
#   3. 한 트랜잭션에 저장 — 후보(최종 상태)·판정·Terminal 기록·Incident 전이 → commit
#
# 2번이 저장보다 앞서는 이유 둘. 어느 쪽도 순서를 바꾸면 성립하지 않는다.
#   - **저장할 수 없는 값을 저장하지 않는다.** ① Schema Check가 거절하는 값에는 NUL처럼
#     PostgreSQL이 담지 못하는 문자가 있다(ai/guardrails.py _reject_nul). 저장을 먼저 하면
#     그 값이 INSERT에서 DataError로 터져 거절이 기록되는 대신 예외가 나고, 그 Incident는
#     ANALYZING·IN_PROGRESS에 남아 회수를 거쳐 같은 출력을 다시 받는다. 출력 단위의
#     같은 제약은 그래프 호출 직후에도 한 번 본다(agent_dispatcher.py 5번 ⓒ).
#   - **AWS 호출 중 트랜잭션을 열어 두지 않는다.** ④ AWS Dry-Run은 실제 AWS 호출이라
#     응답·재시도 시간만큼 커넥션이 트랜잭션에 묶인다. 모델 호출을 트랜잭션 밖으로 뺀
#     것과 같은 이유다(agent_dispatcher.py 4번).
# ③ ARN Match가 쓰는 자산 조회도 그래서 미리 해석해 집합으로 넘긴다 — 경계가
# ManagedAssetLookup Protocol이라 구현을 바꿔 끼우는 것이고, 판정 기준(수집된 자산인가)은
# 그대로다.
#
# **요약 3줄은 AWAITING_APPROVAL로 갈 때만 쓴다.** 조회 계약(api/incidents.py
# _enforce_contract)이 ANALYZING·FAILED에 빈 summary_lines를 요구하므로, 실패로 닫는
# 건에 요약을 남기면 그 Incident의 상세 조회가 500이 된다. 쓰지 않는 요약은 로그로만
# 남긴다(Issue #285) — 후보가 왜 0개였는지 진단할 근거가 그것뿐이다.
#
# **Incident 행을 잠그지 않는다.** 배타 보장은 AI 호출 선점(IN_PROGRESS)이 이미 갖고
# 있고(agent_dispatcher.py 2번), 잠그면 저장 트랜잭션이 여는 시간만큼 행이 잠긴다.
# 전이는 expected 조건부 UPDATE라 잠금 없이도 덮어쓰기가 생기지 않는다.


@dataclass
class AgentAnalysisOutcome:
    """그래프 출력 1건의 저장 결과 — 발행과 스캔 집계를 정하는 호출부가 읽는 값이다."""

    incident_id: str
    next_status: IncidentStatus  # AWAITING_APPROVAL | FAILED
    executable: int
    rejected: int
    occurred_at: datetime  # 저장된 Incident.updated_at — WS 봉투의 occurred_at


def _candidate_command_payload(candidate: RunbookCandidateData) -> dict:
    """저장 전 후보 → 가드레일 ① Schema Check가 받는 경계 JSON.

    display_parameters는 싣지 않는다 — 서버가 parameters에서 파생하는 화면 표시본이라
    검증 대상이 아니고, SchemaCheckedCommand가 추가 필드를 거절한다.
    """
    return {
        "runbook_id": candidate.runbook_id.value,
        "target_arn": candidate.target_arn,
        "parameters": candidate.parameters.model_dump(mode="json"),
        "evidence_ids": list(candidate.evidence_ids),
    }


def _precheck_param_invalid(detail: str) -> PrecheckOutcome:
    """④에 넘길 실행 파라미터를 조립하지 못했다 — AWS를 부르지 않은 거절이다."""
    logger.warning("candidate_precheck_params_failed", extra={"detail": detail[:256]})
    return PrecheckOutcome(
        passed=False,
        reason_code=PrecheckReasonCode.PRECHECK_PARAM_INVALID,
        verification_summary=build_verification_summary(
            VerificationMethod.DESCRIBE,
            verified=["없음(실행 파라미터 조립 실패)"],
            unverified=["AWS 대상 상태", "IAM 권한"],
        ),
    )


def _candidate_precheck(command) -> PrecheckOutcome:
    """④ AWS Dry-Run 경계 — 후보 파라미터를 실행 파라미터로 옮겨 executor에 넘긴다.

    조회로 채우는 값은 여기서 AWS에 물어 온다. **Detection 스냅샷의 값을 쓰지 않는다** —
    ④는 "지금 이 조치가 나가는가"를 보는 단계이고 스냅샷은 Incident 생성 근거라 보장의
    종류가 다르다(agent_dispatcher.py 불변식 ⓑ · schemas/intake.py 계약 원칙).

    지금 배선하는 조회는 RIGHTSIZING의 current_instance_type 하나다. 나머지 FinOps
    후보(ENABLE_AUTOSCALING·EBS_DELETE_UNATTACHED·SG_DELETE_ISOLATED)는 실행 파라미터가
    후보 값과 대상 자원 ID만으로 서고, 격리·NACL 계열은 메뉴에 오르지 않는다
    (ai/capabilities.py 축 ②). 조회 실패는 배선 오류가 아니라 AWS 판정이라 예외가 아니라
    FAIL로 돌려준다(ADR-0007 §1 — 예외로 막는 것은 배선 오류뿐이다).
    """
    target = parse_arn(command.target_arn)
    if target is None:
        # ③ ARN Match가 수집된 자산만 통과시키므로 방어적 경로다
        return _precheck_param_invalid(f"target_arn 해석 실패: {command.target_arn}")

    lookups: dict = {}
    if command.runbook_id is RunbookId.RUNBOOK_EC2_RIGHTSIZING:
        current, code = executor.current_instance_type(target.resource_id, target.region)
        if code is not None:
            return PrecheckOutcome(
                passed=False,
                reason_code=code,
                verification_summary=build_verification_summary(
                    VerificationMethod.DESCRIBE,
                    operations=["describe_instances"],
                    verified=["없음(현재 인스턴스 타입 조회 실패)"],
                    unverified=["AWS 대상 상태", "IAM 권한"],
                ),
            )
        lookups["current_instance_type"] = current

    try:
        parameters = build_precheck_parameters(
            command.runbook_id,
            command.parameters,
            resource_id=target.resource_id,
            evidence_ids=command.evidence_ids,
            **lookups,
        )
    except (ValidationError, ValueError) as exc:
        return _precheck_param_invalid(f"{type(exc).__name__}: {exc}")

    return executor.precheck(command.runbook_id, command.target_arn, parameters)


def _draft_candidates(
    incident_id: str, output: AgentGraphOutput
) -> list[RunbookCandidateData]:
    """그래프 출력의 후보 초안에 서버 식별자와 초기 상태를 붙인다. 저장은 아직 하지 않는다."""
    return [
        RunbookCandidateData(
            candidate_id=str(uuid.uuid4()),
            incident_id=incident_id,
            runbook_id=draft.runbook_id,
            target_arn=draft.target_arn,
            parameters=draft.parameters,
            evidence_ids=list(draft.evidence_ids),
            status=CandidateStatus.PENDING_VALIDATION,
        )
        for draft in output.candidates
    ]


def _managed_arns(db: Session, candidates: list[RunbookCandidateData]) -> set[str]:
    """③ ARN Match가 볼 대상 중 수집된 자산인 것. 가드레일 전에 미리 해석한다."""
    return {
        arn
        for arn in {candidate.target_arn for candidate in candidates}
        if assets_repo.get_asset_by_arn(db, arn) is not None
    }


def _guard_candidate(
    candidate: RunbookCandidateData, managed: set[str]
) -> guardrails.GuardrailOutcome:
    """후보 1건에 가드레일 4단계를 1회 수행한다. **DB를 만지지 않는다.**"""
    return guardrails.run_guardrail_validation(
        GuardrailValidationRequest(
            validation_context=GuardrailValidationContext.AI_CANDIDATE,
            candidate_id=candidate.candidate_id,
            command_payload=_candidate_command_payload(candidate),
        ),
        is_managed_arn=lambda arn: arn in managed,
        precheck=_candidate_precheck,
    )


def _store_candidate(
    db: Session,
    candidate: RunbookCandidateData,
    outcome: guardrails.GuardrailOutcome,
) -> bool:
    """가드레일을 마친 후보 1건과 그 판정을 저장한다. PASS면 True.

    판정 결과를 저장한다 — 거절이 로그로만 남으면 관제 화면에서 "왜 이 제안이 사라졌는가"를
    답할 자리가 없다(_run_rollback_guardrails와 같은 이유). 실행 시점에는 다시 부르지
    않는다(파일 헤더 · Issue #113 §2).

    후보를 PENDING_VALIDATION으로 넣었다가 옮기지 않고 **최종 상태로 한 번에 넣는다** —
    같은 트랜잭션 안에서 두 번 쓰는 것이고, 중간 상태를 볼 수 있는 조회자가 없다.
    """
    passed = outcome.result.result is GuardrailDecision.PASS
    payload = _candidate_command_payload(candidate)
    stored = candidate.model_copy(
        update={
            "status": (
                CandidateStatus.EXECUTABLE if passed else CandidateStatus.REJECTED
            )
        }
    )
    incidents_repo.add_candidate(db, stored)
    guardrails_repo.add_evaluation(
        db,
        validation_context=GuardrailValidationContext.AI_CANDIDATE,
        result=outcome.result,
        candidate_id=candidate.candidate_id,
        validated_command=payload if outcome.command is not None else None,
    )
    return passed


def _log_dropped_summary(incident_id: str, output: AgentGraphOutput) -> None:
    """저장하지 않는 요약을 로그로 남긴다 — 후보가 0개인 이유를 볼 자리가 여기뿐이다.

    줄마다 자른다. 모델이 길이를 스스로 정하는 문자열이라 상한이 없으면 로그 한 줄이
    무한정 길어진다(ai/guardrails.py의 위반 항목 절단과 같은 이유).
    """
    if not output.summary_lines:
        return
    logger.info(
        "agent_summary_dropped",
        extra={
            "incident_id": incident_id,
            "invocation_status": output.invocation_status.value,
            "summary_lines": [line[:256] for line in output.summary_lines],
        },
    )


def record_agent_analysis(
    db: Session, incident_id: str, output: AgentGraphOutput
) -> AgentAnalysisOutcome:
    """그래프 출력 1건 → 가드레일 1회 + 후보 저장 + ANALYZING 이탈. 순서는 파일 절 참조.

    **NO_PROPOSAL과 "후보 전부 REJECTED"는 분석 실패다.** 둘 다 관제자에게 보여줄 조치가
    0개인데 AWAITING_APPROVAL은 실행 가능한 제안 1개 이상을 요구한다(api/incidents.py
    _enforce_contract). 새 상태를 만들지 않고 FAILED로 닫되 agent_invocation_status는
    그래프가 낸 Terminal 값을 그대로 남겨 그래프 오류(FAILED)와 구분한다 — 결함 계측과
    감사가 그 둘을 갈라 봐야 한다(Issue #237 도피 비율).

    출력이 FAILED면 후보도 요약도 없으므로(계약 불변식) 곧바로 Incident를 FAILED로 옮긴다.
    """
    candidates = _draft_candidates(incident_id, output)
    managed = _managed_arns(db, candidates) if candidates else set()
    # 가드레일 ④가 AWS를 부르는 동안 트랜잭션을 열어 두지 않는다
    db.rollback()

    guarded = [(candidate, _guard_candidate(candidate, managed)) for candidate in candidates]

    executable = sum(
        _store_candidate(db, candidate, outcome) for candidate, outcome in guarded
    )
    rejected = len(guarded) - executable

    target = IncidentStatus.AWAITING_APPROVAL if executable else IncidentStatus.FAILED
    if target is not IncidentStatus.AWAITING_APPROVAL:
        _log_dropped_summary(incident_id, output)
    if not incidents_repo.finish_agent_invocation(
        db,
        incident_id,
        output.invocation_status,
        # FAILED로 닫는 건은 빈 요약을 유지한다(조회 계약)
        summary_lines=(
            list(output.summary_lines)
            if target is IncidentStatus.AWAITING_APPROVAL
            else None
        ),
    ):
        raise ValueError(f"AI 호출 종료 전이 실패: {incident_id}")
    if not incidents_repo.update_incident_status(
        db, incident_id, expected=IncidentStatus.ANALYZING, next_status=target
    ):
        # ANALYZING은 관제자 종료 처리의 출발 상태가 아니라(INCIDENT_RESOLVABLE_STATUSES)
        # 분석 중에 상태가 옮겨 갈 경로가 없다. commit 없이 던져 세션 정리에서 되돌린다
        raise ValueError(f"Incident 상태 전이 실패: {incident_id}")

    db.commit()
    incident = incidents_repo.get_incident(db, incident_id)
    # Core UPDATE는 세션이 든 객체를 갱신하지 않는다 — 발행에 실을 시각을 되읽는다
    db.refresh(incident)
    logger.info(
        "agent_analysis_recorded",
        extra={
            "incident_id": incident_id,
            "invocation_status": output.invocation_status.value,
            "incident_status": target.value,
            "executable": executable,
            "rejected": rejected,
        },
    )
    return AgentAnalysisOutcome(
        incident_id=incident_id,
        next_status=target,
        executable=executable,
        rejected=rejected,
        occurred_at=incident.updated_at,
    )
