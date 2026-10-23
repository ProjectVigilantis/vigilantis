"""#492 PR 3: ADMIN 사용자 관리 DTO와 이력의 비밀값 차단."""

import pytest
from pydantic import ValidationError
from schemas.api.users import (
    UserEventState, UserInviteRequest, UserListItem, UserRoleRequest, UserStatusRequest,
)


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


def test_invite_accepts_explicit_tenant_without_platform_grant():
    tenant_id = "1f0c2c1e-0000-4000-8000-000000000002"
    request = UserInviteRequest(email="user@example.com", role="ADMIN", tenant_id=tenant_id)
    assert request.model_dump(mode="json")["tenant_id"] == tenant_id
    assert UserInviteRequest(email="user@example.com", role="ADMIN").tenant_id is None
    with pytest.raises(ValidationError):
        UserInviteRequest(email="user@example.com", role="ADMIN", tenant_id="invalid")
    with pytest.raises(ValidationError):
        UserInviteRequest.model_validate({
            "email": "user@example.com", "role": "ADMIN",
            "tenant_id": tenant_id, "account_type": "PLATFORM",
        })


@pytest.mark.parametrize("model,body", [
    (UserRoleRequest, {"role": "ADMIN", "tenant_id": "1f0c2c1e-0000-4000-8000-000000000002"}),
    (UserRoleRequest, {"role": "ADMIN", "account_type": "PLATFORM"}),
    (UserStatusRequest, {"status": "ACTIVE", "account_type": "PLATFORM"}),
])
def test_management_cannot_change_membership(model, body):
    with pytest.raises(ValidationError):
        model.model_validate(body)


@pytest.mark.parametrize("account_type,tenant_id,valid", [
    ("TENANT", "1f0c2c1e-0000-4000-8000-000000000002", True),
    ("TENANT", None, False),
    ("PLATFORM", "1f0c2c1e-0000-4000-8000-000000000002", False),
    ("UNASSIGNED", None, False),
])
def test_user_list_uses_same_assigned_scope_contract(account_type, tenant_id, valid):
    body = {
        "user_id": "1f0c2c1e-0000-4000-8000-000000000001",
        "email": "user@example.com", "role": "ADMIN", "status": "ACTIVE",
        "last_login_at": None, "account_type": account_type, "tenant_id": tenant_id,
    }
    if valid:
        assert UserListItem.model_validate(body).model_dump(mode="json") == body
    else:
        with pytest.raises(ValidationError):
            UserListItem.model_validate(body)
