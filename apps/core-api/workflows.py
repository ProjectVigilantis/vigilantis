# ==============================================================================
# [파일 설명]
# 업무 흐름 계층 — Router와 Repository 사이에서 처리 순서·상태 전이·트랜잭션
# 경계를 소유한다(3층 분리: Router → Workflow → Repository). (Issue #116)
#
#   - commit은 여기서만 한다. Repository는 commit하지 않는다.
#   - 4단계 가드레일을 실행 시점에 다시 부르지 않는다. 가드레일은 AI 제안 생성
#     직후 1회 수행되고 통과한 제안이 EXECUTABLE이 되므로, 여기서는 그 상태를
#     확인만 한다 (Issue #113 §2, packages/schemas/candidates.py). 단 저장된 행이
#     현행 후보 계약(typed parameters, #154)대로인지는 접수 시점에 재확인한다 —
#     가드레일 재실행이 아니라 저장소 무결성 확인이다.
#   - AWS 실행은 아직 스텁이다 — 예약 레코드를 IN_PROGRESS로 남기는 데까지다.
#     실행 직전 대상 자산 재확인과 후보 INVALIDATED 전이는 실제 실행이 붙을 때
#     함께 들어온다.
#   - 조치 직전 스펙 JSON 백업은 여기서 커밋한다(store_instance_spec_backup).
#     캡처는 services/aws/backup.py가, 저장·결속·커밋 순서는 이 계층이 소유한다 —
#     "AWS 변경보다 먼저 커밋"이 트랜잭션 경계의 문제이기 때문이다.
#   - 접수 경로는 둘이다. 본편 7종은 Guardrail PASS 후보(EXECUTABLE)에서, 롤백
#     3종은 복구를 열어 준 원본 실행에서 접수한다 — 롤백은 후보가 될 수 없다
#     (ADR-0004 정책 ②, packages/schemas/candidates.py). (Issue #126)
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from schemas.api.actions import ExecuteActionRequest, ExecuteActionResponse
from schemas.api.errors import ErrorCode
from schemas.api.incidents import IncidentStatus
from schemas.candidates import CandidateStatus
from schemas.executions import EXECUTION_RECOVERABLE_STATUSES
from schemas.precheck import PrecheckReasonCode
from schemas.runbooks import (
    ROLLBACK_RUNBOOK_BY_MAIN_ID,
    ROLLBACK_RUNBOOK_IDS,
    RunbookId,
    TriggerSource,
)

from db import mappers, models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from exceptions import ApiError
from identifiers import canonical_id
from services.aws import backup
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


def _move_incident_to_in_progress(db: Session, incident_id: str) -> None:
    """접수한 실행이 진행 중인 동안 Incident는 반드시 ACTION_IN_PROGRESS다.

    상세 응답 계약(api/incidents.py)이 상태와 자식 목록의 정합을 강제하므로,
    전이를 빠뜨리면 접수 직후 조회가 500이 된다. 출발 상태를 AWAITING_APPROVAL로
    전제하지 않고 실제 상태를 잠근 뒤 옮긴다 — 종료 상태(RESOLVED·FAILED)에서
    오는 관제자 복구 접수도 정규 경로이기 때문이다. RESOLVED는 "더 진행할
    제안·실행 없음"이지 자산이 원복됐다는 뜻이 아니다 (ADR-0004, Issue #126).
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
        # (런북 명세서 safety_and_rollback.backup_action). 판정으로 삼키면
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
