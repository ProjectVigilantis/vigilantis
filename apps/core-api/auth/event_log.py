# ==============================================================================
# [파일 설명]  담당: 김승철 (BE) · 리뷰: 인증 담당
# 로그인 · 세션의 플랫폼 활동 로그(platform_events · #589 B) — 무엇을 어느 행동 · 결과로 남기는지 한곳에 둔다.
#
#   - 로그인 · MFA · 비밀번호 재설정 시도의 행위자는 로그인 전이라 ANONYMOUS 다. 시도한 계정은 대상(USER)으로
#     잇는다 — 비밀번호 단계를 지난 뒤 흐름이 포착한 안정 신원 (issuer, sub) 로만 찾는다. **이메일로 앱 계정을
#     고르지 않는다**(app_users.email 은 표시용이고 유일하지 않다). 그래서 비밀번호 단계의 실패는 대상 없이 남는다.
#   - 입력한 이메일 · 비밀번호 · 코드 · 토큰은 어디에도 싣지 않는다(#589 Q5). 남는 것은 사유 코드와 요청 문맥
#     (접속지 · 브라우저 정보 · 요청 식별자 — 기록 함수가 채운다)이다.
#   - 실패 · 거절은 요청 트랜잭션과 따로 남긴다(platform_event_log.record_detached).
#   - 성공 가운데 **세션 발급(로그인)만** 그 쓰기와 같은 트랜잭션에 남긴다 — 기록을 못 쓰면 세션이 생기지 않을 뿐이라
#     안전한 쪽으로 실패한다.
#   - **로그아웃 · 비밀번호 재설정은 예외다(2026-11-13 PM 결정 · PR 리뷰).** 세션 폐기 · 회수를 먼저 commit 하고 기록은
#     그 뒤에 따로 남긴다. 기록 한 행을 못 써서 로그아웃한 세션이 살아 있거나, 비밀번호가 바뀐 뒤 이전 세션 회수가
#     되돌려지는 쪽이 기록이 막으려는 위험보다 크다 — 기록에 실패하면 그 한 행이 빠지는 것을 감수한다.
#     잠금 순서도 이 때문에 풀린다: 기록 INSERT 는 행위자 · 대상 사용자를 FK 로 참조해 app_users 의 ROW SHARE 가
#     필요한데, 로그인 완료 · 재설정은 app_users 를 EXCLUSIVE 로 잠근 채 같은 세션 행을 폐기한다. 세션 행 잠금을
#     쥔 채 기록을 쓰면 서로를 기다려 교착한다 — 폐기를 먼저 commit 하면 기록을 쓸 때 쥔 잠금이 없다.
#   - 남기지 않는 것: 입력 형식 문제(AUTH_PASSWORD_POLICY) · 단계가 어긋난 요청(AUTH_STEP_CONFLICT) · 대상 계정을
#     모르는 만료(AUTH_FLOW_EXPIRED — 진행 쿠키 없이 온 요청). 누구의 계정에도 시도가 일어나지 않았다.
#   - 대상 계정이 고객사(TENANT) 계정이면 그 고객사를 소속(tenant_id)으로 싣는다 — 담당자의 조회 범위에 들어간다.
#   - **TOTP 분실 복구(AUTH_TOTP_RECOVERY)** 는 auth.totp_recovery.reset_totp 가 부른다. 행위자는 복구를 실행한 사람
#     (API 의 관리자)이거나 SYSTEM(운영자 CLI — 앱 계정이 없다)이고 대상은 복구되는 계정이다.
#       · 완료(COMPLETED)는 결과 확정과 **같은 트랜잭션**에 남긴다. 기록을 못 쓰면 결과도 확정되지 않아 작업이 결과
#         대기(PROVIDER_PENDING)로 남는다 — 세션 폐기와 세대 올림은 제공자 호출 전 차단 commit 에서 이미 끝나
#         있어 되살아나는 것이 없다(로그아웃 · 재설정과 결론이 다른 까닭이다).
#       · 거절 · 완료되지 못한 결과는 따로 남긴다. 대상이 범위 안으로 정해진 뒤의 일만 남긴다 — 권한 없음 · 범위 밖 ·
#         없는 대상 · 진술 없음은 여기서 남기지 않는다(403 · 범위 밖 404 는 ACCESS_* 한 행이 맡는다).
# ==============================================================================

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

import platform_event_log
from db import models
from db.repositories import auth as auth_repo
from db.repositories import platform_events as platform_events_repo
from schemas.api.auth import AccountType
from schemas.api.errors import ErrorCode
from schemas.platform_events import PlatformActorKind, PlatformEventAction, PlatformEventOutcome
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger("vigilantis.auth")

