# ==============================================================================
# [파일 설명]
# WebSocket /api/v1/ws — 실시간 상태 전송 연결 수립 라우터. (Issue #73, #492 PR 2)
#
#   - Handshake의 Origin을 HTTP 상태 변경 검사와 같은 목록(auth.http.allowed_origins —
#     AUTH_ALLOWED_ORIGINS, 비면 CORS_ALLOW_ORIGINS)으로 정확히 일치 검증한다. Origin이
#     없는 연결(비브라우저 클라이언트)은 통과시킨다 — Origin 검증은 브라우저
#     교차 출처 보호 장치다. 세션 쿠키는 그와 별개로 필요하다.
#   - 세션 쿠키(__Host-vg_session)가 유효해야 연결을 받는다. 없거나 무효면 accept 전에
#     1008로 닫는다(Handshake HTTP 403). 판정은 auth.access.resolve_session_user 하나다.
#   - 임시 출시 관문(#492): 이벤트가 테넌트 범위로 걸러지지 않아 PLATFORM 계정만 받는다. 고객사(TENANT)
#     계정의 연결은 무효 세션과 같이 다룬다 — accept 전 1008, 연결 중이면 4401(사유를 구분하지 않는다).
#   - 연결 중에는 감시 task가 min(지금 + WS_SESSION_CHECK_SECONDS, 유휴 만료, 최대 수명)마다
#     다시 판정하고, 무효면 4401 "AUTH_REQUIRED"로 닫는다(닫기 코드: schemas/api/ws.py).
#     판정은 touch=False라 연결·확인 모두 유휴 시간을 늘리지 않는다.
#   - DB 세션은 판정 한 번 동안만 연다(연결 수명 동안 쥐지 않는다). 동기 DB 작업은
#     이벤트 루프를 막지 않게 스레드풀에서 돈다.
#   - 서버→클라이언트 단방향 채널이다. 클라이언트 발신 내용은 계약이 없어
#     읽고 무시한다.
# ==============================================================================

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta
from functools import partial

import anyio
from fastapi import APIRouter, Depends, WebSocket
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session, sessionmaker
from starlette.websockets import WebSocketDisconnect, WebSocketState

from schemas.api.auth import SessionInfo
from schemas.api.errors import ErrorCode
from schemas.api.ws import WS_CLOSE_AUTH_REQUIRED

from auth.access import get_auth_session_factory, is_platform_account, resolve_session_user
from auth.http import SESSION_COOKIE, allowed_origins, get_clock
from auth.service import CurrentUser
from config import AuthSettings, Settings, get_auth_settings, get_settings
from realtime import RealtimeManager

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger("vigilantis.ws")

_CLOSE_POLICY_VIOLATION = 1008
_CLOSE_INTERNAL_ERROR = 1011

# 테스트가 재정의한다 — 확인 주기(Settings) · 세션 팩토리 · 시계
_SETTINGS = Depends(get_settings)
_AUTH_SETTINGS = Depends(get_auth_settings)
_SESSIONS = Depends(get_auth_session_factory)
_CLOCK = Depends(get_clock)


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    settings: Settings = _SETTINGS,
    auth_settings: AuthSettings = _AUTH_SETTINGS,
    session_factory: sessionmaker[Session] = _SESSIONS,
    clock: Callable[[], datetime] = _CLOCK,
) -> None:
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in allowed_origins(auth_settings):
        await websocket.close(code=_CLOSE_POLICY_VIOLATION)  # 허용 목록 밖 Origin
        return

    token = websocket.cookies.get(SESSION_COOKIE)

    async def check() -> CurrentUser | None:
        user = await run_in_threadpool(
            resolve_session_user,
            session_factory,
            token,
            now=clock(),
            idle_seconds=auth_settings.AUTH_SESSION_IDLE_SECONDS,
        )
        # 임시 출시 관문(#492) — 큐 하나가 전 고객의 이벤트를 모든 연결에 보낸다. 플랫폼 계정만 받는다
        return user if user is not None and is_platform_account(user) else None

    user = await check()
    if user is None:
        await websocket.close(code=_CLOSE_POLICY_VIOLATION)  # 세션 없음 · 무효 — 사유를 구분하지 않는다
        return

    manager: RealtimeManager = websocket.app.state.realtime
    await manager.register(websocket)
    watch = partial(
        _watch_session,
        websocket,
        check,
        user.session,
        clock=clock,
        interval_seconds=settings.WS_SESSION_CHECK_SECONDS,
    )
    try:
        # 끊김(수신) · 세션 무효로 닫음(감시) 중 먼저 끝난 쪽이 다른 쪽을 취소한다. anyio 작업 그룹을
        # 쓰는 것은 서버 종료 · 연결 정리의 취소를 Starlette 와 같은 취소 범위로 받기 위해서다
        async with anyio.create_task_group() as group:

            async def run_then_stop(step: Callable[[], Awaitable[None]]) -> None:
                await step()
                group.cancel_scope.cancel()

            group.start_soon(run_then_stop, partial(_drain, websocket))
            group.start_soon(run_then_stop, watch)
    finally:
        manager.unregister(websocket)


async def _close(websocket: WebSocket, code: int, reason: str = "") -> None:
    """감시가 닫는다. 발행 정리 · 앱 종료가 먼저 닫았거나 클라이언트가 이미 끊었으면 할 일이 없다."""
    if websocket.application_state is WebSocketState.CONNECTED:
        with suppress(WebSocketDisconnect):
            await websocket.close(code=code, reason=reason)


async def _drain(websocket: WebSocket) -> None:
    """클라이언트 프레임을 읽고 버린다 — 끊기면 끝난다.

    텍스트·바이너리 모두 raw receive로 받는다(receive_text()는 바이너리 프레임에서 예외로
    연결을 죽인다).
    """
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _watch_session(
    websocket: WebSocket,
    check: Callable[[], Awaitable[CurrentUser | None]],
    session: SessionInfo,
    *,
    clock: Callable[[], datetime],
    interval_seconds: float,
) -> None:
    """세션이 무효가 되면 4401로 닫는다. 확인 시각은 주기와 만료 시각 중 이른 쪽이다."""
    while True:
        now = clock()
        due = min(
            now + timedelta(seconds=interval_seconds),
            session.idle_expires_at,
            session.absolute_expires_at,
        )
        await asyncio.sleep(max((due - now).total_seconds(), 0.0))
        try:
            user = await check()
        except Exception as exc:  # noqa: BLE001 — 확인하지 못한 연결을 열어 두지 않는다
            # 예외 문자열에는 SQL 파라미터(토큰 해시)가 실릴 수 있어 종류만 남긴다
            logger.warning("ws_session_check_failed", extra={"error_type": type(exc).__name__})
            await _close(websocket, _CLOSE_INTERNAL_ERROR)
            return
        if user is None:
            await _close(websocket, WS_CLOSE_AUTH_REQUIRED, ErrorCode.AUTH_REQUIRED.value)
            return
        session = user.session
