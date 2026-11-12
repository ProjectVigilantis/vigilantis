# ==============================================================================
# [파일 설명]  담당: 김승철 (BE)
# 권한 거절(403 AUTH_FORBIDDEN)의 플랫폼 활동 로그 — ACCESS_FORBIDDEN (#589 B).
#
#   - 공통 오류 처리기(exceptions.handle_api_error)가 403 AUTH_FORBIDDEN 을 봉투로 바꾸기 전에 한 번 부른다.
#     그래서 관문(auth.access.require_*)의 거절과 서비스가 잠금 안에서 다시 판정한 거절이 같은 자리로 모인다.
#   - 행위자는 세션 검사를 통과한 요청자(request.state.current_user)다. 신원이 없는 403(출처 거절 등)은 남기지 않는다.
#   - 요청 트랜잭션과 따로 남긴다(platform_event_log.record_detached) — 거절된 요청은 쓴 것이 없거나 되돌려졌다.
#     DB 쓰기 실패는 앱 로그로만 남고 403 은 그대로 나간다.
#   - 싣는 것은 메서드와 경로뿐이다. 질의 문자열 · 본문은 싣지 않는다. 경로는 요청이 부른 실제 경로다(어느 고객사 ·
#     사용자 · 인시던트를 열려 했는지가 남는다). 그 경로가 기록 형식(공백 없음 · 512자)에 맞지 않으면 라우트 틀
#     (`/api/v1/tenants/{tenant_id}`)을 싣고, 그것도 없으면 남기지 않는다.
#   - 고객사(TENANT) 계정의 거절은 그 계정의 고객사를 소속으로 싣는다 — 담당자의 조회 범위에 들어간다.
#   - 범위 밖 404(ACCESS_OUT_OF_SCOPE)는 여기서 남기지 않는다. 404 가 「없다」인지 「범위 밖」인지는 그 조회를 한
#     자리만 안다.
# ==============================================================================

from __future__ import annotations

import logging
from typing import Optional

import platform_event_log
from db.repositories import platform_events as platform_events_repo
from db.session import get_session_factory
from schemas.api.errors import ErrorCode
from schemas.platform_events import AccessDetails, PlatformEventAction, PlatformEventOutcome
from starlette.requests import Request

_logger = logging.getLogger("vigilantis.platform_events")


def _details(request: Request) -> Optional[dict]:
    """기록 형식에 맞는 (메서드 · 경로). 실제 경로 → 라우트 틀 순으로 맞는 것을 고른다. 없으면 None."""
    route_path = getattr(request.scope.get("route"), "path", None)
    for path in (request.url.path, route_path):
        if not path:
            continue
        values = {
            "reason_code": ErrorCode.AUTH_FORBIDDEN.value,
            "method": request.method,
            "path": path,
        }
        try:
            AccessDetails.model_validate(values)
        except ValueError:
            continue
        return values
    return None


def record_forbidden(request: Request) -> None:
    """403 AUTH_FORBIDDEN 한 건을 남긴다. 신원이 없거나 형식에 맞는 경로가 없으면 남기지 않는다."""
    user = getattr(request.state, "current_user", None)
    if user is None:
        return
    details = _details(request)
    if details is None:
        _logger.warning("access_denial_not_recorded: no recordable path")
        return
    factory = request.app.dependency_overrides.get(get_session_factory, get_session_factory)()
    platform_event_log.record_detached(
        factory,
        PlatformEventAction.ACCESS_FORBIDDEN,
        platform_events_repo.person(user),
        outcome=PlatformEventOutcome.DENIED,
        tenant_id=user.tenant_id,
        details=details,
    )
