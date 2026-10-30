# ==============================================================================
# [파일 설명]
# Vigilantis Core API 서버 진입점 — create_app()과 모듈 수준 app. (Issue #68·#73)
#
#   - 오류는 exceptions의 공통 봉투로, 접근 로그는 request_context 미들웨어가
#     구조화 로그(logging_config)로 남긴다.
#   - 실시간 전송(realtime.RealtimeManager)은 앱 수명주기에 묶어 기동·종료한다.
#   - 스케줄러 넷을 같은 수명주기에 묶는다. 실시간 전송보다 늦게 열고 먼저 닫는다.
#     수집→판정 스캔(services.scheduler)은 SCAN_ENABLED로, 접수된 조치 실행 디스패치·
#     회수 스캔(dispatcher)과 AI 분석 대기 스캔(agent_dispatcher)은 DISPATCH_ENABLED를
#     Medium 승인 기한 감시와 공유해 켜고 끈다 (Issue #232·#277·#285·#447).
#   - 모의 위협 파일 소비자는 MOCK_THREAT_INBOX_DIR 지정 시 독립 기동한다(#322).
#   - 접근 보호(#492 PR 2): 조치 · 자산 · 인시던트 · 감사 기록 · 메트릭 라우터는 포함 시점에
#     Origin(상태 변경만) → 세션 검사를 건다. 도입 신청 POST는 별도 공개 입력 경계(#552)다.
#     API 문서는 API_DOCS_ENABLED 일 때만 열린다. 조치 · 자산 · 인시던트는 각 라우트가 요청자의 고객사
#     범위로 제한하고(#533), 메트릭은 플랫폼 관리 등급(CENTRAL_ADMIN · TENANT_MANAGER)만 받는다(#492).
# ==============================================================================

from __future__ import annotations

import logging
import re
import sys
import time
import uuid
from functools import partial
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

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

import agent_dispatcher  # noqa: E402
import dispatcher  # noqa: E402
from auth.access import require_business_access, require_user  # noqa: E402
from auth.http import log_provider_status, register_auth_http, require_origin  # noqa: E402
from config import get_auth_settings, get_settings  # noqa: E402
from db.session import get_session_factory  # noqa: E402
from exceptions import register_error_handlers, unexpected_error_response  # noqa: E402
from logging_config import request_id_var, setup_logging  # noqa: E402
from mock_threat_source import MockThreatConsumer  # noqa: E402
from notifications.runtime import build_notification_runtime  # noqa: E402
from realtime import RealtimeManager  # noqa: E402
from routers import actions as actions_router  # noqa: E402
from routers import assets as assets_router  # noqa: E402
from routers import audit as audit_router  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from routers import incidents as incidents_router  # noqa: E402
from routers import metrics as metrics_router  # noqa: E402
from routers import notifications as notifications_router
from routers import signup_requests as signup_requests_router
from routers import staff as staff_router  # noqa: E402
from routers import tenants as tenants_router  # noqa: E402
from routers import users as users_router  # noqa: E402
from routers import ws as ws_router  # noqa: E402
from security.soar import start_timeout_watcher  # noqa: E402
from services.scheduler import start_scheduler as start_scan_scheduler  # noqa: E402
from support_http import SignupBodyLimitMiddleware

_http_logger = logging.getLogger("vigilantis.http")

