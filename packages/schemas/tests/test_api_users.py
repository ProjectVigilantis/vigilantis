"""#492 PR 3: ADMIN 사용자 관리 DTO와 이력의 비밀값 차단."""

import pytest
from pydantic import ValidationError
from schemas.api.users import UserEventState, UserInviteRequest, UserRoleRequest, UserStatusRequest


def test_invite_requires_explicit_existing_account_recovery():
    body = UserInviteRequest(email="user@example.com", role="VIEWER")
    assert body.recover_existing is False
    assert UserInviteRequest(
        email="user@example.com", role="OPERATOR", recover_existing=True,
    ).recover_existing is True


@pytest.mark.parametrize("model,body", [
    (UserInviteRequest, {"email": "invalid", "role": "VIEWER"}),
    (UserRoleRequest, {"role": "ROOT"}),
    (UserStatusRequest, {"status": "PENDING"}),
])
def test_management_requests_reject_invalid_contract_values(model, body):
    with pytest.raises(ValidationError):
        model.model_validate(body)


@pytest.mark.parametrize("field", ["password", "code", "token", "cookie", "temporary_password"])
def test_audit_snapshot_cannot_accept_secret_fields(field):
    with pytest.raises(ValidationError):
        UserEventState.model_validate({field: "not-stored"})


def test_audit_snapshot_uses_only_typed_values():
    assert UserEventState(role="ADMIN", status="ACTIVE").model_dump(
        mode="json", exclude_none=True,
    ) == {"role": "ADMIN", "status": "ACTIVE"}
    with pytest.raises(ValidationError):
        UserEventState(revoked_session_count=-1)
