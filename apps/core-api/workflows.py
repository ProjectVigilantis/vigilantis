# ==============================================================================
# [파일 설명]
# 업무 흐름 계층 — Router와 Repository 사이에서 처리 순서·상태 전이·트랜잭션
# 경계를 소유한다(3층 분리: Router → Workflow → Repository). (Issue #116)
#
#   - commit은 여기서만 한다. Repository는 commit하지 않는다.
#   - 4단계 가드레일을 실행 시점에 다시 부르지 않는다. 가드레일은 AI 제안 생성
#     직후 1회 수행되고 통과한 제안이 EXECUTABLE이 되므로, 여기서는 그 상태를
#     확인만 한다 (Issue #113 §2, packages/schemas/candidates.py).
#   - AWS 실행은 아직 스텁이다 — 예약 레코드를 IN_PROGRESS로 남기는 데까지다.
#     실행 직전 대상 자산 재확인과 후보 INVALIDATED 전이는 실제 실행이 붙을 때
#     함께 들어온다.
# ==============================================================================

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from schemas.api.actions import ExecuteActionRequest, ExecuteActionResponse
from schemas.api.errors import ErrorCode
from schemas.candidates import CandidateStatus
from schemas.runbooks import TriggerSource

from db import models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from exceptions import ApiError


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


def _canonical_incident_id(value: str) -> Optional[str]:
    """계약상 incident_id는 자유 문자열이지만 저장 컬럼은 uuid다 — 저장 값과 같은
    정규형(소문자·하이픈)으로 맞춰 조회·재요청 비교가 표기 차이에 흔들리지 않게
    한다. 형식이 어긋난 값을 그대로 조회하면 캐스트 오류로 500이 된다."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _replay_or_conflict(
    existing: models.ActionExecution, request: ExecuteActionRequest
) -> ExecutionReservation:
    """같은 Key가 같은 조치를 가리킬 때만 재요청이다 — 다른 조치면 409."""
    if (
        existing.incident_id != _canonical_incident_id(request.incident_id)
        or existing.runbook_id != request.runbook_id
    ):
        raise ApiError(ErrorCode.IDEMPOTENCY_KEY_CONFLICT)
    return ExecutionReservation(response=_to_response(existing), created=False)


def _executable_candidate(
    db: Session, request: ExecuteActionRequest
) -> models.RunbookCandidate:
    """요청한 Runbook과 일치하는 EXECUTABLE 후보. 없으면 409로 거절한다."""
    incident_id = _canonical_incident_id(request.incident_id)
    if incident_id is None or incidents_repo.get_incident(db, incident_id) is None:
        raise ApiError(ErrorCode.INCIDENT_NOT_FOUND)

    executable = incidents_repo.list_candidates(
        db, incident_id, status=CandidateStatus.EXECUTABLE
    )
    for candidate in sorted(executable, key=lambda row: row.created_at):
        if candidate.runbook_id == request.runbook_id:
            return candidate
    raise ApiError(ErrorCode.PROPOSAL_NOT_EXECUTABLE)


def reserve_execution(
    db: Session, request: ExecuteActionRequest
) -> ExecutionReservation:
    """조치 실행 예약 — 멱등 확인 → Incident 확인 → 제안 확인 → 예약 → 후보 선점.

    멱등 확인이 맨 앞이어야 한다. 재요청 시점에는 후보가 이미 CLAIMED라, 제안
    확인을 먼저 하면 정상 재요청이 200이 아니라 409로 떨어진다.
    """
    existing = executions_repo.get_by_idempotency_key(db, request.idempotency_key)
    if existing is not None:
        return _replay_or_conflict(existing, request)

    try:
        candidate = _executable_candidate(db, request)
    except ApiError as exc:
        if exc.code is ErrorCode.PROPOSAL_NOT_EXECUTABLE:
            # 최초 멱등 조회와 후보 확인 사이에 같은 Key의 앞선 요청이 commit되면
            # 후보는 이미 CLAIMED다 — 같은 Key 재요청을 409로 오판하지 않도록
            # 키를 한 번 더 확인한다. 이 재확인은 INSERT 이전에만 둔다(이후에는
            # 같은 세션의 자기 미커밋 행이 잡혀 커밋 안 된 실행을 재생하게 된다).
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
                incident_id=candidate.incident_id,
                runbook_id=candidate.runbook_id,
                target_arn=candidate.target_arn,
                trigger_source=TriggerSource.USER_APPROVAL,
                candidate_id=candidate.candidate_id,
                idempotency_key=request.idempotency_key,
            )
    except IntegrityError:
        # 같은 Key의 동시 요청 — 앞선 요청이 이미 예약했다
        # (db/repositories/executions.py 헤더 규정)
        raced = executions_repo.get_by_idempotency_key(db, request.idempotency_key)
        if raced is None:
            raise  # 멱등 키가 아닌 다른 제약 위반이다 — 500으로 보고한다
        return _replay_or_conflict(raced, request)

    claimed = incidents_repo.update_candidate_status(
        db,
        candidate.candidate_id,
        expected=CandidateStatus.EXECUTABLE,
        next_status=CandidateStatus.CLAIMED,
    )
    if not claimed:
        # 조회 이후 다른 요청이 후보를 선점했다. commit하지 않으므로 예약도 남지 않는다
        raise ApiError(ErrorCode.PROPOSAL_NOT_EXECUTABLE)

    # 상세 응답에 포함되는 자식 상태(후보 CLAIMED·실행 추가)가 바뀌었다 —
    # 부모 updated_at을 함께 올린다(incidents_repo.touch_incident 관례)
    incidents_repo.touch_incident(db, candidate.incident_id)

    reserved = _to_response(execution)
    db.commit()
    return ExecutionReservation(response=reserved, created=True)
