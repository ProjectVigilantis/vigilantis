# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · 접근 보호)
# 고객사별 API 호출 집계 (#572 6번) — 고객사(TENANT) 계정의 `/api/*` 호출을 고객사 × UTC 1시간 칸으로 센다.
#
#   - 자리: 순수 ASGI 미들웨어(TenantApiUsageMiddleware). 응답이 끝난 뒤 세션 관문(auth.access.require_user,
#     routers.auth 의 /me · /activity)이 request.state.current_user 에 남긴 신원을 읽는다 — 인증 로직을 다시
#     돌리지 않고, 세션을 통과하지 못한 요청(401 · 공개 경로)은 신원이 없어 세지 않는다. WebSocket 은 http
#     scope 가 아니라 지나간다. 플랫폼 계정은 세지 않는다.
#   - 응답 코드와 무관하게 1회다(403 · 404 · 422 도 호출이다). 정상 완료한 요청만 센다 — 미처리 예외는
#     request_context 가 500 봉투로 바꾸므로 정상 완료에 든다.
#   - 쓰기: 요청마다 UPSERT 한 문장을 짧은 자체 DB 세션으로 스레드풀에서 commit 한다(record_call 이 트랜잭션
#     소유자). 인메모리 누적 · 주기 flush 를 두지 않는 이유 — 모아 둔 분량이 없어 재기동 · 비정상 종료에 잃는 것은
#     그 순간 기록 중이던 요청뿐이고, 상태(누적분 · 타이머)가 없어 단순하다. 기록은 최선 노력이다(아래 실패 처리).
#     비용은 요청당 UPSERT 1문이며 응답 전송 뒤라 응답 시간에 더해지지 않는다.
#   - 보관: 날짜가 바뀐 뒤 첫 기록에서 API_USAGE_RETENTION_DAYS 일(오늘 포함)보다 이른 칸을 같은 트랜잭션에서
#     지운다(보관 단위는 일이라 차례도 날짜 기준). 정리 차례는 잠금 아래 하루에 한 요청만 맡는다(동시 요청은
#     스레드풀에서 겹친다) — 그 기록이 실패하면 차례를 돌려놓아 다음 기록이 다시 맡는다.
#   - 실패(DB 오류 · 고객사 행 없음)는 로그만 남긴다 — 통계가 이미 보낸 응답이나 다음 요청을 막지 않는다.
#     이메일 · 쿠키 · 경로는 로그에 싣지 않는다.
#   - 세션 팩토리는 get_usage_session_factory 로 받는다. 테스트는 app.dependency_overrides 로 바꿔 끼운다
#     (WebSocket 의 get_auth_session_factory 와 같은 방식).
# ==============================================================================

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta

from schemas.api.auth import AccountType
from schemas.api.platform import API_USAGE_RETENTION_DAYS
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from db.repositories import platform as platform_repo
from db.session import get_session_factory
from platform_overview import floor_to_hour

_logger = logging.getLogger("vigilantis.api_usage")

#: 세션 팩토리 — 부르면 `with` 로 쓸 수 있는 Session 을 준다.
UsageSessionFactory = Callable[[], AbstractContextManager[Session]]


def get_usage_session_factory() -> UsageSessionFactory:
    """호출 기록용 세션 팩토리 — 기록마다 짧은 DB 세션을 연다. 테스트가 재정의한다."""
    return get_session_factory()


def _utcnow() -> datetime:
    """기록 시각. 테스트가 날짜 경계를 재현하려고 바꿔 끼운다."""
    return datetime.now(UTC)


def retention_cutoff(day: date) -> datetime:
    """day 를 포함해 API_USAGE_RETENTION_DAYS 일을 남기는 기준 시각(UTC 자정) — 이보다 이른 칸은 지운다."""
    first_kept = day - timedelta(days=API_USAGE_RETENTION_DAYS - 1)
    return datetime(first_kept.year, first_kept.month, first_kept.day, tzinfo=UTC)


def record_call(db: Session, *, tenant_id: str, at: datetime, prune: bool) -> bool:
    """at 가 속한 (tenant_id, UTC 정시) 칸 +1 을 commit 한다. prune 이면 보관 기간 밖 칸도 같은 트랜잭션에서 지운다.

    실패하면 rollback 하고 오류 종류만 로그에 남긴 뒤 False 다 — 호출부(미들웨어)는 이미 응답을 보냈다.
    """
    try:
        platform_repo.add_call(db, tenant_id=tenant_id, hour=floor_to_hour(at))
        if prune:
            platform_repo.delete_calls_before(db, retention_cutoff(at.astimezone(UTC).date()))
        db.commit()
        return True
    except SQLAlchemyError as exc:
        db.rollback()
        _logger.error("api_usage_record_failed error_type=%s", type(exc).__name__)
        return False


def _counted_tenant_id(scope: Scope) -> str | None:
    """세션 관문이 남긴 신원이 고객사(TENANT) 계정이면 그 고객사 ID, 아니면 None."""
    user = scope.get("state", {}).get("current_user")
    if user is None or user.account_type is not AccountType.TENANT:
        return None
    return user.tenant_id


class TenantApiUsageMiddleware:
    """`/api/*` 응답이 끝난 뒤 고객사 계정의 호출을 기록한다. 머리말 참조."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # 이 날짜의 보관 기간 정리를 맡았거나 마쳤다 — 날짜가 바뀌면 다음 기록이 다시 맡는다. 기록은 스레드풀에서
        # 겹치므로 확인과 갱신을 잠금 하나로 묶는다
        self._pruned_day: date | None = None
        self._prune_lock = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        await self.app(scope, receive, send)
        tenant_id = _counted_tenant_id(scope)
        if tenant_id is None:
            return
        app = scope["app"]
        factory = app.dependency_overrides.get(get_usage_session_factory, get_usage_session_factory)()
        await run_in_threadpool(self._record, factory, tenant_id)

    def _claim_prune(self, day: date) -> bool:
        """그 날짜의 정리 차례를 맡는다 — 같은 날 두 번째부터, 그리고 이미 더 뒤 날짜를 정리했으면 False.

        날짜는 잠금 밖에서 읽으므로 자정 직전에 들어온 요청이 늦게 올 수 있다 — 추적 날짜를 뒤로 돌리지 않는다.
        """
        with self._prune_lock:
            if self._pruned_day is not None and self._pruned_day >= day:
                return False
            self._pruned_day = day
            return True

    def _release_prune(self, day: date) -> None:
        """맡은 정리를 끝내지 못했다 — 그 날짜의 다음 기록이 다시 맡도록 하루 전으로 돌려놓는다(더 뒤 날짜를 맡은 뒤면 그대로)."""
        with self._prune_lock:
            if self._pruned_day == day:
                self._pruned_day = day - timedelta(days=1)

    def _record(self, factory: UsageSessionFactory, tenant_id: str) -> None:
        now = _utcnow()
        day = now.date()
        prune = self._claim_prune(day)
        with factory() as db:
            recorded = record_call(db, tenant_id=tenant_id, at=now, prune=prune)
        if prune and not recorded:
            self._release_prune(day)
