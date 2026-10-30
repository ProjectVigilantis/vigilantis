# ==============================================================================
# /api/v1/inquiries 계약 (#552) — 고객 문의와 처리 이력(append-only).
#
#   - 테넌트·작성자는 요청 본문에 없다(extra=forbid) — 인증된 신원에서만 정한다.
#   - 표시용 tenant_name · author_email · author_role 은 조회 시점의 현재 조인 값이다(당시 스냅샷이 아니다).
#   - 상태는 앞으로만 간다: OPEN → IN_PROGRESS · RESOLVED, IN_PROGRESS → RESOLVED. 재개·같은 상태 전이는 없다.
#   - 문자열은 앞뒤 공백을 지우고 빈 값·NUL·짝 없는 서로게이트(UTF-8 저장 불가)를 거절한다.
#     검증 오류는 위치만 응답하므로 입력 값은 응답·로그에 실리지 않는다.
# ==============================================================================

from __future__ import annotations

from typing import Annotated

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from .assets import UtcDateTime
from .auth import UserRole
from .support import (
    SHORT_TEXT_MAX_LENGTH,
    SUPPORT_TEXT_MAX_LENGTH,
    InquiryCategory,
    InquiryEventAction,
    InquiryStatus,
)

#: 허용 전이(현재 상태 → 다음 상태 집합). 여기에 없는 전이는 409 INQUIRY_STATE_CONFLICT 다.
INQUIRY_TRANSITIONS: dict[InquiryStatus, frozenset[InquiryStatus]] = {
    InquiryStatus.OPEN: frozenset({InquiryStatus.IN_PROGRESS, InquiryStatus.RESOLVED}),
    InquiryStatus.IN_PROGRESS: frozenset({InquiryStatus.RESOLVED}),
    InquiryStatus.RESOLVED: frozenset(),
}


def _storable(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL 문자를 사용할 수 없습니다")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("유효한 UTF-8 문자열이어야 합니다") from exc
    return value


def _text(max_length: int):
    return Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length),
        AfterValidator(_storable),
    ]


InquiryTitle = _text(SHORT_TEXT_MAX_LENGTH)
InquiryText = _text(SUPPORT_TEXT_MAX_LENGTH)


class InquiryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: InquiryCategory
    title: InquiryTitle
    body: InquiryText
    occurred_at: AwareDatetime | None = None
    page_path: InquiryText | None = None


class InquiryStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: InquiryStatus
    #: 같은 STATUS_CHANGED 이벤트에 남는 선택 메모.
    note: InquiryText | None = None


class InquiryNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: InquiryText


class InquiryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inquiry_id: str
    tenant_id: str
    tenant_name: str
    author_email: str
    author_role: UserRole
    category: InquiryCategory
    title: str
    body: str
    occurred_at: UtcDateTime | None = None
    page_path: str | None = None
    status: InquiryStatus
    created_at: UtcDateTime
    updated_at: UtcDateTime


class InquiriesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[InquiryItem] = Field(default_factory=list)


class InquiryEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    inquiry_id: str
    actor_user_id: str
    #: 행위자의 현재 이메일(조회 시점 조인 값 — 당시 스냅샷이 아니다).
    actor_email: str
    action: InquiryEventAction
    before_status: InquiryStatus | None = None
    after_status: InquiryStatus | None = None
    note: str | None = None
    occurred_at: UtcDateTime


class InquiryEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[InquiryEventItem] = Field(default_factory=list)
