"""#552 도입 신청 API 계약 — 익명 접수(POST)와 중앙 관리자 처리(GET · PUT status).

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

from typing import Annotated

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
)


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


class SignupRequestItem(BaseModel):
    """중앙 관리자용 신청 한 건. 재시도 키는 싣지 않는다."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    company_name: str
    contact_name: str
    email: str
    phone: str | None = None
    note: str | None = None
    status: SignupRequestStatus
    rejected_reason: str | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime


class SignupRequestsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SignupRequestItem]


class SignupRequestStatusRequest(BaseModel):
    """PUT /api/v1/signup-requests/{id}/status 본문. 반려 사유는 REJECTED 에서만, 그리고 반드시 받는다."""

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
