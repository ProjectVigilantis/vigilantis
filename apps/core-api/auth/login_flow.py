"""로그인 상태 머신 (Post-MVP T-1, #492 PR 1).

서버가 이 로그인 흐름 안에서 Cognito 로부터 TOTP 단계 성공 응답을 직접 받은 경우에만 COMPLETED 와
VerifiedIdentity 를 돌려준다.
  (a) AdminRespondToAuthChallenge(SOFTWARE_TOKEN_MFA) 가 AuthenticationResult 를 돌려줌
  (b) VerifySoftwareToken Status == SUCCESS 후 AdminRespondToAuthChallenge(MFA_SETUP) 가 AuthenticationResult 를 돌려줌
그 밖의 토큰 발급 · 챌린지는 거절한다. acr · auth_time · 사용자 MFA 설정은 근거로 쓰지 않는다.
토큰은 호출자에게 반환하지 않고 로그 · 저장하지 않는다. refresh token 은 완료 · 실패 · 요청 중 만료 확인 때
그 자리에서 폐기하고, 사용자가 떠난 흐름은 다음 요청의 정리(sweep) 때 폐기한다(재시작하면 메모리와 함께 사라진다).

TOTP 분실 복구(#515) — 비밀번호 단계 뒤 · TOTP 증명 전에 AdminGetUser 가 돌려준 sub 로 앱 계정의 인증
세대를 포착한다(LoginTicket). TOTP 증명 뒤 GetUser 의 sub 가 포착한 sub 와 같아야 하고, VerifiedIdentity 는
포착한 세대를 싣는다 — 완료(auth.service.complete_login)가 users 잠금 안에서 현재 세대와 대조한다.
복구 차단 중(RESET_PENDING)이면 흐름을 시작하지 않는다. 새 등록 대기 중에 포착한 흐름은 새 MFA_SETUP 만
진행한다. TOTP 등록 변경(AssociateSoftwareToken · VerifySoftwareToken · MFA_SETUP 응답 · 선호 설정)은 관문의
신원 변경 울타리 안에서, 포착한 세대가 아직 현재일 때만 Cognito 에 보낸다. 새 등록은 MFA_SETUP 응답의
AuthenticationResult 와 GetUser 신원 대조가 끝난 뒤에만 기록한다(토큰은 흐름이 쥐어 기록 실패 때 폐기된다).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import NamedTuple, Protocol
from urllib.parse import quote

from schemas.api.auth import LoginStep

from .cognito import (
    AuthCodeInvalidError,
    AuthError,
    AuthFailedError,
    AuthFlowExpiredError,
    AuthPasswordPolicyError,
    AuthRateLimitedError,
    AuthStepConflictError,
    CognitoAdapter,
    CognitoResponse,
    Tokens,
)
from .flow_store import FailureLimiter, FlowStore
from .password_policy import check_password

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 180
TOKEN_TTL_CAP_SECONDS = 300
_FRIENDLY_DEVICE_NAME = "Vigilantis"
_NAME = re.compile(r"[A-Za-z0-9_]{1,40}")


class MfaStep(str, Enum):
    SOFTWARE_TOKEN_MFA = "SOFTWARE_TOKEN_MFA"
    MFA_SETUP = "MFA_SETUP"


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    sub: str
    email: str
    mfa_step: MfaStep
    email_proven: bool = False  # 이 흐름에서 이메일 코드(VerifyUserAttribute)가 성공했을 때만 True
    #: TOTP 증명 전에 포착한 앱 계정의 인증 세대(#515). None 은 포착 때 앱 계정이 없었다는 뜻이다.
    generation: int | None = field(kw_only=True)


@dataclass(frozen=True)
class LoginTicket:
    """비밀번호 단계 뒤 · TOTP 증명 전에 포착한 안정 신원(issuer, sub)과 그때의 인증 세대 (#515)."""

    issuer: str
    sub: str
    #: None — 포착 때 그 신원의 앱 계정이 없었다(완료가 ACCOUNT_NOT_PROVISIONED 로 끝난다).
    generation: int | None
    #: 복구 뒤 새 등록을 기다리는 중에 포착했다 — 새 MFA_SETUP 만 진행할 수 있다.
    setup_only: bool = False


class LoginGate(Protocol):
    """LoginFlow 가 앱 상태를 읽고 TOTP 등록 변경을 직렬화하는 경계 (#515). AuthError 만 던진다."""

    def bind(self, issuer: str, sub: str) -> LoginTicket:
        """현재 세대를 포착한다. 복구 차단 중이면 AuthFailedError."""
        ...

    def proof_recorded(self, ticket: LoginTicket) -> bool:
        """포착한 세대가 아직 현재이고 그 세대의 이메일 증명이 기록돼 있는지."""
        ...

    def enrollment(self, ticket: LoginTicket) -> AbstractContextManager[Callable[[], None]]:
        """신원별 TOTP 등록 변경 구간 — 들어갈 때 포착한 세대가 현재여야 한다(아니면 AuthFailedError).

        같은 신원의 다른 변경 · 복구가 풀리지 않았으면 AuthProviderUnavailableError. 구간이 결과 불명으로 끝나면
        (AuthProviderUnavailableError · 그 밖의 예외) 신원은 운영자 정리 전까지 막힌다. 내놓는 함수는 새 등록
        성공을 기록한다(새 등록 대기 → 차단 없음) — 실패하면 AuthError 를 낸다.
        """
        ...


class StepResult(NamedTuple):
    step: LoginStep
    identity: VerifiedIdentity | None = None


@dataclass(frozen=True)
class TotpSetup:
    secret: str = field(repr=False)
    otpauth_uri: str = field(repr=False)


@dataclass
class _Flow:
    email: str
    username: str = ""
    step: LoginStep = LoginStep.TOTP_REQUIRED
    session: str | None = field(default=None, repr=False)
    setup_started: bool = False
    failures: int = 0
    expires_at: float = 0.0
    tokens: Tokens | None = field(default=None, repr=False)
    mfa_step: MfaStep | None = None
    identity: VerifiedIdentity | None = None
    ticket: LoginTicket | None = None


_CHALLENGE_STEPS = {
    "NEW_PASSWORD_REQUIRED": LoginStep.NEW_PASSWORD_REQUIRED,
    "MFA_SETUP": LoginStep.TOTP_SETUP_REQUIRED,
    "SOFTWARE_TOKEN_MFA": LoginStep.TOTP_REQUIRED,
}


class LoginFlow:
    def __init__(
        self,
        cognito: CognitoAdapter,
        store: FlowStore[_Flow] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_failures: int = 3,
        code_failure_limit: int = 10,
        code_failure_window_seconds: int = 900,
        gate: LoginGate,
    ) -> None:
        self._gate = gate
        self._cognito = cognito
        self._store: FlowStore[_Flow] = store if store is not None else FlowStore()
        self._clock = clock
        self._max_failures = max_failures
        self._limiter = FailureLimiter(code_failure_limit, code_failure_window_seconds)

    # ------------------------------------------------------------------ 공개 API

    def start(self, email: str, password: str) -> tuple[str, LoginStep]:
        self._sweep()
        flow = _Flow(email=email)
        resp = self._cognito.initiate_auth(email, password)
        self._apply_challenge(flow, resp, allow_new_password=True, set_username=True)
        if self._limiter.blocked(flow.username, self._clock()):
            self._log_rate_limited(flow.username)
            raise AuthRateLimitedError() from None
        self._bind(flow)
        token = self._store.new_token()
        self._store.put(token, flow, flow.expires_at)
        return token, flow.step

    def submit_new_password(self, flow_token: str, new_password: str) -> StepResult:
        if not check_password(new_password):  # Cognito(흐름 정리 포함) 호출 전에 거절한다
            raise AuthPasswordPolicyError() from None

        def op(flow: _Flow) -> StepResult:
            assert flow.session is not None
            resp = self._cognito.respond_to_challenge(
                "NEW_PASSWORD_REQUIRED", flow.session, flow.username, {"NEW_PASSWORD": new_password}
            )
            self._apply_challenge(flow, resp, allow_new_password=False)
            return StepResult(flow.step)

        return self._run(flow_token, op, lambda f: f.step is LoginStep.NEW_PASSWORD_REQUIRED)

    def begin_totp_setup(self, flow_token: str) -> TotpSetup:
        def op(flow: _Flow) -> TotpSetup:
            assert flow.session is not None and flow.ticket is not None
            with self._gate.enrollment(flow.ticket):
                secret, session = self._cognito.associate_software_token(flow.session)
            if not session:
                self._reject("ASSOCIATE_WITHOUT_SESSION")
            flow.session = session
            flow.setup_started = True
            flow.expires_at = self._clock() + SESSION_TTL_SECONDS
            label = quote(flow.email, safe="")
            uri = f"otpauth://totp/Vigilantis:{label}?secret={secret}&issuer=Vigilantis"
            return TotpSetup(secret, uri)

        return self._run(flow_token, op, lambda f: f.step is LoginStep.TOTP_SETUP_REQUIRED)

    def verify_totp_setup(self, flow_token: str, code: str) -> StepResult:
        def op(flow: _Flow) -> StepResult:
            assert flow.session is not None and flow.ticket is not None
            # 등록 확정 · MFA_SETUP 응답 · 선호 설정까지 같은 울타리 안에서 한다 — 복구 차단과 겹치지 않는다
            with self._gate.enrollment(flow.ticket) as record_enrollment:
                status, session = self._cognito.verify_software_token(
                    flow.session, code, _FRIENDLY_DEVICE_NAME
                )
                if status != "SUCCESS" or not session:
                    self._reject("VERIFY_SOFTWARE_TOKEN_" + (status if _NAME.fullmatch(status) else "UNKNOWN"))
                flow.session = session
                resp = self._cognito.respond_to_challenge("MFA_SETUP", session, flow.username, {})
                if resp.tokens is None:
                    self._reject_challenge(resp, after="MFA_SETUP")
                # 새 등록 기록(이 세대의 새 등록 대기를 끝낸다 · 이메일 증명은 별도)은 AuthenticationResult ·
                # 신원 대조 뒤에만 — 실패하면 흐름이 쥔 토큰이 폐기된다
                return self._after_totp(
                    flow, resp.tokens, MfaStep.MFA_SETUP, on_verified=record_enrollment
                )

        return self._run(
            flow_token,
            op,
            lambda f: f.step is LoginStep.TOTP_SETUP_REQUIRED and f.setup_started,
            submits_code=True,
        )

    def submit_totp(self, flow_token: str, code: str) -> StepResult:
        def op(flow: _Flow) -> StepResult:
            assert flow.session is not None
            resp = self._cognito.respond_to_challenge(
                "SOFTWARE_TOKEN_MFA", flow.session, flow.username, {"SOFTWARE_TOKEN_MFA_CODE": code}
            )
            if resp.tokens is None:
                self._reject_challenge(resp, after="SOFTWARE_TOKEN_MFA")
            return self._after_totp(flow, resp.tokens, MfaStep.SOFTWARE_TOKEN_MFA)

        return self._run(flow_token, op, lambda f: f.step is LoginStep.TOTP_REQUIRED, submits_code=True)

    def submit_email_code(self, flow_token: str, code: str) -> StepResult:
        def op(flow: _Flow) -> StepResult:
            assert flow.tokens is not None and flow.identity is not None
            self._cognito.verify_email_code(flow.tokens.access_token, code)
            flow.identity = replace(flow.identity, email_proven=True)
            return StepResult(LoginStep.COMPLETED, flow.identity)

        return self._run(
            flow_token,
            op,
            lambda f: f.step is LoginStep.EMAIL_VERIFICATION_REQUIRED,
            submits_code=True,
        )

    # ------------------------------------------------------------------ 내부 구현

    def _log_rate_limited(self, username: str) -> None:
        digest = hashlib.sha256(username.encode()).hexdigest()[:12]
        logger.warning("login code failure limit reached user=%s", digest)

    def _run(self, flow_token, op, matches, *, submits_code: bool = False):
        self._sweep()
        taken = self._store.take(flow_token, self._clock())
        if taken is None:
            raise AuthFlowExpiredError() from None
        flow, expired = taken
        if expired:
            self._discard(flow)
            raise AuthFlowExpiredError() from None
        if not matches(flow):
            self._store.put(flow_token, flow, flow.expires_at)
            raise AuthStepConflictError() from None
        if submits_code and self._limiter.blocked(flow.username, self._clock()):
            self._discard(flow)
            self._log_rate_limited(flow.username)
            raise AuthRateLimitedError() from None
        try:
            result = op(flow)
        except AuthPasswordPolicyError:
            self._store.put(flow_token, flow, flow.expires_at)
            raise
        except AuthCodeInvalidError:
            # 시도 상한: 한 흐름에서 코드 제출은 max_failures 번까지다. 그 번째 실패에서 흐름을 끝낸다.
            self._limiter.record(flow.username, self._clock())
            flow.failures += 1
            if flow.failures >= self._max_failures:
                self._discard(flow)
                raise AuthFlowExpiredError() from None
            self._store.put(flow_token, flow, flow.expires_at)
            raise
        except AuthError:
            self._discard(flow)
            raise
        if isinstance(result, StepResult) and result.step is LoginStep.COMPLETED:
            self._discard(flow)
        else:
            self._store.put(flow_token, flow, flow.expires_at)
        return result

    def _sweep(self) -> None:
        for flow in self._store.sweep(self._clock()):
            self._discard(flow)

    def _revoke(self, tokens: Tokens | None) -> None:
        if tokens is None or not tokens.refresh_token:
            return
        try:
            self._cognito.revoke_refresh_token(tokens.refresh_token)
        except AuthError:
            logger.error("login refresh token revoke failed")

    def _discard(self, flow: _Flow) -> None:
        tokens, flow.tokens = flow.tokens, None
        self._revoke(tokens)

    def _reject(self, condition: str, tokens: Tokens | None = None):
        self._revoke(tokens)
        logger.error("login rejected: %s", condition)
        raise AuthFailedError() from None

    def _reject_challenge(self, resp: CognitoResponse, *, after: str):
        name = resp.challenge if resp.challenge and _NAME.fullmatch(resp.challenge) else "UNKNOWN"
        self._reject(f"unexpected challenge {name} after {after}")

    def _apply_challenge(
        self,
        flow: _Flow,
        resp: CognitoResponse,
        *,
        allow_new_password: bool,
        set_username: bool = False,
    ) -> None:
        if resp.tokens is not None:
            self._reject("tokens issued before TOTP", resp.tokens)
        step = _CHALLENGE_STEPS.get(resp.challenge or "")
        if step is None or (step is LoginStep.NEW_PASSWORD_REQUIRED and not allow_new_password):
            self._reject_challenge(resp, after="password step")
        if not resp.session:
            self._reject("challenge without session")
        if set_username:
            username = resp.params.get("USER_ID_FOR_SRP")
            if not username:
                self._reject("challenge without USER_ID_FOR_SRP")
            flow.username = username
        flow.session = resp.session
        flow.step = step
        flow.setup_started = False
        flow.expires_at = self._clock() + SESSION_TTL_SECONDS
        self._require_setup_while_reenrolling(flow)

    def _bind(self, flow: _Flow) -> None:
        """TOTP 증명 전에 안정 신원과 인증 세대를 포착한다 — 이메일 · 사용자 이름으로 앱 계정을 고르지 않는다."""
        user = self._cognito.admin_get_user(flow.username)
        if user is None:
            self._reject("AdminGetUser found no user after password step")
        flow.ticket = self._gate.bind(self._cognito.issuer, user.sub)
        self._require_setup_while_reenrolling(flow)

    def _require_setup_while_reenrolling(self, flow: _Flow) -> None:
        # 새 등록 대기 중 포착한 흐름에 기존 TOTP 챌린지가 오면 삭제 확인 뒤에도 남은 등록이다 — 진행하지 않는다
        if flow.ticket is not None and flow.ticket.setup_only and flow.step is LoginStep.TOTP_REQUIRED:
            self._reject("totp reset requires fresh enrollment")

    def _proof_recorded(self, ticket: LoginTicket) -> bool:
        try:
            return bool(self._gate.proof_recorded(ticket))
        except Exception:  # noqa: BLE001 - fail closed on any lookup error
            logger.error("email proof lookup failed")
            return False  # 조회 실패는 증명 없음으로 본다(이메일 코드 요구)

    def _after_totp(
        self,
        flow: _Flow,
        tokens: Tokens,
        mfa_step: MfaStep,
        *,
        on_verified: Callable[[], None] | None = None,
    ) -> StepResult:
        """on_verified 는 토큰을 흐름에 맡기고 GetUser 신원이 포착한 신원과 같다고 확인한 뒤에 부른다."""
        now = self._clock()
        flow.tokens = tokens
        flow.session = None
        flow.expires_at = now + min(tokens.expires_in, TOKEN_TTL_CAP_SECONDS)
        flow.mfa_step = mfa_step
        first_enrollment = mfa_step is MfaStep.MFA_SETUP
        if first_enrollment:
            self._cognito.prefer_software_token_mfa(tokens.access_token)
        attrs = self._cognito.get_user(tokens.access_token)
        sub = attrs.get("sub")
        if not sub:
            self._reject("GetUser without sub")
        ticket = flow.ticket
        assert ticket is not None
        if sub != ticket.sub or self._cognito.issuer != ticket.issuer:
            self._reject("GetUser identity differs from bound identity")
        flow.identity = VerifiedIdentity(
            issuer=self._cognito.issuer,
            sub=sub,
            email=attrs.get("email") or flow.email,
            mfa_step=mfa_step,
            generation=ticket.generation,
        )
        if on_verified is not None:
            on_verified()
        if (
            not first_enrollment
            and attrs.get("email_verified") == "true"
            and self._proof_recorded(ticket)
        ):
            return StepResult(LoginStep.COMPLETED, flow.identity)
        self._cognito.send_email_code(tokens.access_token)
        flow.step = LoginStep.EMAIL_VERIFICATION_REQUIRED
        return StepResult(flow.step)