#: 진행 중인 흐름이 포착한 안정 신원 (issuer, sub).
Subject = tuple[str, str]

#: 오류로 끝나지 못한 시도 — 나머지 거절은 DENIED 다.
_FAILED_CODES = frozenset(
    {
        ErrorCode.AUTH_PROVIDER_UNAVAILABLE,
        ErrorCode.AUTH_EMAIL_DELIVERY_FAILED,
        ErrorCode.INTERNAL_ERROR,
    }
)
#: 시도로 치지 않는 거절(머리말).
_NOT_AN_ATTEMPT = frozenset({ErrorCode.AUTH_PASSWORD_POLICY, ErrorCode.AUTH_STEP_CONFLICT})


def outcome_of(code: ErrorCode) -> PlatformEventOutcome:
    return PlatformEventOutcome.FAILED if code in _FAILED_CODES else PlatformEventOutcome.DENIED


def _target(db: Session, subject: Optional[Subject]) -> tuple[Optional[str], Optional[str]]:
    """안정 신원 → (대상 user_id, 소속 tenant_id). 신원을 모르거나 앱 계정이 없으면 (None, None)."""
    if subject is None:
        return None, None
    user = auth_repo.get_user_by_identity(db, issuer=subject[0], cognito_sub=subject[1])
    return (None, None) if user is None else (user.user_id, user.tenant_id)


def attempt_failed(
    session_factory: Callable[[], Session],
    action: PlatformEventAction,
    code: ErrorCode,
    *,
    subject: Optional[Subject] = None,
) -> None:
    """실패 · 거절된 시도 한 건을 따로 남긴다. 호출부는 자기 트랜잭션을 되돌린 뒤 부른다.

    대상 조회나 쓰기가 DB 오류로 실패해도 예외를 올리지 않는다 — 이미 거절 중인 응답을 막지 않는다."""
    if code in _NOT_AN_ATTEMPT or (code is ErrorCode.AUTH_FLOW_EXPIRED and subject is None):
        return
    target_id = tenant_id = None
    if subject is not None:
        try:
            with session_factory() as db:
                target_id, tenant_id = _target(db, subject)
                db.rollback()  # 읽기 트랜잭션을 닫고 쓴다(같은 세션을 돌려쓰는 공장에서도 잠금이 남지 않게)
        except SQLAlchemyError as exc:
            logger.error("auth_event_target_lookup_failed error_type=%s", type(exc).__name__)
    platform_event_log.record_detached(
        session_factory,
        action,
        platform_events_repo.ANONYMOUS,
        outcome=outcome_of(code),
        tenant_id=tenant_id,
        target_id=target_id,
        details={"reason_code": code.value},
    )


def login_succeeded(db: Session, user: models.AppUser, *, session_id: str) -> None:
    """세션 발급과 같은 트랜잭션에 로그인 성공을 남긴다. 그 요청에서 생긴 세션을 함께 싣는다."""
    platform_events_repo.record_platform_event(
        db,
        PlatformEventAction.AUTH_LOGIN,
        platform_events_repo.ANONYMOUS,
        tenant_id=user.tenant_id,
        target_id=user.user_id,
        session_id=session_id,
    )


def _record_after_commit(
    session_factory: Callable[[], Session],
    action: PlatformEventAction,
    actor: platform_events_repo.EventActor,
    *,
    tenant_id: Optional[str],
    target_id: Optional[str] = None,
    outcome: PlatformEventOutcome = PlatformEventOutcome.SUCCEEDED,
    reason_code: Optional[str] = None,
) -> None:
    """이미 끝난 일의 행을 따로 남긴다. 어떤 실패도 올리지 않는다 — 끝난 일의 응답을 바꾸지 않는다.

    DB 쓰기 실패는 record_detached 가 앱 로그로 돌린다. 계약 위반(ValueError)은 거기서 올라오므로 여기서 받는다."""
    try:
        platform_event_log.record_detached(
            session_factory,
            action,
            actor,
            outcome=outcome,
            tenant_id=tenant_id,
            target_id=target_id,
            details=None if reason_code is None else {"reason_code": reason_code},
        )
    except ValueError:
        logger.error("auth_event_not_recorded: outside the event contract action=%s", action.value)


def password_reset_succeeded(
    session_factory: Callable[[], Session], *, user_id: Optional[str], tenant_id: Optional[str]
) -> None:
    """비밀번호가 바뀌고 세션 회수가 commit 된 **뒤에** 따로 남긴다. 앱 계정이 없는 신원이면 대상 없이 남는다.

    쓰기가 실패해도 예외를 올리지 않는다 — 재설정은 이미 끝났고, 빠지는 것은 이 한 행뿐이다(머리말)."""
    _record_after_commit(
        session_factory,
        PlatformEventAction.AUTH_PASSWORD_RESET,
        platform_events_repo.ANONYMOUS,
        tenant_id=tenant_id,
        target_id=user_id,
    )


