"""TOTP 복구는 명시적 신원 확인과 반복 요청 식별자를 요구한다."""

from uuid import UUID

import pytest
from pydantic import ValidationError
from schemas.api.totp_recovery import TotpRecoveryRequest

OPERATION_ID = "11111111-2222-4333-8444-555555555555"


def test_totp_recovery_requires_explicit_confirmed_identity():
    request = TotpRecoveryRequest(
        operation_id=OPERATION_ID,
        identity_verified=True,
    )
    assert request.operation_id == UUID(OPERATION_ID)
    assert request.identity_verified is True


@pytest.mark.parametrize("value", [False, None, 0, 1, "true", "false"])
def test_totp_recovery_rejects_unconfirmed_or_coerced_identity(value):
    with pytest.raises(ValidationError):
        TotpRecoveryRequest(operation_id=OPERATION_ID, identity_verified=value)


@pytest.mark.parametrize(
    "body",
    [
        {"operation_id": OPERATION_ID},
        {"identity_verified": True},
        {"operation_id": "not-a-uuid", "identity_verified": True},
        {
            "operation_id": OPERATION_ID,
            "identity_verified": True,
            "account_type": "PLATFORM",
        },
        {
            "operation_id": OPERATION_ID,
            "identity_verified": True,
            "recover_existing": True,
        },
    ],
)
def test_totp_recovery_rejects_missing_fields_and_unrelated_authority(body):
    with pytest.raises(ValidationError):
        TotpRecoveryRequest.model_validate(body)
