# ==============================================================================
# [파일 설명]
# POST /api/v1/actions/execute — 원클릭 조치 실행 라우터입니다. Idempotency Key로
# 중복 실행을 막습니다. (Issue #116)
#
#   - 요청·응답은 공개 계약 schemas.api.actions로만 직렬화한다.
#   - 처리 순서·상태 전이·트랜잭션은 workflows.reserve_execution이 소유한다 —
#     라우터는 DTO 전달과 상태 코드 선택만 한다.
#   - 4단계 가드레일을 여기서 다시 부르지 않는다. 가드레일은 AI 제안 생성 직후
#     1회 수행되고, 통과한 제안이 EXECUTABLE이 된다 (Issue #113 §2).
#   - 라우터는 예약(IN_PROGRESS)까지만 한다. AWS 실행은 dispatcher.py가 예약된
#     실행을 집어 services/aws/executor.py로 넘긴다(Issue #232).
#   - 접근 보호(#492 PR 2): Origin · 세션은 포함 시점(main)에, 역할(ADMIN · OPERATOR)은
#     여기서 검사한다. 거절되면 reserve_execution 에 닿지 않는다 — 실행 행 · 후보 전이가 없다.
#   - 테넌트 범위: 요청자의 범위(auth.access.tenant_scope_of)를 reserve_execution 에 넘긴다. 범위 밖
#     404 · 대상 테넌트 정지 409 와 그 판정 순서는 Workflow 가 정한다.
# ==============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from schemas.api.actions import ExecuteActionRequest, ExecuteActionResponse

import workflows
from auth.access import require_action_role, tenant_scope_of
from auth.service import CurrentUser
from db.session import get_db

router = APIRouter(prefix="/api/v1", tags=["actions"])


@router.post(
    "/actions/execute",
    response_model=ExecuteActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def execute_action(
    payload: ExecuteActionRequest,
    response: Response,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_action_role),  # 요청자 신원 — 감사 기록(T-2)이 쓴다
) -> ExecuteActionResponse:
    """신규 예약은 202, 같은 Key 재요청은 200 — 응답 본문은 두 경우가 같다."""
    reservation = workflows.reserve_execution(db, payload, scope=tenant_scope_of(user))
    if not reservation.created:
        response.status_code = status.HTTP_200_OK
    return reservation.response
