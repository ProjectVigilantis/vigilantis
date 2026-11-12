"""비밀번호 재설정 (#492 PR 3) — Cognito ForgotPassword / ConfirmForgotPassword + 앱 세션 회수.

- 요청: 존재 · 자격 · 이메일 미인증을 구분하지 않는다(어댑터가 같은 결과로 삼킨다). 이 계층은 응답을 만들지 않는다.
- 확정: 정책 확인 → DB 읽기 가능 확인(짧은 세션) → AdminGetUser 로 sub 확인 → ConfirmForgotPassword →
  (성공 뒤에만) 짧은 트랜잭션에서 사용자 행을 다시 찾아 세션 회수. SDK 호출 중에는 DB 트랜잭션이 없다.
- 비밀번호는 바뀌었는데 회수가 실패하면 500 이다(204 가 아니다). 이 구간에 이전 세션이 남을 수 있음은 승인된 한계다.
- 로그에는 예외 종류만 남긴다 — 이메일 · 코드 · 비밀번호 · SQL 은 싣지 않는다.
- 활동 로그(#589 B)는 확정만 남긴다(AUTH_PASSWORD_RESET). 성공은 세션 회수와 같은 트랜잭션에, 거절 · 실패는
  따로 남긴다. 대상 계정은 AdminGetUser 가 돌려준 sub 로만 잇는다 — 조회 전에 끝난 실패는 대상이 없다.
  요청(메일 발송)은 존재를 구분하지 않아 남기지 않는다. 정책에 어긋난 새 비밀번호(422)도 남기지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from db import models
from db.repositories import auth as auth_repo
from exceptions import ApiError
from schemas.api.errors import ErrorCode
from sqlalchemy import text
from sqlalchemy.orm import Session

from schemas.platform_events import PlatformEventAction

from . import event_log, sessions
from .cognito import AuthError, AuthPasswordPolicyError, CognitoAdapter
from .password_policy import check_password

logger = logging.getLogger(__name__)


def request_reset(adapter: CognitoAdapter, email: str) -> None:
    adapter.request_password_reset(email)


def _internal_error(condition: str, exc: Exception | None = None) -> ApiError:
    logger.error("password reset %s%s", condition, f" error={type(exc).__name__}" if exc else "")
    return ApiError(ErrorCode.INTERNAL_ERROR)


def confirm_reset(
    adapter: CognitoAdapter,
    db: Session,
    *,
    email: str,
    code: str,
    new_password: str,
    now: datetime,
    session_factory: Callable[[], Session],
) -> None:
    """session_factory 는 거절 · 실패를 요청 트랜잭션 밖에 남길 때만 쓴다(#589 B)."""
    if not check_password(new_password):
        raise AuthPasswordPolicyError()
    subject: event_log.Subject | None = None  # AdminGetUser 가 돌려준 신원 — 그 뒤의 실패만 대상이 있다
    try:
        try:  # 비밀번호를 바꾸기 전에 DB 를 쓸 수 있는지만 본다 — 트랜잭션은 Cognito 호출 전에 닫는다
            db.execute(text("SELECT 1"))
            db.rollback()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            raise _internal_error("database unavailable before change", exc) from None

        user = adapter.admin_get_user(email)  # 없음이 증명되지 않은 실패는 여기서 503 · 429 로 끝난다
        if user is not None:
            subject = (adapter.issuer, user.sub)
        adapter.confirm_password_reset(email, code, new_password)  # 없는 사용자도 여기서 AUTH_CODE_INVALID

        if user is None:  # 확정이 성공했는데 조회는 없음이었다 — sub 를 모르니 회수를 보장할 수 없다
            raise _internal_error("confirmed without known subject")
        try:
            auth_repo.lock_users_table(db)
            row = auth_repo.get_user_by_identity(db, issuer=adapter.issuer, cognito_sub=user.sub)
            if row is not None:
                sessions.revoke_user_sessions(
                    db,
                    user_id=row.user_id,
                    now=now,
                    reason=models.AuthSessionRevokeReason.PASSWORD_RESET,
                )
            # 성공은 세션 회수와 같은 트랜잭션이다 — 기록을 못 쓰면 아래 except 가 500 으로 끝낸다
            event_log.password_reset_succeeded(db, row)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            raise _internal_error("session revoke failed after change", exc) from None
    except (AuthError, ApiError) as exc:
        # 여기까지 온 거절 · 실패는 요청 트랜잭션을 이미 되돌렸다(Cognito 호출 중에는 트랜잭션이 없다)
        event_log.attempt_failed(
            session_factory, PlatformEventAction.AUTH_PASSWORD_RESET, exc.code, subject=subject
        )
        raise
