# ==============================================================================
# [파일 설명]
# Vigilantis Core API 서버 진입점 — create_app()과 모듈 수준 app. (Issue #68·#73)
#
#   - 오류는 exceptions의 공통 봉투로, 접근 로그는 request_context 미들웨어가
#     구조화 로그(logging_config)로 남긴다.
#   - 실시간 전송(realtime.RealtimeManager)은 앱 수명주기에 묶어 기동·종료한다.
#   - Scheduler(주기 수집) 기동은 수집·판정 연결 작업(#67)에서 연결한다.
# ==============================================================================

from __future__ import annotations

import logging
import re
import sys
import time
import uuid
from pathlib import Path

# import 경로 부트스트랩 — 다른 진입점(db/migrations/env.py·tests·시드 스크립트)과
# 같은 방식. packages(schemas)가 editable 설치로 해석되지 않는 현 워크스페이스
# 제약의 우회이며, 저장소 루트는 넣지 않는다(schemas identity 분열 방지).
_CORE_API = Path(__file__).resolve().parent
_PACKAGES = _CORE_API.parents[1] / "packages"
for _p in (str(_CORE_API), str(_PACKAGES)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contextlib import asynccontextmanager  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from config import get_settings  # noqa: E402
from exceptions import register_error_handlers, unexpected_error_response  # noqa: E402
from logging_config import request_id_var, setup_logging  # noqa: E402
from realtime import RealtimeManager  # noqa: E402
from routers import actions as actions_router  # noqa: E402
from routers import assets as assets_router  # noqa: E402
from routers import incidents as incidents_router  # noqa: E402
from routers import ws as ws_router  # noqa: E402

_http_logger = logging.getLogger("vigilantis.http")

# 수신 X-Request-ID 재사용 허용 형식 — 위반하면 새로 발급하고 원본 값은
# 응답·오류·로그 어디에도 쓰지 않는다 (팀 공용 규약 확정 전까지 임시 형식)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    realtime = RealtimeManager(send_timeout_seconds=settings.WS_SEND_TIMEOUT_SECONDS)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await realtime.start()
        try:
            yield
        finally:
            await realtime.stop()

    app = FastAPI(title="Vigilantis Core API", lifespan=lifespan)
    app.state.realtime = realtime

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """요청마다 request_id 확정 + 접근 로그 1줄. 본문은 기록하지 않는다."""
        received = request.headers.get("X-Request-ID", "")
        request_id = received if _REQUEST_ID_PATTERN.fullmatch(received) else uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            try:
                response = await call_next(request)
            except Exception:  # noqa: BLE001 — 미처리 예외도 봉투로 응답(스택은 로그로)
                response = unexpected_error_response(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            # 접근 로그는 request_id ContextVar가 살아 있는 동안(reset 전) 남긴다
            _http_logger.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(token)

    # CORS는 request_context보다 나중에 추가한다 — 나중 추가가 바깥층이라
    # 미들웨어가 만든 오류 봉투 응답에도 CORS 헤더가 붙는다
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins_list(),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_error_handlers(app)
    app.include_router(actions_router.router)
    app.include_router(assets_router.router)
    app.include_router(incidents_router.router)
    app.include_router(ws_router.router)

    @app.get("/health")
    def health() -> dict:
        """생존 확인 전용 — DB에 접속하지 않는다(스키마 준비는 compose migrate 몫)."""
        return {"status": "ok"}

    return app


# 컨테이너 기동 명령(uvicorn main:app)이 읽는 모듈 수준 앱 객체
app = create_app()
