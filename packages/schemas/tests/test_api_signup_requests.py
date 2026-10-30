"""#552 도입 신청 계약 — 값 규약(공백 · 길이 · 저장 불가 문자)과 반려 사유 규칙."""

import pytest
from pydantic import ValidationError
from schemas.api.signup_requests import SignupRequestCreate, SignupRequestStatusRequest

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
