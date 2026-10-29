"""#544 T-2: 감사 기록 조회 계약 — 행위자 노출 규칙과 발동 원천의 짝."""

import pytest
from pydantic import ValidationError
from schemas.api.audit import (
    AUDIT_EVENTS_MAX_LIMIT,
    AuditActor,
    AuditEventsResponse,
    ExecutionStartedEvent,
    IncidentResolvedEvent,
)

_PERSON = {"kind": "TENANT_USER", "email": "operator@example.com"}
_SYSTEM = {"kind": "SYSTEM", "email": None}


def _execution(**overrides):
    return {
        "action": "ACTION_EXECUTION_STARTED",
        "event_id": "ev-1",
        "occurred_at": "2026-10-29T02:00:00Z",
        "actor": _PERSON,
        "incident_id": "inc-1",
        "tenant_id": "tn-1",
        "trigger_source": "USER_APPROVAL",
        "execution_id": "ex-1",
        "runbook_id": "RUNBOOK_EC2_ISOLATE",
        "target_arn": "arn:aws:ec2:ap-northeast-2:000000000000:instance/i-0abc",
        **overrides,
    }


def _resolution(**overrides):
    return {
        "action": "INCIDENT_RESOLVED",
        "event_id": "ev-2",
        "occurred_at": "2026-10-29T02:05:00Z",
        "actor": _PERSON,
        "incident_id": "inc-1",
        "tenant_id": None,
        "resolution": "JUSTIFIED",
        **overrides,
    }


def _page(*items, next_cursor=None):
    return AuditEventsResponse.model_validate({"items": list(items), "next_cursor": next_cursor})


def test_items_are_split_by_action():
    page = _page(_execution(), _resolution(), next_cursor="c1")
    assert [type(item) for item in page.items] == [ExecutionStartedEvent, IncidentResolvedEvent]
    dumped = page.model_dump(mode="json")
    assert dumped["items"][0]["occurred_at"] == "2026-10-29T02:00:00Z"
    assert dumped["next_cursor"] == "c1"


def test_unknown_action_is_rejected():
    with pytest.raises(ValidationError):
        _page(_resolution(action="USER_INVITED"))


@pytest.mark.parametrize("actor", [
    {"kind": "TENANT_USER", "email": "a@example.com"},
    {"kind": "PLATFORM_USER", "email": "ops@example.com"},  # 플랫폼 계정이 볼 때
    {"kind": "PLATFORM_USER", "email": None},               # 고객사 계정이 볼 때
    {"kind": "SYSTEM", "email": None},
])
def test_actor_email_follows_who_is_shown(actor):
    assert AuditActor.model_validate(actor).model_dump(mode="json") == actor


@pytest.mark.parametrize("actor", [
    {"kind": "TENANT_USER", "email": None},
    {"kind": "SYSTEM", "email": "ops@example.com"},
    {"kind": "TENANT_USER", "email": "a@example.com", "user_id": "u-1"},
])
def test_actor_rejects_hidden_or_missing_identity(actor):
    with pytest.raises(ValidationError):
        AuditActor.model_validate(actor)


@pytest.mark.parametrize("trigger", [
    "PRE_MITIGATION_0_5S", "TIMEOUT_ISOLATION_1M", "AUTO_ON_FAILURE",
])
def test_automatic_starts_belong_to_system(trigger):
    assert _page(_execution(actor=_SYSTEM, trigger_source=trigger)).items[0].actor.kind == "SYSTEM"
    with pytest.raises(ValidationError):
        _page(_execution(trigger_source=trigger))


def test_user_approval_needs_a_person():
    with pytest.raises(ValidationError):
        _page(_execution(actor=_SYSTEM))


def test_resolution_is_never_automatic():
    with pytest.raises(ValidationError):
        _page(_resolution(actor=_SYSTEM))


@pytest.mark.parametrize("build,field", [
    (_resolution, "resolution_note"),
    (_execution, "validated_command"),
    (_execution, "idempotency_key"),
])
def test_items_carry_no_free_text_or_command(build, field):
    with pytest.raises(ValidationError):
        _page(build(**{field: "x"}))


def test_cursor_is_required_and_never_empty():
    with pytest.raises(ValidationError):
        AuditEventsResponse.model_validate({"items": []})
    with pytest.raises(ValidationError):
        _page(next_cursor="")
    assert _page().next_cursor is None


def test_page_size_is_capped():
    items = [_resolution(event_id=f"ev-{i}") for i in range(AUDIT_EVENTS_MAX_LIMIT + 1)]
    with pytest.raises(ValidationError):
        _page(*items)
    assert len(_page(*items[:-1]).items) == AUDIT_EVENTS_MAX_LIMIT