# 수신 X-Request-ID 재사용 허용 형식 — 위반하면 새로 발급하고 원본 값은
# 응답·오류·로그 어디에도 쓰지 않는다 (팀 공용 규약 확정 전까지 임시 형식)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(
        settings.LOG_LEVEL,
        log_file_path=settings.LOG_FILE_PATH if settings.LOG_FILE_ENABLED else None,
        backup_count=settings.LOG_FILE_BACKUP_COUNT,
    )

    realtime = RealtimeManager(send_timeout_seconds=settings.WS_SEND_TIMEOUT_SECONDS)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # 실시간 전송을 먼저 열고 스캔을 붙인다 — 반대면 첫 스캔이 발행할 곳을
        # 찾지 못한다. 종료 시 진행 중인 스캔은 끝까지 돈다 — shutdown(wait=False)는
        # 스레드풀에 이미 넘어간 잡을 취소하지 못한다. 그 스캔의 발행이 전송 종료
        # 뒤에 오면 publish_dropped_at_shutdown 경고로 버려진다(realtime.py 규약)
        await realtime.start()
        # 기동에 성공한 것만 그때그때 모은다 — 뒤쪽 기동이 던져도 앞서 뜬 스케줄러가
        # finally 에서 내려간다. 리스트 컴프리헨션은 도중에 던지면 결과가 통째로
        # 유실돼 이미 기동한 스케줄러를 아무도 못 내린다.
        schedulers = []
        threat_consumer = None
        notification_runtime = None
        starts = (
            # 수집→판정→FinOps Incident 생성. 실데이터 시연의 최상류.
            partial(start_scan_scheduler, realtime.publish),
            # 접수된 조치 실행 디스패치·회수 스캔.
            partial(dispatcher.start_dispatcher, realtime.publish),
            # AI 분석 대기 Incident 스캔·회수.
            partial(agent_dispatcher.start_agent_dispatcher, realtime.publish),
            # AI 처리와 독립적으로 저장된 Medium 승인 기한을 감시한다.
            partial(start_timeout_watcher, realtime.publish),
        )
        try:
            for start in starts:
                started = start()
                if started is not None:
                    schedulers.append(started)
            if settings.MOCK_THREAT_INBOX_DIR.strip():
                threat_consumer = MockThreatConsumer(
                    Path(settings.MOCK_THREAT_INBOX_DIR),
                    get_session_factory(),
                    realtime.publish,
                    interval_seconds=settings.MOCK_THREAT_POLL_SECONDS,
                )
                threat_consumer.start()
            if settings.NOTIFICATION_ENABLED:
                notification_runtime = build_notification_runtime(settings)
                await notification_runtime.start()
            yield
        finally:
            try:
                if notification_runtime is not None:
                    await notification_runtime.stop()
            finally:
                try:
                    if threat_consumer is not None:
                        # 처리 중인 접수의 commit·발행을 마친 뒤 실시간 전송을 닫는다.
                        await threat_consumer.stop()
                finally:
                    for scheduler in reversed(schedulers):
                        scheduler.shutdown(wait=False)
                    await realtime.stop()

    # 문서 3종(/docs · /redoc · /openapi.json)은 켤 때만 둔다 — 끄면 경로 자체가 없어 404다
    docs = {} if settings.API_DOCS_ENABLED else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="Vigilantis Core API", lifespan=lifespan, **docs)
    app.state.realtime = realtime
    # request_context·보안 헤더·CORS 안쪽에서 파싱 전에 거절한다. 413에도 공통 헤더가 붙는다.
    app.add_middleware(SignupBodyLimitMiddleware)

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
            fields = {
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            # 세션 검사(auth.access.require_user)를 통과한 요청만 신원이 있다 — 식별자와 역할만
            # 싣는다(이메일 · 쿠키 · 토큰은 싣지 않는다)
            user = getattr(request.state, "current_user", None)
            if user is not None:
                fields["user_id"] = user.user_id
                fields["role"] = user.role.value
            _http_logger.info("http_request", extra=fields)
            request_id_var.reset(token)

    # /api/v1/* 보안 헤더 미들웨어와 AuthError 매핑 (#492) — CORS 보다 안쪽, request_context 보다
    # 바깥이라 미처리 예외의 500 봉투에도 헤더가 붙는다
    register_auth_http(app)
    log_provider_status(get_auth_settings())

    # CORS는 request_context보다 나중에 추가한다 — 나중 추가가 바깥층이라
    # 미들웨어가 만든 오류 봉투 응답에도 CORS 헤더가 붙는다.
    # allow_credentials: 로그인 쿠키가 교차 출처(FE 3000 → API)로 오가야 한다 — 출처는 명시 목록만(* 금지)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_error_handlers(app)
    # 접근 보호(#492 PR 2) — 순서가 계약이다: Origin(상태 변경만, DB 전) → 세션 → 라우트의 역할 검사.
    # 셋 다 라우터 본문 · Workflow · AWS 보다 먼저 돈다. auth 라우터는 Origin을 검사하고,
    # 실제 Cognito 경로만 공급자 설정을 요구한다. WebSocket은 연결 수립 때 직접 검사한다.
    protected = [Depends(require_origin), Depends(require_user)]
    # 조치 · 자산 · 인시던트(#533)는 세션 배선만 쓰고, 고객사 범위는 각 라우트가 auth.access.tenant_scope_of 로
    # 받는다(auth.access 머리말). 메트릭(#492)은 세션 다음에 플랫폼 관리 등급 관문을 더하고 범위는 라우트가 건다.
    # 사용자 · 직원 · 테넌트 관리는 각 라우터 · 서비스가 등급과 범위를 건다(고객사 ADMIN 은 사용자 관리만)
    business = [*protected, Depends(require_business_access)]
    app.include_router(actions_router.router, dependencies=protected)
    app.include_router(assets_router.router, dependencies=protected)
    app.include_router(audit_router.router, dependencies=protected)
    app.include_router(auth_router.router)
    app.include_router(incidents_router.router, dependencies=protected)
    app.include_router(metrics_router.router, dependencies=business)
    app.include_router(notifications_router.router, dependencies=protected)
    # 같은 prefix의 GET/PUT은 라우터가 CENTRAL_ADMIN 관문을 건다. 공개는 POST 한 경로뿐이다.
    app.include_router(signup_requests_router.router)
    app.include_router(staff_router.router, dependencies=protected)
    app.include_router(tenants_router.router, dependencies=protected)
    app.include_router(users_router.router, dependencies=protected)
    app.include_router(ws_router.router)

    @app.get("/health")
    def health() -> dict:
        """생존 확인 전용 — DB에 접속하지 않는다(스키마 준비는 compose migrate 몫)."""
        return {"status": "ok"}

    return app


# 컨테이너 기동 명령(uvicorn main:app)이 읽는 모듈 수준 앱 객체
app = create_app()
