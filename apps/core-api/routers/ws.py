# ==============================================================================
# [파일 설명]
# WebSocket /api/v1/ws — 실시간 상태 전송 연결 수립 라우터. (Issue #73)
#
#   - Handshake의 Origin을 CORS 허용 목록과 같은 값으로 검증한다. Origin이
#     없는 연결(비브라우저 클라이언트)은 통과시킨다 — Origin 검증은 브라우저
#     교차 출처 보호 장치다.
#   - 서버→클라이언트 단방향 채널이다. 클라이언트 발신 내용은 계약이 없어
#     읽고 무시한다.
# ==============================================================================

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from config import get_settings
from realtime import RealtimeManager

router = APIRouter(prefix="/api/v1")


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in get_settings().cors_allow_origins_list():
        await websocket.close(code=1008)  # 정책 위반 — 허용 목록 밖 Origin
        return

    manager: RealtimeManager = websocket.app.state.realtime
    await manager.register(websocket)
    try:
        # 텍스트·바이너리 프레임 모두 내용 무시 — 끊김 감지를 위해 raw receive 사용
        # (receive_text()는 바이너리 프레임에서 예외로 연결을 죽인다)
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
    finally:
        manager.unregister(websocket)
