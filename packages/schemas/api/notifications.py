"""#552 운영자 알림 수신함 API — GET /api/v1/notifications · POST /api/v1/notifications/read.

수신함은 최신 50건만 보여 주고, 개별 읽음 요청도 그 화면 단위(50건)를 넘지 않는다.
전체 읽음은 notification_ids 를 null 로 보내며, 그 요청 시점에 권한이 있는 안 읽은 알림 전부다.
읽음 표시는 사용자별이고 문의·가입 신청 상태는 바꾸지 않는다.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .support import InquiryCategory, NotificationKind

INBOX_PAGE_SIZE = 50


class NotificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_id: str
    kind: NotificationKind
    target_id: str
    tenant_id: str | None = None
    subject: str
    summary: str
    inquiry_category: InquiryCategory | None = None
    created_at: datetime
    read_at: datetime | None = None


class NotificationsResponse(BaseModel):
    """items 는 최신 50건, unread_count 는 현재 권한 범위 전체의 안 읽은 수다."""

    model_config = ConfigDict(extra="forbid")

    items: list[NotificationItem]
    unread_count: int = Field(ge=0)


class NotificationsReadRequest(BaseModel):
    """notification_ids 가 null 이면 요청 시점에 권한이 있는 안 읽은 알림 전부, 목록이면 그 알림만이다."""

    model_config = ConfigDict(extra="forbid")

    notification_ids: list[UUID] | None = Field(default=None, max_length=INBOX_PAGE_SIZE)
