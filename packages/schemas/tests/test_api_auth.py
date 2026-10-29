"""/api/v1/auth 계약 테스트 (Post-MVP T-1, #492 PR 1) — 단계 값 · 비밀값 비노출 · 입력 경계."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from schemas.api.auth import (
    AccountType,
    CodeRequest,
    CurrentUserResponse,
    LoginRequest,
    LoginStep,
    LoginStepResponse,
    NewPasswordRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    SessionInfo,
    TotpSetupResponse,
    UserRole,
)

# 계약 원문. 코드가 아니라 이 리터럴 집합이 기대값이다.
CONTRACT_LOGIN_STEPS = {
    "NEW_PASSWORD_REQUIRED",
    "TOTP_SETUP_REQUIRED",
    "TOTP_REQUIRED",
    "EMAIL_VERIFICATION_REQUIRED",
    "COMPLETED",
}
CONTRACT_ROLES = {"ADMIN", "OPERATOR", "VIEWER"}

PASSWORD = "Pw-literal-for-test-1!"
CODE = "123456"
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def test_login_steps_match_contract_exactly():
    assert {s.value for s in LoginStep} == CONTRACT_LOGIN_STEPS


def test_roles_match_contract_exactly():
    assert {r.value for r in UserRole} == CONTRACT_ROLES


@pytest.mark.parametrize("model, data, secret", [
    (LoginRequest, {"email": "user@example.com", "password": PASSWORD}, PASSWORD),
    (NewPasswordRequest, {"new_password": PASSWORD}, PASSWORD),
    (CodeRequest, {"code": CODE}, CODE),
])
def test_secret_inputs_never_appear_in_repr_or_dump(model, data, secret):
    obj = model.model_validate(data)
    assert secret not in repr(obj)
    assert secret not in str(obj)
    assert secret not in obj.model_dump_json()
    assert secret not in json.dumps(obj.model_dump(mode="json"))


def test_secret_inputs_keep_original_value_for_server_use():
    assert LoginRequest.model_validate(
        {"email": "user@example.com", "password": PASSWORD}
    ).password.get_secret_value() == PASSWORD
    assert CodeRequest.model_validate({"code": CODE}).code.get_secret_value() == CODE


@pytest.mark.parametrize("code", ["12345", "1234567", "12345a", "١٢٣٤٥٦", " 12345", ""])
def test_code_must_be_six_ascii_digits(code):
    with pytest.raises(ValidationError):
        CodeRequest.model_validate({"code": code})


@pytest.mark.parametrize("model, data, secret", [
    (CodeRequest, {"code": "98765x"}, "98765x"),
    (LoginRequest, {"email": "bad", "password": "Leaky-Pw-" + "z" * 250}, "Leaky-Pw-"),
    (NewPasswordRequest, {"new_password": "Leaky-1!"}, "Leaky-1!"),
])
def test_validation_errors_do_not_echo_secret_input(model, data, secret):
    with pytest.raises(ValidationError) as exc:
        model.model_validate(data)
    # 로그 · 예외 문자열 경로. 구조화된 errors(include_input=True)는 원본 입력을 담는 것이
    # pydantic 설계이므로, API 응답에서 값이 빠지는지는 core-api 검증 오류 처리기 테스트가 본다.
    assert secret not in str(exc.value)


@pytest.mark.parametrize("data", [
    {"email": "not-an-email", "password": PASSWORD},
    {"email": "user@example.com", "password": ""},
    {"email": "user@example.com", "password": "x" * 257},
    {"email": "user@example.com", "password": PASSWORD, "remember_me": True},
])
def test_login_request_violations(data):
    with pytest.raises(ValidationError):
        LoginRequest.model_validate(data)


@pytest.mark.parametrize("password", ["Short-1!", "x" * 257])
def test_new_password_length_bounds(password):
    with pytest.raises(ValidationError):
        NewPasswordRequest.model_validate({"new_password": password})


@pytest.mark.parametrize("length", [11, 257])
def test_reset_uses_new_password_length_bounds(length):
    with pytest.raises(ValidationError):
        PasswordResetConfirmRequest.model_validate({
            "email": "user@example.com", "code": CODE, "new_password": "x" * length,
        })


def test_reset_preserves_values_and_hides_both_secrets():
    request = PasswordResetConfirmRequest.model_validate({
        "email": "user@example.com", "code": CODE, "new_password": PASSWORD,
    })
    assert request.new_password.get_secret_value() == PASSWORD
    assert request.code.get_secret_value() == CODE
    for serialized in (repr(request), request.model_dump_json()):
        assert PASSWORD not in serialized
        assert CODE not in serialized
    assert PasswordResetRequest(email="user@example.com").email == "user@example.com"


@pytest.mark.parametrize("code", ["12345", "1234567", "12345a", "１２３４５６"])
def test_reset_uses_existing_code_shape(code):
    with pytest.raises(ValidationError):
        PasswordResetConfirmRequest.model_validate({
            "email": "user@example.com", "code": code, "new_password": PASSWORD,
        })


def test_step_response_roundtrip_and_extra_forbidden():
    resp = LoginStepResponse.model_validate({"next_step": "TOTP_REQUIRED"})
    assert LoginStepResponse.model_validate_json(resp.model_dump_json()) == resp
    with pytest.raises(ValidationError):
        LoginStepResponse.model_validate({"next_step": "COMPLETED", "access_token": "x"})


def test_totp_setup_uri_must_carry_same_secret():
    uri = f"otpauth://totp/Vigilantis:user%40example.com?secret={SECRET}&issuer=Vigilantis"
    assert TotpSetupResponse.model_validate({"secret": SECRET, "otpauth_uri": uri}).secret == SECRET
    with pytest.raises(ValidationError):
        TotpSetupResponse.model_validate({"secret": SECRET, "otpauth_uri": "otpauth://totp/x?secret=AAAA"})
    with pytest.raises(ValidationError):
        TotpSetupResponse.model_validate({"secret": "not-base32!", "otpauth_uri": uri})


def _session(idle_offset_minutes: int) -> dict:
    issued = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)
    absolute = issued + timedelta(hours=8)
    return {
        "absolute_expires_at": absolute.isoformat().replace("+00:00", "Z"),
        "idle_expires_at": (absolute + timedelta(minutes=idle_offset_minutes)).isoformat().replace("+00:00", "Z"),
    }


def test_idle_expiry_cannot_exceed_absolute_expiry():
    assert SessionInfo.model_validate(_session(0))
    with pytest.raises(ValidationError):
        SessionInfo.model_validate(_session(1))


def test_current_user_response_roundtrip():
    resp = CurrentUserResponse.model_validate({
        "user_id": "1f0c2c1e-0000-4000-8000-000000000001",
        "email": "user@example.com",
        "role": "VIEWER",
        "account_type": "TENANT",
        "tenant_id": "1f0c2c1e-0000-4000-8000-000000000002",
        "session": _session(-470),
    })
    assert CurrentUserResponse.model_validate_json(resp.model_dump_json()) == resp


def test_account_types_match_explicit_access_contract():
    assert {kind.value for kind in AccountType} == {"PLATFORM", "TENANT", "UNASSIGNED"}


@pytest.mark.parametrize("account_type,tenant_id", [
    ("PLATFORM", None),
    ("TENANT", "1f0c2c1e-0000-4000-8000-000000000002"),
])
def test_current_user_serializes_assigned_scope(account_type, tenant_id):
    response = CurrentUserResponse.model_validate({
        "user_id": "1f0c2c1e-0000-4000-8000-000000000001",
        "email": "user@example.com", "role": "ADMIN",
        "account_type": account_type, "tenant_id": tenant_id,
        "session": _session(-470),
    })
    payload = response.model_dump(mode="json")
    assert payload["account_type"] == account_type
    assert payload["tenant_id"] == tenant_id


@pytest.mark.parametrize("scope", [
    {},
    {"account_type": "PLATFORM"},
    {"account_type": "PLATFORM", "tenant_id": "1f0c2c1e-0000-4000-8000-000000000002"},
    {"account_type": "TENANT", "tenant_id": None},
    {"account_type": "TENANT", "tenant_id": "not-a-uuid"},
    {"account_type": "UNASSIGNED", "tenant_id": None},
])
def test_current_user_rejects_missing_or_inconsistent_scope(scope):
    with pytest.raises(ValidationError):
        CurrentUserResponse.model_validate({
            "user_id": "1f0c2c1e-0000-4000-8000-000000000001",
            "email": "user@example.com", "role": "ADMIN",
            "session": _session(-470), **scope,
        })
