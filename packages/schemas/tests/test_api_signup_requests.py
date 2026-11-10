"""#552 도입 신청 계약 — 값 규약(공백 · 길이 · 저장 불가 문자)과 반려 사유 규칙 · #572 승인 본문과 응답."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from schemas.api.signup_requests import (
    SignupRequestApproveRequest,
    SignupRequestApproveResponse,
    SignupRequestCreate,
    SignupRequestStatusRequest,
)
from schemas.api.tenants import TenantCreateRequest

VALID = {"company_name": "Acme", "contact_name": "Kim", "email": "Kim@Example.com"}


def _create(**overrides):
    return SignupRequestCreate.model_validate({**VALID, **overrides})


def test_minimal_body_keeps_email_case_and_optional_none():
    body = _create()
    assert body.email == "Kim@Example.com"
    assert body.phone is None and body.note is None


def test_strings_are_stripped_and_blank_optionals_are_none():
    body = _create(company_name="  Acme  ", contact_name="\tKim\n", phone="   ", note="")
    assert (body.company_name, body.contact_name) == ("Acme", "Kim")
    assert body.phone is None and body.note is None


def test_length_limits_apply_after_strip_in_characters():
    assert _create(company_name=" " + "가" * 100 + " ").company_name == "가" * 100
    assert _create(phone="1" * 100).phone == "1" * 100
    assert _create(note="n" * 1000).note == "n" * 1000
    for field, size in (("company_name", 101), ("contact_name", 101), ("phone", 101), ("note", 1001)):
        with pytest.raises(ValidationError):
            _create(**{field: "x" * size})
    with pytest.raises(ValidationError):
        _create(email="a" * 250 + "@b.co")


@pytest.mark.parametrize("field", ["company_name", "contact_name"])
@pytest.mark.parametrize("value", ["", "   "])
def test_required_names_cannot_be_blank(field, value):
    with pytest.raises(ValidationError):
        _create(**{field: value})


@pytest.mark.parametrize("field", ["company_name", "contact_name", "email", "phone", "note"])
@pytest.mark.parametrize("bad", ["x\x00y", "x\ud800y"])
def test_unstorable_characters_are_rejected(field, bad):
    value = "a" + bad + "@b.co" if field == "email" else bad
    with pytest.raises(ValidationError):
        _create(**{field: value})


@pytest.mark.parametrize("email", ["", "no-at", "a b@c.d", "a@b@c"])
def test_email_uses_existing_type(email):
    with pytest.raises(ValidationError):
        _create(email=email)


def test_extra_fields_forbidden_and_input_not_echoed():
    with pytest.raises(ValidationError) as err:
        _create(status="APPROVED")
    assert "APPROVED" not in str(err.value)


def test_status_request_reason_rules():
    reject = SignupRequestStatusRequest(status="REJECTED", reason="  사유  ")
    assert reject.reason == "사유"
    for status in ("IN_REVIEW", "APPROVED", "NEW"):
        assert SignupRequestStatusRequest(status=status).reason is None
        assert SignupRequestStatusRequest(status=status, reason=None).reason is None
        with pytest.raises(ValidationError):
            SignupRequestStatusRequest(status=status, reason="x")
    for bad in (None, "", "   ", "x" * 501, "a\x00b"):
        with pytest.raises(ValidationError):
            SignupRequestStatusRequest(status="REJECTED", reason=bad)
    assert len(SignupRequestStatusRequest(status="REJECTED", reason="x" * 500).reason) == 500


def test_response_field_name_is_not_accepted_on_request():
    with pytest.raises(ValidationError):
        SignupRequestStatusRequest(status="REJECTED", rejected_reason="사유")


# --- 승인 (#572 묶음 B) -------------------------------------------------------------

ACCOUNT = "123456789012"


def _accepts(model, data) -> bool:
    try:
        model.model_validate(data)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize(
    "target,accepted",
    [
        ({"aws_account_id": ACCOUNT, "regions": ["ap-northeast-2"]}, True),
        ({"aws_account_id": ACCOUNT, "regions": ["ap-northeast-2", "us-east-1"]}, True),
        ({"aws_account_id": "12345678901", "regions": ["ap-northeast-2"]}, False),
        ({"aws_account_id": "12345678901a", "regions": ["ap-northeast-2"]}, False),
        ({"aws_account_id": ACCOUNT, "regions": []}, False),
        ({"aws_account_id": ACCOUNT, "regions": ["ap-northeast-2", "ap-northeast-2"]}, False),
        ({"aws_account_id": ACCOUNT, "regions": ["Seoul"]}, False),
        ({"aws_account_id": ACCOUNT, "regions": "ap-northeast-2"}, False),
        ({"aws_account_id": ACCOUNT}, False),
        ({"regions": ["ap-northeast-2"]}, False),
    ],
)
def test_approval_and_tenant_create_judge_account_and_regions_alike(target, accepted):
    assert _accepts(SignupRequestApproveRequest, target) is accepted
    assert _accepts(TenantCreateRequest, {"name": "Acme", **target}) is accepted


@pytest.mark.parametrize("extra", [{"name": "Acme"}, {"group_id": None}, {"access_role_arn": None}])
def test_approval_takes_only_account_and_regions(extra):
    with pytest.raises(ValidationError):
        SignupRequestApproveRequest.model_validate(
            {"aws_account_id": ACCOUNT, "regions": ["ap-northeast-2"], **extra}
        )


NOW = datetime(2026, 11, 9, tzinfo=UTC)
TENANT_ID = "66666666-6666-4666-8666-666666666666"


def _approval_response(**request_overrides) -> dict:
    request = {
        "request_id": "77777777-7777-4777-8777-777777777777", "tenant_id": TENANT_ID,
        "company_name": "Acme", "contact_name": "Kim", "email": "k@example.com",
        "status": "APPROVED", "created_at": NOW, "updated_at": NOW,
        "decision": {"actor_user_id": "u", "actor_email": "a@example.com", "decided_at": NOW, "outcome": "APPROVED"},
    }
    tenant = {
        "tenant_id": TENANT_ID, "name": "Acme", "status": "PENDING_CONNECTION", "aws_account_id": ACCOUNT,
        "asset_count": 0, "open_incident_count": 0, "group_id": None,
        "access_role_arn": f"arn:aws:iam::{ACCOUNT}:role/vigilantis-access", "regions": ["ap-northeast-2"],
        "onboarding_url": "https://ap-northeast-2.console.aws.amazon.com/cloudformation/home",
        "created_at": NOW, "updated_at": NOW,
    }
    return {"request": {**request, **request_overrides}, "tenant": {"tenant": tenant, "external_id": "e" * 32}}


def test_approval_response_carries_the_approved_request_linked_to_the_created_tenant():
    body = SignupRequestApproveResponse.model_validate(_approval_response())
    assert body.request.tenant_id == body.tenant.tenant.tenant_id
    with pytest.raises(ValidationError):
        SignupRequestApproveResponse.model_validate(_approval_response(tenant_id=None))
    with pytest.raises(ValidationError):
        SignupRequestApproveResponse.model_validate(_approval_response(status="IN_REVIEW", decision=None))
