"""운영자 알림 수신함 API 계약(#552) — 읽음 요청 본문과 응답 항목."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from schemas.api.notifications import (
    INBOX_PAGE_SIZE,
    NotificationItem,
    NotificationsReadRequest,
    NotificationsResponse,
)


def test_read_request_null_empty_and_ids():
    assert NotificationsReadRequest().notification_ids is None
    assert NotificationsReadRequest(notification_ids=None).notification_ids is None
    assert NotificationsReadRequest(notification_ids=[]).notification_ids == []
    one = uuid.uuid4()
    parsed = NotificationsReadRequest.model_validate({"notification_ids": [str(one)]})
    assert parsed.notification_ids == [one]


@pytest.mark.parametrize(
    "payload",
    [
        {"notification_ids": ["not-a-uuid"]},
        {"notification_ids": "x"},
        {"notification_ids": [1]},
        {"notification_ids": [str(uuid.uuid4())] * (INBOX_PAGE_SIZE + 1)},
        {"extra": 1},
    ],
)
def test_read_request_rejects_invalid_bodies(payload):
    with pytest.raises(ValidationError):
        NotificationsReadRequest.model_validate(payload)


def test_response_shape_and_unread_count_is_not_bounded_by_page():
    item = NotificationItem(
        notification_id=str(uuid.uuid4()),
        kind="SIGNUP_REQUEST",
        target_id=str(uuid.uuid4()),
        subject="학원",
        summary="가입 신청",
        created_at=datetime(2026, 10, 30, tzinfo=UTC),
    )
    assert item.tenant_id is None and item.inquiry_category is None and item.read_at is None
    resp = NotificationsResponse(items=[item], unread_count=INBOX_PAGE_SIZE * 10)
    assert resp.unread_count == 500
    with pytest.raises(ValidationError):
        NotificationsResponse(items=[], unread_count=-1)
    with pytest.raises(ValidationError):
        NotificationItem.model_validate({**item.model_dump(), "extra": 1})