@dataclass(frozen=True)
class LoggedOut:
    """폐기한 세션의 주인 — 폐기를 commit 한 뒤 기록에 쓸 값만 미리 읽어 둔다."""

    actor: platform_events_repo.EventActor
    tenant_id: Optional[str]


def logout_actor(db: Session, session: models.AuthSession) -> Optional[LoggedOut]:
    """로그아웃 기록의 행위자(그 세션의 사용자). 폐기와 같은 트랜잭션에서 **읽기만** 한다 — 쓰지 않는다.

    기록 계약이 받는 사람은 PLATFORM(등급 있음) · TENANT 뿐이다. 그 밖(소속이 풀린 계정 · 등급 없는 플랫폼 계정)의
    남은 세션은 폐기만 하고 앱 로그에 남긴다(None) — 기록 때문에 로그아웃이 실패하면 안 된다."""
    user = auth_repo.get_user(db, session.user_id)
    recordable = user is not None and (
        user.account_type is AccountType.TENANT
        or (user.account_type is AccountType.PLATFORM and user.platform_role is not None)
    )
    if not recordable:
        logger.warning("auth_logout_not_recorded: account outside the event contract")
        return None
    return LoggedOut(
        actor=platform_events_repo.EventActor(
            PlatformActorKind.USER,
            user_id=user.user_id,
            account_type=user.account_type,
            platform_role=user.platform_role,
            session_id=session.session_id,
        ),
        tenant_id=user.tenant_id,
    )


def logged_out(session_factory: Callable[[], Session], who: LoggedOut) -> None:
    """세션 폐기가 commit 된 **뒤에** 로그아웃을 따로 남긴다. 쓰기가 실패해도 예외를 올리지 않는다(머리말)."""
    _record_after_commit(
        session_factory, PlatformEventAction.AUTH_LOGOUT, who.actor, tenant_id=who.tenant_id
    )


# --- TOTP 분실 복구 -------------------------------------------------------------------


def check_totp_recovery_actor(
    actor: platform_events_repo.EventActor, *, target_user_id: str, tenant_id: Optional[str]
) -> None:
    """완료 행이 기록 계약에 맞는지 미리 본다. 맞지 않으면 ValueError — 복구가 아무것도 바꾸기 전에 멈춘다.

    완료 행은 결과 확정과 같은 트랜잭션이라, 계약에 걸리면 제공자의 TOTP 등록은 지워졌는데 작업은 결과 대기로
    남는다. 그 자리까지 가서 알게 되지 않도록 차단 commit 전에 같은 값으로 한 번 만들어 본다(쓰지 않는다)."""
    platform_events_repo.build_platform_event(
        action=PlatformEventAction.AUTH_TOTP_RECOVERY,
        actor=actor,
        tenant_id=tenant_id,
        target_id=target_user_id,
    )


def totp_recovery_completed(
    db: Session,
    actor: platform_events_repo.EventActor,
    *,
    target_user_id: str,
    tenant_id: Optional[str],
) -> None:
    """복구 완료(COMPLETED)를 결과 확정과 같은 트랜잭션에 남긴다. commit 은 호출부가 한다."""
    platform_events_repo.record_platform_event(
        db,
        PlatformEventAction.AUTH_TOTP_RECOVERY,
        actor,
        tenant_id=tenant_id,
        target_id=target_user_id,
    )


def totp_recovery_not_completed(
    session_factory: Callable[[], Session],
    actor: platform_events_repo.EventActor,
    outcome: PlatformEventOutcome,
    reason_code: str,
    *,
    target_user_id: str,
) -> None:
    """거절됐거나 완료되지 못한 복구 한 건을 따로 남긴다. 호출부는 자기 트랜잭션을 끝낸 뒤 부른다.

    소속은 대상 계정에서 읽는다. 조회나 쓰기가 실패해도 예외를 올리지 않는다 — 이미 정해진 응답을 바꾸지 않는다."""
    tenant_id = None
    try:
        with session_factory() as db:
            user = auth_repo.get_user(db, target_user_id)
            tenant_id = None if user is None else user.tenant_id
            db.rollback()  # 읽기 트랜잭션을 닫고 쓴다
    except SQLAlchemyError as exc:
        logger.error("auth_event_target_lookup_failed error_type=%s", type(exc).__name__)
    _record_after_commit(
        session_factory,
        PlatformEventAction.AUTH_TOTP_RECOVERY,
        actor,
        tenant_id=tenant_id,
        target_id=target_user_id,
        outcome=outcome,
        reason_code=reason_code,
    )
