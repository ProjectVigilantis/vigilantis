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
#   - 범위 밖 404(ACCESS_OUT_OF_SCOPE)는 응답 코드만으로는 가를 수 없다 — 「없다」와 「범위 밖」이 같은 404 다.
#     그 조회를 한 자리가 exceptions.OutOfScopeError 로 표시한 것만 남긴다. 싣는 칸은 403 과 같고(메서드 · 경로 ·
#     요청자의 고객사), 사유 코드는 나간 응답 코드(INCIDENT_NOT_FOUND 등)다. 대상의 고객사는 싣지 않는다.
#   - 범위 밖 404 는 **응답을 보낸 뒤에 쓴다.** 처리기(defer_out_of_scope)는 행만 만들어 요청에 걸어 두고
#     (계약 검증 · 요청 문맥은 요청이 살아 있을 때만 된다), 가장 바깥 미들웨어(DeferredPlatformEventMiddleware)가
#     응답이 끝난 뒤 쓴다 — 호출 집계(api_usage)와 같은 자리다. 응답 전에 쓰면 「범위 밖」일 때만 쓰기가 끼어
#     응답 시간으로 그 ID 가 있다는 것이 드러난다. 이 행은 중앙 관리자만 읽는다(db.repositories.platform_events).
#     쓰기가 실패하거나 그 사이 프로세스가 내려가면 행은 빠진다 — 404 는 이미 나갔다.
# ==============================================================================

from __future__ import annotations

import logging
from typing import Optional

import platform_event_log
from db.repositories import platform_events as platform_events_repo
from db.session import get_session_factory
from fastapi.concurrency import run_in_threadpool
from schemas.api.errors import ErrorCode
from schemas.platform_events import AccessDetails, PlatformEventAction, PlatformEventOutcome
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

_logger = logging.getLogger("vigilantis.platform_events")

#: 응답 뒤에 쓸 행을 요청에 걸어 두는 자리(request.state — ASGI scope 의 state 에 실린다).
_PENDING = "pending_platform_events"


def _details(request: Request, reason: ErrorCode = ErrorCode.AUTH_FORBIDDEN) -> Optional[dict]:
    """기록 형식에 맞는 (사유 · 메서드 · 경로). 실제 경로 → 라우트 틀 순으로 맞는 것을 고른다. 없으면 None."""
    route_path = getattr(request.scope.get("route"), "path", None)
    for path in (request.url.path, route_path):
        if not path:
            continue
        values = {
            "reason_code": reason.value,
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
    try:
        platform_event_log.record_detached(
            factory,
            PlatformEventAction.ACCESS_FORBIDDEN,
            platform_events_repo.person(user),
            outcome=PlatformEventOutcome.DENIED,
            tenant_id=user.tenant_id,
            details=details,
        )
    except ValueError:
        # 기록 계약 밖의 계정(계정 종류가 늘었는데 계약이 따라오지 않은 경우 등). 공통 오류 처리기 안이라 올리면
        # 403 이 500 으로 바뀐다 — 거절은 그대로 내보내고 앱 로그에 남긴다(PR 리뷰)
        _logger.error("access_denial_not_recorded: actor outside the event contract")


def defer_out_of_scope(request: Request, code: ErrorCode) -> None:
    """범위 밖 404 한 건의 행을 만들어 요청에 걸어 둔다. **쓰지 않는다** — 쓰기는 응답 뒤다(머리말).

    신원이 없거나 형식에 맞는 경로가 없으면 걸지 않는다. 기록 계약 밖의 계정이면 앱 로그에만 남긴다 — 처리기
    안이라 올리면 404 가 500 으로 바뀐다."""
    user = getattr(request.state, "current_user", None)
    if user is None:
        return
    details = _details(request, code)
    if details is None:
        _logger.warning("access_denial_not_recorded: no recordable path")
        return
    try:
        row = platform_events_repo.build_platform_event(
            action=PlatformEventAction.ACCESS_OUT_OF_SCOPE,
            actor=platform_events_repo.person(user),
            outcome=PlatformEventOutcome.DENIED,
            tenant_id=user.tenant_id,
            details=details,
        )
    except ValueError:
        _logger.error("access_denial_not_recorded: actor outside the event contract")
        return
    setattr(request.state, _PENDING, [*getattr(request.state, _PENDING, ()), row])


def _write_pending(factory, rows) -> None:
    for row in rows:
        platform_event_log.write_detached(factory, row)


class DeferredPlatformEventMiddleware:
    """응답이 끝난 뒤, 처리기가 요청에 걸어 둔 활동 로그 행을 쓴다(지금은 범위 밖 404 뿐이다).

    request_context 보다 바깥에 둔다 — 응답과 접근 로그가 끝난 뒤라 기록 시간이 응답 시간에 들지 않는다.
    걸린 행이 없으면 아무것도 하지 않는다(세션도 열지 않는다). 앱이 예외로 끝나면 쓰지 않는다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)
        if scope["type"] != "http":
            return
        rows = scope.get("state", {}).pop(_PENDING, None)
        if not rows:
            return
        app = scope["app"]
        factory = app.dependency_overrides.get(get_session_factory, get_session_factory)()
        await run_in_threadpool(_write_pending, factory, rows)
