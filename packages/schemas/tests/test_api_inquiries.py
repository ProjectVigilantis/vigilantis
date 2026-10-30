"""/api/v1/inquiries 계약 테스트 (#552) — 입력 경계 · 신원 필드 거절 · 전이 표 · 응답 직렬화."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from schemas.api.inquiries import (
    INQUIRY_TRANSITIONS,
    InquiryCreateRequest,
    InquiryEventItem,
    InquiryItem,
    InquiryNoteRequest,
    InquiryStatusRequest,
)
from schemas.api.support import InquiryStatus

VALID = {"category": "BUG", "title": "  제목  ", "body": "본문"}


def create(**overrides):
    return InquiryCreateRequest.model_validate({**VALID, **overrides})


def test_create_trims_and_defaults_optional_fields():
    request = create()
    assert request.title == "제목"
    assert request.occurred_at is None and request.page_path is None


@pytest.mark.parametrize("field", ["tenant_id", "author_user_id", "status", "inquiry_id"])
def test_create_rejects_identity_and_server_fields(field):
    with pytest.raises(ValidationError):
        create(**{field: "x"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"category": "OTHER_KIND"},
        {"title": ""},
        {"title": "   "},
        {"title": "가" * 101},
        {"body": " "},
        {"body": "a" * 1001},
        {"page_path": " "},
        {"page_path": "a" * 1001},
        {"title": "a\x00b"},
        {"body": "a\x00b"},
        {"page_path": "/a\x00"},
        {"body": "a\ud800b"},
        {"title": "\udfff"},
        {"occurred_at": "2026-01-01T00:00:00"},  # 시간대 없는 시각
    ],
)
def test_create_rejects_invalid_values(overrides):
    with pytest.raises(ValidationError):
        create(**overrides)


def test_create_accepts_boundary_lengths_and_aware_time():
    request = create(title="a" * 100, body="b" * 1000, page_path="/" + "c" * 999,
                     occurred_at="2026-01-01T09:00:00+09:00")
    assert request.occurred_at.utcoffset() is not None


def test_validation_error_does_not_echo_secret_input():
    with pytest.raises(ValidationError) as caught:
        create(body="SECRET\x00VALUE")
    assert "SECRET" not in str(caught.value.errors(include_input=False))


@pytest.mark.parametrize("bad", ["", "  ", "a\x00", "\ud800", "a" * 1001])
def test_note_rejects_blank_nul_surrogate_and_overlong(bad):
    with pytest.raises(ValidationError):
        InquiryNoteRequest(note=bad)


def test_note_is_required_and_trimmed():
    with pytest.raises(ValidationError):
        InquiryNoteRequest.model_validate({})
    assert InquiryNoteRequest(note=" 확인 ").note == "확인"


def test_status_request_note_optional_but_nonblank_when_given():
    assert InquiryStatusRequest(status="IN_PROGRESS").note is None
    assert InquiryStatusRequest(status="RESOLVED", note="처리").note == "처리"
    with pytest.raises(ValidationError):
        InquiryStatusRequest(status="RESOLVED", note=" ")
    with pytest.raises(ValidationError):
        InquiryStatusRequest(status="DONE")


def test_transition_table_is_forward_only():
    assert INQUIRY_TRANSITIONS == {
        InquiryStatus.OPEN: {InquiryStatus.IN_PROGRESS, InquiryStatus.RESOLVED},
        InquiryStatus.IN_PROGRESS: {InquiryStatus.RESOLVED},
        InquiryStatus.RESOLVED: set(),
    }


def test_response_times_serialize_as_utc_z():
    at = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    item = InquiryItem(
        inquiry_id="i", tenant_id="t", tenant_name="n", author_email="a@example.com",
        author_role="VIEWER", category="OUTAGE", title="t", body="b", occurred_at=at,
        status="OPEN", created_at=at, updated_at=at,
    )
    dumped = item.model_dump(mode="json")
    assert dumped["created_at"] == "2026-01-01T09:00:00Z"
    assert dumped["page_path"] is None
    event = InquiryEventItem(
        event_id="e", inquiry_id="i", actor_user_id="u", actor_email="staff@example.com",
        action="NOTE_ADDED", occurred_at=at,
    )
    dumped_event = event.model_dump(mode="json")
    assert dumped_event["before_status"] is None
    assert dumped_event["actor_email"] == "staff@example.com"
    with pytest.raises(ValidationError):
        InquiryEventItem(
            event_id="e", inquiry_id="i", actor_user_id="u", action="NOTE_ADDED", occurred_at=at
        )
