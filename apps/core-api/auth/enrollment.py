# ==============================================================================
# [파일 설명]
# 플랫폼 계정 등록 링크 사용 — POST /api/v1/auth/platform-enrollment 의 트랜잭션 소유자. (#492)
#
#   - 링크는 운영 CLI(auth.bootstrap_enrollment)만 발급한다. 링크 1개로 PLATFORM ADMIN 계정 1개
#     (tenant_id NULL · ACTIVE · email_verified_at NULL)를 만든다. 계정 종류 · 역할 · 소속은 요청으로 받지 않고,
#     요청의 세션 쿠키도 읽지 않는다(권한 근거는 링크뿐). 세션을 만들지 않는다 — 첫 로그인은 Cognito 초대 메일의
#     임시 비밀번호로 기존 /login 단계(새 비밀번호 · TOTP 등록 · 이메일 코드)를 밟는다.
#   - 순서: 링크 사전 확인(읽기 — 쓸 수 없으면 404) → Cognito 어댑터 조립(미설정 503 — 링크는 그대로) →
#     예약(ISSUED → RESERVED 조건부 UPDATE + commit) → AdminCreateUser(트랜잭션 밖) →
#     확정(users 테이블 잠금 · 링크 행 FOR UPDATE 안에서 RESERVED · 미만료 재확인 → 앱 사용자 생성 →
#     REDEEMED, 한 commit).
#   - 예약 commit 이 실패하면 Cognito 를 부르지 않는다. 예약 뒤에는 어떤 실패(Cognito 오류 · 시간 초과 ·
#     메일 발송 실패 · 이미 있는 이메일 · 확정 DB 실패 · 그 사이 회수 · 만료)든 예약을 그대로 둔다 — Cognito 사용자가
#     생겼는지 앱은 모른다. 자동으로 ISSUED 로 되돌리거나 다시 만들지 않고, Cognito 사용자를 지우거나 비밀번호 ·
#     TOTP 를 바꾸지 않으며 기존 Cognito 사용자를 연결하지 않는다. 운영자가 CLI list 의 RESERVED 를 보고 Cognito
#     상태를 맞춘 뒤 회수 · 재발급한다.
#   - 확정 COMMIT 이 PG 에 반영된 뒤 응답만 잃으면(연결 끊김 등) 링크는 이미 REDEEMED · 앱 계정이 있을 수 있다 —
#     확정 단계 DB 오류는 결과 미확정으로만 기록하고 500 이다(링크 RESERVED · 계정 없음을 단정하지 않는다).
#   - 제공자 성공은 앱 계정 성공이 아니다 — 204 는 확정 commit 뒤에만이다. 204 도 초대 메일 도착의 증명이 아니다.
#   - 로그에는 enrollment_id · user_id · 오류 코드 · 예외 종류만 남긴다 — 토큰 · 해시 · 이메일 · SQL 은 싣지 않는다.
# ==============================================================================

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from config import AuthSettings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import enrollment as enrollment_repo
from exceptions import ApiError
from schemas.api.auth import AccountType, UserRole
from schemas.api.errors import ErrorCode
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .cognito import AuthError, CognitoAdapter
from .sessions import hash_token

logger = logging.getLogger("vigilantis.auth")

_RESERVED = models.PlatformEnrollmentStatus.RESERVED


def log_enrollment_status(settings: AuthSettings) -> None:
    """기동 진단 — 공개 계정 생성 경로가 열려 있으면 알린다(값 · 비밀값 없음)."""
    if settings.AUTH_PLATFORM_ENROLLMENT_ENABLED:
        logger.warning(
            "platform_enrollment_enabled: POST /api/v1/auth/platform-enrollment 이 CLI 발급 링크를 받는다"
            " — 등록이 끝나면 AUTH_PLATFORM_ENROLLMENT_ENABLED 를 끈다"
        )


def _db_failure(stage: str, exc: Exception) -> ApiError:
    logger.error("platform_enrollment_db_failure stage=%s error_type=%s", stage, type(exc).__name__)
    return ApiError(ErrorCode.INTERNAL_ERROR)


def redeem(
    db: Session,
    get_adapter: Callable[[], CognitoAdapter],
    *,
    token: str,
    email: str,
    clock: Callable[[], datetime],
) -> str:
    """링크 1개를 써서 PLATFORM ADMIN 을 만든다 → user_id. 거절은 ApiError · AuthError 다."""
    token_hash = hash_token(token)
    try:
        available = enrollment_repo.is_redeemable(db, token_hash=token_hash, now=clock())
        db.rollback()  # 읽기 트랜잭션을 어댑터 조립 · SDK 전에 끝낸다
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_failure("check", exc) from None
    if not available:
        raise ApiError(ErrorCode.ENROLLMENT_UNAVAILABLE)

    adapter = get_adapter()  # Cognito 미설정이면 여기서 503 — 링크는 아직 ISSUED 다

    try:
        enrollment_id = enrollment_repo.reserve(db, token_hash=token_hash, email=email, now=clock())
        if enrollment_id is None:  # 그 사이 다른 요청이 잡았거나 회수 · 만료됐다
            db.rollback()
            raise ApiError(ErrorCode.ENROLLMENT_UNAVAILABLE)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_failure("reserve", exc) from None
    logger.info("platform_enrollment_reserved enrollment_id=%s", enrollment_id)

    try:
        provider_user = adapter.admin_create_user(email)
    except Exception as exc:
        code = exc.code.value if isinstance(exc, AuthError) else type(exc).__name__
        logger.warning(
            "platform_enrollment_provider_failed enrollment_id=%s error=%s reservation=kept",
            enrollment_id,
            code,
        )
        raise

    try:
        auth_repo.lock_users_table(db)
        row = enrollment_repo.lock(db, enrollment_id)
        now = clock()
        if row is None or row.status is not _RESERVED or row.expires_at <= now:
            # 그 사이 회수 · 만료 — 계정을 만들지 않는다. Cognito 사용자는 남아 있을 수 있다
            state = "MISSING" if row is None else ("EXPIRED" if row.status is _RESERVED else row.status.value)
            db.rollback()
            logger.warning(
                "platform_enrollment_finalize_rejected enrollment_id=%s state=%s provider_user=may_remain",
                enrollment_id,
                state,
            )
            raise ApiError(ErrorCode.ENROLLMENT_UNAVAILABLE)
        user = auth_repo.create_user(
            db,
            issuer=adapter.issuer,
            cognito_sub=provider_user.sub,
            email=email,
            role=UserRole.ADMIN,
            account_type=AccountType.PLATFORM,
            tenant_id=None,
            now=now,
            status=models.AppUserStatus.ACTIVE,
            email_verified_at=None,
        )
        row.status = models.PlatformEnrollmentStatus.REDEEMED
        row.redeemed_user_id = user.user_id
        row.redeemed_at = now
        db.flush()
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        # COMMIT 오류는 PG 반영 여부를 알려 주지 않는다 — 링크 RESERVED · REDEEMED, 앱 계정 유무 모두 미확정이다
        logger.error(
            "platform_enrollment_finalize_failed enrollment_id=%s error_type=%s"
            " db_outcome=unknown link=reserved_or_redeemed app_user=may_exist provider_user=may_remain",
            enrollment_id,
            type(exc).__name__,
        )
        raise ApiError(ErrorCode.INTERNAL_ERROR) from None
    logger.info("platform_enrollment_redeemed enrollment_id=%s user_id=%s", enrollment_id, user.user_id)
    return user.user_id
