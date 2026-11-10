"""#552 도입 신청 API 계약 — 익명 접수(POST)와 중앙 관리자 처리(GET · PUT status · POST approve).

승인(#572 묶음 B)은 POST /{id}/approve 로만 한다. 한 트랜잭션에서 고객사를 만들고(이름은 저장된 company_name,
PENDING_CONNECTION · 담당 그룹 없음) 신청을 APPROVED 로 옮겨 tenant_id 로 잇는다. 받는 상태는 NEW · IN_REVIEW 이고
밖이면 409 SIGNUP_REQUEST_STATE_CONFLICT 다. 같은 계정의 고객사가 있으면 409 TENANT_ACCOUNT_CONFLICT, 온보딩
설정이 없으면 503 TENANT_ONBOARDING_UNAVAILABLE 이며, 어느 거절에서도 신청 · 고객사 · 결정 기록이 바뀌지 않는다.
PUT status 로 APPROVED 를 보내면 409 SIGNUP_REQUEST_APPROVAL_REQUIRED 다.

접수 재시도 키(Idempotency-Key)는 헤더로 받는다(UUIDv4). 같은 키 · 같은 검증 결과 값이면 처음 접수한
request_id 를 202 로 다시 돌려주고, 값이 다르면 409 IDEMPOTENCY_KEY_CONFLICT 다. 키는 접수와 같은 수명으로
저장한다 — 보존 · 삭제 기준은 공개 전 T15 에서 정한다. 키는 스팸 방지도 이메일 소유 확인도 아니다.

값 규약(재시도 비교도 이 검증 결과 값으로 한다 — 원문 JSON 이 아니다):
  - 모든 문자열은 앞뒤 공백을 지운다. 회사명 · 담당자명은 비어 있을 수 없다.
  - 전화 · 메모는 지운 뒤 비면 null 과 같다. 전화번호 형식은 약속하지 않는다.
  - 이메일은 기존 Email 형식 그대로이며 대소문자를 바꾸지 않는다.
  - PostgreSQL 이 저장하지 못하는 NUL · 짝 없는 surrogate 는 422 다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    StringConstraints,
    model_validator,
)

from .assets import UtcDateTime
from .auth import Email
from .support import (
    REJECTION_REASON_MAX_LENGTH,
    SHORT_TEXT_MAX_LENGTH,
    SUPPORT_TEXT_MAX_LENGTH,
    SignupRequestStatus,
    SupportPage,
)
from .tenants import AwsAccountId, TenantCreateResponse, TenantRegions, require_unique


def _storable(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL 문자를 사용할 수 없습니다")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("유효한 UTF-8 문자열이어야 합니다") from exc
    return value


def _blank_to_none(value: str | None) -> str | None:
    return value or None


def _text(max_length: int, *, min_length: int = 1):
    return Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length),
        AfterValidator(_storable),
    ]


_ShortText = _text(SHORT_TEXT_MAX_LENGTH)
_OptionalShortText = Annotated[
    _text(SHORT_TEXT_MAX_LENGTH, min_length=0) | None, AfterValidator(_blank_to_none)
]
_OptionalNote = Annotated[
    _text(SUPPORT_TEXT_MAX_LENGTH, min_length=0) | None, AfterValidator(_blank_to_none)
]
_StorableEmail = Annotated[Email, AfterValidator(_storable)]
_RejectionReason = _text(REJECTION_REASON_MAX_LENGTH)

# 입력 값이 오류 응답 · 로그에 실리지 않게 한다(개인정보).
_CONFIG = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SignupRequestCreate(BaseModel):
    """POST /api/v1/signup-requests 본문. 세션 없는 공개 접수다."""

    model_config = _CONFIG

    company_name: _ShortText
    contact_name: _ShortText
    email: _StorableEmail
    phone: _OptionalShortText = None
    note: _OptionalNote = None


class SignupRequestAccepted(BaseModel):
    """202 응답 — 처음 접수와 재시도 응답이 같다."""

    model_config = ConfigDict(extra="forbid")

    request_id: str


class SignupRequestDecisionItem(BaseModel):
    """처리자의 현재 정보가 아니라 판정 당시 DB에 보존한 사실."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    actor_user_id: str
    actor_email: str
    decided_at: UtcDateTime
    outcome: Literal[SignupRequestStatus.APPROVED, SignupRequestStatus.REJECTED]
    rejected_reason: str | None = None


class SignupRequestItem(BaseModel):
    """중앙 관리자용 신청 한 건. 재시도 키는 싣지 않는다."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    #: 승인으로 만든 고객사. 옛 승인 · 거절 · 미결정은 null 이고, 고객사를 지우면 null 로 돌아간다(신청 · 결정 기록은 남는다).
    tenant_id: str | None = None
    company_name: str
    contact_name: str
    email: str
    phone: str | None = None
    note: str | None = None
    status: SignupRequestStatus
    rejected_reason: str | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    decision: SignupRequestDecisionItem | None = None


class SignupRequestsResponse(SupportPage):
    model_config = ConfigDict(extra="forbid")

    items: list[SignupRequestItem]


class SignupRequestApproveRequest(BaseModel):
    """POST /api/v1/signup-requests/{id}/approve 본문 — 만들 고객사의 AWS 계정과 리전만 받는다.

    이름 · 그룹은 받지 않는다(이름은 저장된 company_name, 그룹은 없음). 계정 · 리전 규칙은 TenantCreateRequest 와 같다.
    """

    model_config = _CONFIG

    aws_account_id: AwsAccountId
    regions: TenantRegions

    @model_validator(mode="after")
    def _enforce_contract(self) -> SignupRequestApproveRequest:
        require_unique("regions", self.regions)
        return self


class SignupRequestApproveResponse(BaseModel):
    """승인 응답 — 승인된 신청과 만들어진 고객사(POST /tenants 생성 응답 그대로 · external_id 포함)."""

    model_config = ConfigDict(extra="forbid")

    request: SignupRequestItem
    tenant: TenantCreateResponse

    @model_validator(mode="after")
    def _enforce_contract(self) -> SignupRequestApproveResponse:
        if self.request.status is not SignupRequestStatus.APPROVED:
            raise ValueError("승인 응답의 신청은 APPROVED 입니다")
        if self.request.tenant_id != self.tenant.tenant.tenant_id:
            raise ValueError("승인 응답의 신청은 만들어진 고객사를 가리킵니다")
        return self


class SignupRequestStatusRequest(BaseModel):
    """PUT /api/v1/signup-requests/{id}/status 본문. 반려 사유는 REJECTED 에서만, 그리고 반드시 받는다.

    APPROVED 는 형식상 받지만 서버가 409 SIGNUP_REQUEST_APPROVAL_REQUIRED 로 거절한다(승인은 POST approve).
    """

    model_config = _CONFIG

    status: SignupRequestStatus
    reason: _RejectionReason | None = None

    @model_validator(mode="after")
    def _reason_only_for_rejection(self) -> SignupRequestStatusRequest:
        rejected = self.status is SignupRequestStatus.REJECTED
        if rejected and self.reason is None:
            raise ValueError("반려 사유가 필요합니다")
        if not rejected and self.reason is not None:
            raise ValueError("반려 사유는 REJECTED 에서만 받습니다")
        return self
