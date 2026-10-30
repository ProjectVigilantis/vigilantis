"""TOTP 분실 복구 핵심 (#515) — 지속 복구 작업 · 사용자별 인증 세대 · 신원별 TOTP 변경 울타리.

HTTP · CLI 어댑터는 reset_totp 를 부른다. 누가 누구를 복구하는지(권한 정책)는 호출부의 authorize 가 정한다 —
이 모듈은 두 번째 권한 체계를 두지 않고, 상태와 순서만 정한다.

신원 변경 울타리(totp_provider_mutations): 같은 Cognito 신원(issuer, sub)의 TOTP 변경 — 복구의 삭제와 로그인 흐름의
등록 변경(DbLoginGate.enrollment) — 은 제공자 호출 전에 울타리 행을 commit 하고, 풀리지 않은 울타리가 있으면 다른
변경 · 복구를 시작하지 않는다. 울타리는 DB 행이라 연결 · 작업자가 사라져도 남는다. 풀림은 두 가지뿐이다:
  - 작업자가 그 구간 제공자 호출의 확정 응답(성공 · 4xx)을 기록한다(PROVIDER_RETURNED).
  - 운영자가 그 호출이 더는 적용될 수 없음을 앱 밖에서 확인했다고 진술한다(reconcile_provider_mutation,
    OPERATOR_RECONCILED). 앱은 그 진술을 증명하지 않는다.
결과 불명(5xx · 시간 초과 · 전송 실패 · 분류되지 않은 실패) · 작업자 중단 · 기록 실패는 울타리를 남긴다.
시간 임대 · 자동 만료 · 자동 이어받기가 없고, 새 operation_id 도 울타리를 풀지 않는다.
확정 응답이 그 호출의 유일한 시도라는 전제는 어댑터가 보장한다 — TOTP 등록 변경 호출은 SDK 재시도 없이 한 번만 보낸다
(cognito.CognitoAdapter 의 mutation_client · provider.build_adapter). 재시도가 있으면 마지막 4xx 가 앞선 시도의 시간
초과를 가린다.
잠금 순서: 울타리 행과 app_users 를 함께 다루는 트랜잭션은 모두 users 잠금(auth_repo.lock_users_table)을 먼저 잡는다
(차단 · 등록 울타리 생성 · 새 등록 기록 · 복구 결과 기록 · 운영자 정리) — 순서가 엇갈려 교착하지 않게 한다.

reset_totp 순서:
  1. 진술(identity_verified is True) 확인 → 짧은 읽기 트랜잭션에서 authorize(행위자 · 대상). 같은 operation_id 의
     작업이 이미 있으면 그 결과를 돌려준다(Cognito 클라이언트 조립 · 호출 0회).
  2. 그 뒤에야 get_adapter()(Cognito 클라이언트 조립). 대상 issuer 가 어댑터의 풀과 다르면 거절.
  3. 차단 commit: users 잠금 → authorize 재판정(최신 상태) → 풀리지 않은 울타리가 있으면 BUSY(아무것도 안 바꾼다)
     → 세대 +1 · RESET_PENDING · 이메일 증명 제거 · 대상 세션 전부 폐기(TOTP_RESET) · 작업 행(PROVIDER_PENDING)
     · 울타리(RESET).
  4. 트랜잭션 · DB 연결 없이 AdminDeleteSoftwareToken 1회. 재시도 없는 클라이언트(provider.build_recovery_adapter)는
     호출부가 get_adapter 로 넘긴다.
  5. 결과 기록 commit(새 트랜잭션) — 울타리가 아직 이 작업의 것일 때만 작업 상태를 확정한다. 확정 응답이면 같은
     commit 에 울타리를 풀고, 삭제 확인 · 등록 없음이면 ENROLLMENT_REQUIRED(새 MFA_SETUP 만 허용). 결과 불명이면
     PROVIDER_UNKNOWN 으로 기록하고 울타리를 남긴다. 기록하지 못하면 PROVIDER_PENDING · 울타리 그대로다.

불변식:
  - 성공은 COMPLETED 하나. 결과 불명 · 중단 · 기록 실패 · 거절 · 등록 없음(PROVIDER_NOT_FOUND)은 성공이 아니다.
  - 같은 operation_id 는 제공자를 두 번 부르지 않는다(PROVIDER_PENDING 이어도, 새 등록 뒤여도). 새 operation_id 는
    새 초기화이지만 풀리지 않은 울타리가 있으면 시작하지 않는다. 자동 재삭제 · Cognito 롤백이 없다.
  - 복구는 계정 상태 · 종류 · 역할 · 비밀번호를 바꾸지 않는다. 사람의 신원 확인은 행위자의 외부 절차이며
    이 모듈은 그 진술을 기록할 뿐 증명하지 않는다. 제공자 신원 결합은 (issuer, sub) 로 따로 한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from uuid import UUID

from db import models
from db.models import (
    MfaRecoveryState,
    TotpProviderMutationKind,
    TotpProviderMutationResolution,
    TotpRecoveryStatus,
)
from db.repositories import auth as auth_repo
from db.repositories import totp_recovery as recovery_repo
from schemas.api.auth import AccountType
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from . import sessions
from .cognito import (
    AuthError,
    AuthFailedError,
    AuthProviderUnavailableError,
    CognitoAdapter,
    SoftwareTokenDeletion,
)
from .login_flow import LoginTicket

logger = logging.getLogger("vigilantis.auth")

#: 권한 판정 콜백 — (트랜잭션 안의 세션, 대상 행 · 없으면 None) → 기록할 행위자 user_id(앱 계정 없는 운영자는 None).
#: 거절은 예외로 한다(그대로 전파된다). commit · Cognito 호출을 하지 않는다. 읽기 트랜잭션과 차단 트랜잭션
#: (users 잠금 안 — 최신 상태)에서 두 번 불린다.
Authorize = Callable[[Session, models.AppUser | None], str | None]

_RECOVERABLE = frozenset({AccountType.TENANT, AccountType.PLATFORM})
_STATUS_BY_OUTCOME = {
    SoftwareTokenDeletion.DELETED: TotpRecoveryStatus.COMPLETED,
    SoftwareTokenDeletion.NOT_FOUND: TotpRecoveryStatus.PROVIDER_NOT_FOUND,
    SoftwareTokenDeletion.REJECTED: TotpRecoveryStatus.PROVIDER_REJECTED,
    SoftwareTokenDeletion.UNKNOWN: TotpRecoveryStatus.PROVIDER_UNKNOWN,
}
#: 제공자에 등록이 없다고 확인된 결과 — 새 MFA_SETUP 만 받는 상태로 넘긴다. NOT_FOUND 는 이 작업이 삭제했다는
#: 증명이 아니므로 작업은 성공이 아니지만, 남은 등록이 있으면 기존 TOTP 챌린지가 와서 로그인 흐름이 거절한다.
_NO_REGISTRATION = frozenset({TotpRecoveryStatus.COMPLETED, TotpRecoveryStatus.PROVIDER_NOT_FOUND})
_RETURNED = TotpProviderMutationResolution.PROVIDER_RETURNED
_RECONCILED = TotpProviderMutationResolution.OPERATOR_RECONCILED


class RecoveryRejection(str, Enum):
    """제공자 호출 · 앱 상태 변경 전에 거절한 사유."""

    IDENTITY_NOT_CONFIRMED = "IDENTITY_NOT_CONFIRMED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    #: UNASSIGNED · 비활성 등 복구 대상이 아니다
    TARGET_INELIGIBLE = "TARGET_INELIGIBLE"
    SELF_TARGET = "SELF_TARGET"
    #: operation_id 가 다른 대상의 작업이다
    OPERATION_CONFLICT = "OPERATION_CONFLICT"
    #: 대상의 issuer 가 어댑터의 사용자 풀과 다르다
    PROVIDER_IDENTITY_MISMATCH = "PROVIDER_IDENTITY_MISMATCH"
    #: 같은 신원의 풀리지 않은 TOTP 변경 울타리가 있다(다른 복구 · 로그인 흐름의 등록이 진행 중 · 중단 · 결과 불명).
    #: 기다리면 풀린다는 뜻이 아니다 — 그 작업자의 확정 응답 기록이나 reconcile_provider_mutation 만 푼다.
    BUSY = "BUSY"
    #: reconcile_provider_mutation — 운영자가 외부 확인을 진술하지 않았다
    EXTERNAL_STATE_NOT_CONFIRMED = "EXTERNAL_STATE_NOT_CONFIRMED"
    #: reconcile_provider_mutation — 대상 신원에 그 mutation_id 의 풀리지 않은 울타리가 없다(이미 풀렸거나 다른 울타리)
    MUTATION_NOT_FOUND = "MUTATION_NOT_FOUND"


class TotpRecoveryRejected(Exception):
    """제공자 호출 · 앱 상태 변경 없이 거절했다. 메시지는 사유 코드뿐이다."""

    def __init__(self, reason: RecoveryRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class RecoveryOperation:
    """복구 작업 1건의 확정 기록(DB 와 같은 값)."""

    operation_id: str
    target_user_id: str
    actor_user_id: str | None
    status: TotpRecoveryStatus
    generation: int
    revoked_session_count: int
    created_at: datetime
    finalized_at: datetime | None

    @classmethod
    def of(cls, row: models.TotpRecoveryOperation) -> RecoveryOperation:
        return cls(
            operation_id=str(row.operation_id),
            target_user_id=str(row.target_user_id),
            actor_user_id=None if row.actor_user_id is None else str(row.actor_user_id),
            status=row.status,
            generation=row.generation,
            revoked_session_count=row.revoked_session_count,
            created_at=row.created_at,
            finalized_at=row.finalized_at,
        )


@dataclass(frozen=True)
class TotpResetResult:
    #: DB 에 확정된 작업 — 결과 기록이 실패했으면 PROVIDER_PENDING 이다
    operation: RecoveryOperation
    #: 이미 있던 작업을 돌려줬다 — 이번 호출은 제공자 · 앱 상태를 바꾸지 않았다
    replayed: bool
    #: 이번 호출이 관측한 제공자 응답(호출하지 않았으면 None). 확정 상태가 아니다 — 판정은 operation.status 로 한다
    provider_outcome: SoftwareTokenDeletion | None

    @property
    def completed(self) -> bool:
        """지속 복구 완료 — COMPLETED 가 DB 에 기록됐다."""
        return self.operation.status is TotpRecoveryStatus.COMPLETED


@dataclass(frozen=True)
class ProviderMutation:
    """신원 변경 울타리 1건(DB 와 같은 값)."""

    mutation_id: str
    kind: TotpProviderMutationKind
    #: RESET 이면 그 복구 작업
    operation_id: str | None
    started_at: datetime
    #: None — 풀리지 않았다(그 신원의 다른 TOTP 변경 · 복구를 막는다)
    resolution: TotpProviderMutationResolution | None
    resolved_at: datetime | None
    #: 정리를 진술한 행위자(앱 계정 없는 운영자 · 정리 아님이면 None)
    reconciled_by: str | None

    @classmethod
    def of(cls, row: models.TotpProviderMutation) -> ProviderMutation:
        return cls(
            mutation_id=str(row.mutation_id),
            kind=row.kind,
            operation_id=None if row.operation_id is None else str(row.operation_id),
            started_at=row.started_at,
            resolution=row.resolution,
            resolved_at=row.resolved_at,
            reconciled_by=None if row.reconciled_by is None else str(row.reconciled_by),
        )


# --- 로그인 관문 ------------------------------------------------------------------


def _provider_returned(exc: BaseException) -> bool:
    """등록 변경 구간을 끝낸 예외가 제공자의 확정 응답(4xx 분류 · 응답 검증 거절)인지. 결과 불명이면 False.

    AuthProviderUnavailableError 는 5xx · 전송 실패 · 시간 초과 · 분류되지 않은 4xx · DB 실패를 함께 덮으므로
    적용 여부를 모른다고 본다. AuthError 가 아닌 예외(버그 · 작업자 중단)도 같다.
    확정 응답이 구간의 모든 등록 변경 시도를 끝냈다는 판단은 그 호출들이 재시도 없이 한 번만 나갔기 때문에만
    성립한다(CognitoAdapter.mutation_client) — 이 함수는 마지막 예외만 본다.
    """
    return isinstance(exc, AuthError) and not isinstance(exc, AuthProviderUnavailableError)


@dataclass
class _Claim:
    mutation_id: str
    #: 울타리를 열 때 그 신원의 앱 계정(없으면 None — 기록할 차단이 없다)
    user_id: str | None
    open: bool = True


def _ticket_current(user: models.AppUser | None, ticket: LoginTicket) -> bool:
    """등록 변경 울타리를 열 때(_claim) — 앱 계정이 포착 때의 세대 · 복구 상태 그대로인지.

    새 등록 대기 중 포착한 흐름(setup_only)은 ENROLLMENT_REQUIRED 일 때만 통과한다 — 같은 세대의 다른 흐름이 새 등록을
    기록해 NONE 이 되면, 늦은 흐름은 제공자 변경 전에 거절된다(그 등록의 이메일 증명을 빌려 둘째 TOTP 를 붙이지 못한다).
    이긴 흐름의 기록 · 이메일 단계는 이 판정을 다시 거치지 않는다(울타리는 구간 전체를 덮는다).
    """
    if user is None:
        return ticket.generation is None
    expected = MfaRecoveryState.ENROLLMENT_REQUIRED if ticket.setup_only else MfaRecoveryState.NONE
    return user.auth_generation == ticket.generation and user.mfa_recovery_state is expected


class DbLoginGate:
    """LoginFlow 의 DB 관문(login_flow.LoginGate). 조회마다 짧은 세션을 열고 닫는다 — Cognito 호출 중 트랜잭션 없음."""

    def __init__(self, engine: Callable[[], Engine], *, clock: Callable[[], datetime]) -> None:
        self._engine = engine
        self._clock = clock

    def bind(self, issuer: str, sub: str) -> LoginTicket:
        try:
            with Session(self._engine()) as db:
                user = auth_repo.get_user_by_identity(db, issuer=issuer, cognito_sub=sub)
                snapshot = None if user is None else (user.auth_generation, user.mfa_recovery_state)
        except SQLAlchemyError as exc:
            logger.error("login generation lookup failed error_type=%s", type(exc).__name__)
            raise AuthProviderUnavailableError() from None
        if snapshot is None:  # 앱 계정 없음 — 기존대로 진행하고 완료가 ACCOUNT_NOT_PROVISIONED 로 끝난다
            return LoginTicket(issuer, sub, generation=None)
        generation, state = snapshot
        if state is MfaRecoveryState.RESET_PENDING:
            logger.warning("login rejected: totp reset incomplete")
            raise AuthFailedError()
        return LoginTicket(
            issuer, sub, generation, setup_only=state is MfaRecoveryState.ENROLLMENT_REQUIRED
        )

    def proof_recorded(self, ticket: LoginTicket) -> bool:
        with Session(self._engine()) as db:
            user = auth_repo.get_user_by_identity(db, issuer=ticket.issuer, cognito_sub=ticket.sub)
            return (
                user is not None
                and user.auth_generation == ticket.generation
                and user.mfa_recovery_state is MfaRecoveryState.NONE
                and user.email_verified_at is not None
            )

    @contextmanager
    def enrollment(self, ticket: LoginTicket) -> Iterator[Callable[[], None]]:
        """TOTP 등록 변경 구간 — 들어갈 때 울타리(ENROLLMENT)를 commit 하고, 확정 응답으로 끝나면 푼다.

        결과 불명으로 끝나면(_provider_returned 가 False) 울타리를 남긴다 — 그 신원의 복구 · 다른 등록 변경은
        reconcile_provider_mutation 전까지 시작하지 않는다. 내놓는 함수는 새 등록 성공(MFA_SETUP 의
        AuthenticationResult · 신원 대조 뒤)을 기록하면서 같은 commit 에 울타리를 푼다.
        """
        claim = self._claim(ticket)
        try:
            yield lambda: self._record(claim, ticket)
        except BaseException as exc:
            if claim.open and _provider_returned(exc):
                try:
                    self._release(claim)
                except AuthError:
                    pass  # _release 가 기록했다 — 구간의 원래 예외를 낸다
            elif claim.open:
                logger.error(
                    "totp enrollment outcome unknown: identity fence kept mutation=%s",
                    claim.mutation_id,
                )
            raise
        if claim.open:
            self._release(claim)

    def _claim(self, ticket: LoginTicket) -> _Claim:
        try:
            with Session(self._engine()) as db:
                # users 잠금 — 복구 차단과 직렬화해 세대 확인과 울타리 생성을 한 시점에 둔다
                auth_repo.lock_users_table(db)
                user = auth_repo.get_user_by_identity(
                    db, issuer=ticket.issuer, cognito_sub=ticket.sub
                )
                if not _ticket_current(user, ticket):
                    logger.warning("totp enrollment rejected: login generation changed")
                    raise AuthFailedError()
                mutation_id = recovery_repo.claim_mutation(
                    db,
                    issuer=ticket.issuer,
                    cognito_sub=ticket.sub,
                    kind=TotpProviderMutationKind.ENROLLMENT,
                    operation_id=None,
                    now=self._clock(),
                )
                if mutation_id is None:
                    logger.warning("totp enrollment rejected: identity fence unresolved")
                    raise AuthProviderUnavailableError()
                claim = _Claim(mutation_id, None if user is None else user.user_id)
                db.commit()
        except SQLAlchemyError as exc:
            logger.error("totp enrollment fence failed error_type=%s", type(exc).__name__)
            raise AuthProviderUnavailableError() from None
        return claim

    def _record(self, claim: _Claim, ticket: LoginTicket) -> None:
        """새 등록 성공을 기록하고 같은 commit 에 울타리를 푼다. 울타리를 잃었거나 세대가 바뀌었으면 AuthFailedError."""
        try:
            with Session(self._engine()) as db:
                # users 잠금 먼저 — 울타리 행을 먼저 바꾸면 users 잠금을 쥔 _claim · 정리와 교착한다
                auth_repo.lock_users_table(db)
                now = self._clock()
                recorded = recovery_repo.resolve_mutation(
                    db, mutation_id=claim.mutation_id, resolution=_RETURNED, now=now
                ) and (
                    claim.user_id is None  # 앱 계정 없음 — 기록할 차단이 없다
                    or recovery_repo.record_enrollment(
                        db, user_id=claim.user_id, generation=ticket.generation, now=now
                    )
                )
                if not recorded:
                    logger.warning(
                        "totp enrollment not recorded: identity fence or login generation changed"
                    )
                    raise AuthFailedError()
                db.commit()
        except SQLAlchemyError as exc:
            logger.error("totp enrollment state failed error_type=%s", type(exc).__name__)
            raise AuthProviderUnavailableError() from None
        claim.open = False

    def _release(self, claim: _Claim) -> None:
        """확정 응답으로 끝난 구간의 울타리를 푼다. 이미 풀렸으면 AuthFailedError, 기록 실패는 AuthProviderUnavailableError."""
        try:
            with Session(self._engine()) as db:
                released = recovery_repo.resolve_mutation(
                    db, mutation_id=claim.mutation_id, resolution=_RETURNED, now=self._clock()
                )
                db.commit()
        except SQLAlchemyError as exc:
            logger.error("totp enrollment fence release failed error_type=%s", type(exc).__name__)
            raise AuthProviderUnavailableError() from None
        claim.open = False
        if not released:
            logger.warning("totp enrollment fence already resolved mutation=%s", claim.mutation_id)
            raise AuthFailedError()


# --- 복구 ---------------------------------------------------------------------------


def _canonical_user_id(value: str) -> str | None:
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


def _authorized_target(
    db: Session, target_id: str | None, authorize: Authorize
) -> tuple[models.AppUser, str | None]:
    target = auth_repo.get_user(db, target_id) if target_id is not None else None
    actor_id = authorize(db, target)
    if target is None:
        raise TotpRecoveryRejected(RecoveryRejection.TARGET_NOT_FOUND)
    if actor_id is not None and actor_id == target.user_id:
        raise TotpRecoveryRejected(RecoveryRejection.SELF_TARGET)
    if (
        target.account_type not in _RECOVERABLE
        or target.status is not models.AppUserStatus.ACTIVE
    ):
        raise TotpRecoveryRejected(RecoveryRejection.TARGET_INELIGIBLE)
    return target, actor_id


def _replay(row: models.TotpRecoveryOperation, target_user_id: str) -> TotpResetResult:
    if str(row.target_user_id) != target_user_id:
        raise TotpRecoveryRejected(RecoveryRejection.OPERATION_CONFLICT)
    return TotpResetResult(RecoveryOperation.of(row), replayed=True, provider_outcome=None)


def _fence(
    db: Session,
    operation_id: str,
    target_id: str,
    identity: tuple[str, str],
    authorize: Authorize,
    clock: Callable[[], datetime],
) -> tuple[RecoveryOperation, str] | TotpResetResult:
    """제공자 호출 전 차단 commit — (작업, 울타리 mutation_id). 잠금 안에서 같은 작업이 이미 생겼으면 그 결과(재요청)."""
    try:
        auth_repo.lock_users_table(db)
        target, actor_id = _authorized_target(db, target_id, authorize)
        if (target.issuer, target.cognito_sub) != identity:
            raise TotpRecoveryRejected(RecoveryRejection.PROVIDER_IDENTITY_MISMATCH)
        stored = recovery_repo.get_operation(db, operation_id)
        if stored is not None:
            replayed = _replay(stored, target.user_id)
            db.rollback()
            return replayed
        issuer, sub = identity
        if recovery_repo.get_unresolved_mutation(db, issuer=issuer, cognito_sub=sub) is not None:
            raise TotpRecoveryRejected(RecoveryRejection.BUSY)
        now = clock()
        generation = recovery_repo.fence_user(
            db, user_id=target.user_id, from_generation=target.auth_generation, now=now
        )
        if generation is None:  # users 잠금 안에서 방금 읽은 세대다 — 바뀌었으면 아무것도 쓰지 않는다
            raise TotpRecoveryRejected(RecoveryRejection.BUSY)
        count = sessions.revoke_user_sessions(
            db, user_id=target.user_id, now=now, reason=models.AuthSessionRevokeReason.TOTP_RESET
        )
        try:
            row = recovery_repo.create_operation(
                db,
                operation_id=operation_id,
                target_user_id=target.user_id,
                actor_user_id=actor_id,
                generation=generation,
                revoked_session_count=count,
                now=now,
            )
        except IntegrityError:  # 같은 operation_id 를 다른 대상의 작업이 먼저 썼다
            raise TotpRecoveryRejected(RecoveryRejection.OPERATION_CONFLICT) from None
        mutation_id = recovery_repo.claim_mutation(
            db,
            issuer=issuer,
            cognito_sub=sub,
            kind=TotpProviderMutationKind.RESET,
            operation_id=operation_id,
            now=now,
        )
        if mutation_id is None:  # users 잠금 안에서 방금 없음을 확인했다 — 일어나면 아무것도 쓰지 않는다
            raise TotpRecoveryRejected(RecoveryRejection.BUSY)
        pending = RecoveryOperation.of(row)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return pending, mutation_id


def _delete(adapter: CognitoAdapter, sub: str, operation_id: str) -> SoftwareTokenDeletion:
    # Username 에 로컬 사용자의 sub 를 넘긴다(AdminDeleteSoftwareToken API 참조: 별칭이 아니면 sub 를 받는다)
    try:
        return adapter.admin_delete_software_token(sub)
    except Exception as exc:  # noqa: BLE001 - 분류되지 않은 실패는 적용 여부를 모른다
        logger.error(
            "totp reset provider call failed operation=%s error_type=%s",
            operation_id,
            type(exc).__name__,
        )
        return SoftwareTokenDeletion.UNKNOWN


def _finalize(
    engine: Engine,
    pending: RecoveryOperation,
    mutation_id: str,
    outcome: SoftwareTokenDeletion,
    clock: Callable[[], datetime],
) -> TotpResetResult:
    """제공자 결과를 새 트랜잭션에서 확정한다 — 울타리가 아직 이 작업의 것일 때만. 확정 응답이면 같은 commit 에
    울타리를 풀고, 결과 불명이면 남긴다. 기록하지 못하면 PROVIDER_PENDING 결과다(예외로 바꾸지 않는다)."""
    status = _STATUS_BY_OUTCOME[outcome]
    try:
        with Session(engine) as db:
            # users 잠금 먼저 — 울타리 행을 먼저 잠그면 users 잠금을 쥔 정리 · 차단과 교착한다
            auth_repo.lock_users_table(db)
            now = clock()
            if outcome is SoftwareTokenDeletion.UNKNOWN:
                owned = recovery_repo.holds_mutation(db, mutation_id=mutation_id)
            else:
                owned = recovery_repo.resolve_mutation(
                    db, mutation_id=mutation_id, resolution=_RETURNED, now=now
                )
            recorded = (
                owned
                and recovery_repo.finalize_operation(
                    db, operation_id=pending.operation_id, status=status, now=now
                )
                and (
                    status not in _NO_REGISTRATION
                    or recovery_repo.require_enrollment(
                        db, user_id=pending.target_user_id, generation=pending.generation, now=now
                    )
                )
            )
            if recorded:
                db.commit()
            else:
                db.rollback()
    except SQLAlchemyError as exc:
        logger.error(
            "totp reset finalization failed operation=%s outcome=%s error_type=%s",
            pending.operation_id,
            outcome.value,
            type(exc).__name__,
        )
        return TotpResetResult(pending, replayed=False, provider_outcome=outcome)
    if not recorded:  # 울타리를 운영자가 이미 정리했다 — 이 작업의 결과를 확정하지 않는다
        logger.error(
            "totp reset finalization skipped: identity fence no longer held operation=%s outcome=%s",
            pending.operation_id,
            outcome.value,
        )
        return TotpResetResult(pending, replayed=False, provider_outcome=outcome)
    logger.info(
        "totp reset finalized operation=%s status=%s revoked_sessions=%d",
        pending.operation_id,
        status.value,
        pending.revoked_session_count,
    )
    return TotpResetResult(
        replace(pending, status=status, finalized_at=now), replayed=False, provider_outcome=outcome
    )


def reset_totp(
    engine: Engine,
    *,
    operation_id: UUID,
    target_user_id: str,
    identity_verified: bool,
    authorize: Authorize,
    get_adapter: Callable[[], CognitoAdapter],
    clock: Callable[[], datetime],
) -> TotpResetResult:
    """대상의 TOTP 를 복구한다(모듈 설명의 순서). 판정은 결과의 completed(= COMPLETED 기록)로 한다.

    예외: TotpRecoveryRejected — 제공자 호출 · 앱 상태 변경 없음(BUSY 는 풀리지 않은 울타리 —
    get_unresolved_provider_mutation 으로 확인한다). authorize 의 예외 · get_adapter 의 예외 · 차단 commit 전의
    DB 오류도 그대로 전파되며 상태를 바꾸지 않았다. 차단 commit 뒤로는 예외 대신 결과를 돌려준다.
    """
    if identity_verified is not True:
        raise TotpRecoveryRejected(RecoveryRejection.IDENTITY_NOT_CONFIRMED)
    op_id = str(UUID(str(operation_id)))
    with Session(engine) as db:
        target, _ = _authorized_target(db, _canonical_user_id(target_user_id), authorize)
        stored = recovery_repo.get_operation(db, op_id)
        replayed = None if stored is None else _replay(stored, target.user_id)
        target_id, identity = target.user_id, (target.issuer, target.cognito_sub)
    if replayed is not None:
        return replayed
    adapter = get_adapter()  # 판정을 모두 통과한 뒤에만 Cognito 클라이언트를 조립한다
    if adapter.issuer != identity[0]:
        raise TotpRecoveryRejected(RecoveryRejection.PROVIDER_IDENTITY_MISMATCH)
    with Session(engine) as db:
        fenced = _fence(db, op_id, target_id, identity, authorize, clock)
    if isinstance(fenced, TotpResetResult):
        return fenced
    pending, mutation_id = fenced
    outcome = _delete(adapter, identity[1], op_id)  # 트랜잭션 · DB 연결 없이
    return _finalize(engine, pending, mutation_id, outcome, clock)


def reconcile_provider_mutation(
    engine: Engine,
    *,
    target_user_id: str,
    mutation_id: UUID,
    external_state_verified: bool,
    authorize: Authorize,
    clock: Callable[[], datetime],
) -> ProviderMutation:
    """운영자 전용 — 대상 신원의 풀리지 않은 울타리 1건을 운영자의 외부 확인 진술로 푼다(OPERATOR_RECONCILED).

    external_state_verified 는 운영자가 앱 밖에서 그 제공자 호출(작업자 · 요청)이 끝났거나 더는 적용될 수 없음을
    확인했다는 진술이다 — 앱은 증명하지 않으며 시간 경과 · DB 상태 · Cognito MFA 목록으로 추정하지 않는다.
    Cognito 를 부르지 않고, 사용자 세대 · 차단 상태 · 세션 · 복구 작업 상태를 바꾸지 않는다(RESET 울타리의 작업은
    기록된 상태 그대로 — 성공이 아니다). 정리 뒤 RESET_PENDING 대상은 새 operation_id 의 reset_totp 로, 등록
    울타리였던 대상은 다음 로그인의 등록(또는 새 복구)으로 이어간다.

    authorize 는 users 잠금 안에서 한 번 불린다(reset_totp 와 같은 계약 · 대상 판정). 예외: TotpRecoveryRejected —
    EXTERNAL_STATE_NOT_CONFIRMED · TARGET_NOT_FOUND · TARGET_INELIGIBLE · SELF_TARGET · MUTATION_NOT_FOUND(상태 변경
    없음). authorize 의 예외 · DB 오류는 그대로 전파되며 상태를 바꾸지 않았다.
    """
    if external_state_verified is not True:
        raise TotpRecoveryRejected(RecoveryRejection.EXTERNAL_STATE_NOT_CONFIRMED)
    wanted = str(UUID(str(mutation_id)))
    with Session(engine) as db:
        try:
            auth_repo.lock_users_table(db)
            target, actor_id = _authorized_target(
                db, _canonical_user_id(target_user_id), authorize
            )
            row = recovery_repo.get_unresolved_mutation(
                db, issuer=target.issuer, cognito_sub=target.cognito_sub
            )
            if row is None or str(row.mutation_id) != wanted:
                raise TotpRecoveryRejected(RecoveryRejection.MUTATION_NOT_FOUND)
            now = clock()
            # 작업자의 기록과 겹치면(행 잠금 뒤 이미 풀림) 아무것도 바꾸지 않는다
            if not recovery_repo.resolve_mutation(
                db, mutation_id=wanted, resolution=_RECONCILED, now=now, reconciled_by=actor_id
            ):
                raise TotpRecoveryRejected(RecoveryRejection.MUTATION_NOT_FOUND)
            reconciled = replace(
                ProviderMutation.of(row),
                resolution=_RECONCILED,
                resolved_at=now,
                reconciled_by=actor_id,
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
    logger.warning(
        "totp identity fence reconciled by operator attestation mutation=%s kind=%s",
        wanted,
        reconciled.kind.value,
    )
    return reconciled


def get_unresolved_provider_mutation(db: Session, target_user_id: str) -> ProviderMutation | None:
    """대상 신원의 풀리지 않은 울타리 — 읽기 전용, 권한 판정 없음(운영 CLI 미리보기 · 정리 대상 확인)."""
    target_id = _canonical_user_id(target_user_id)
    target = auth_repo.get_user(db, target_id) if target_id is not None else None
    if target is None:
        return None
    row = recovery_repo.get_unresolved_mutation(
        db, issuer=target.issuer, cognito_sub=target.cognito_sub
    )
    return None if row is None else ProviderMutation.of(row)


def get_recovery_operation(db: Session, operation_id: UUID) -> RecoveryOperation | None:
    """읽기 전용 조회(운영 CLI 미리보기 등). 권한 판정을 하지 않는다 — 호출부가 판정한다."""
    row = recovery_repo.get_operation(db, str(UUID(str(operation_id))))
    return None if row is None else RecoveryOperation.of(row)


def list_recovery_operations(db: Session, target_user_id: str) -> list[RecoveryOperation]:
    """대상의 복구 작업(세대 순) — 읽기 전용, 권한 판정 없음."""
    target_id = _canonical_user_id(target_user_id)
    if target_id is None:
        return []
    return [
        RecoveryOperation.of(row)
        for row in recovery_repo.list_operations(db, target_user_id=target_id)
    ]
