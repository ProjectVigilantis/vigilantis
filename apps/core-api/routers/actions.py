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
#   - AWS 실행은 아직 스텁이다 — 예약 레코드만 IN_PROGRESS로 남는다.
# ==============================================================================

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from schemas.api.actions import ExecuteActionRequest, ExecuteActionResponse

import workflows
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
) -> ExecuteActionResponse:
    """신규 예약은 202, 같은 Key 재요청은 200 — 응답 본문은 두 경우가 같다."""
    reservation = workflows.reserve_execution(db, payload)
    if not reservation.created:
        response.status_code = status.HTTP_200_OK
    return reservation.response
