"""TOTP 분실 복구 요청 (#515). 신원 확인 진술과 반복 요청 식별자를 받는다."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator


class TotpRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 같은 요청을 다시 보낼 때 유지한다. 새 UUID는 별도의 초기화 요청이다.
    operation_id: UUID
    # 외부 절차로 신원을 확인했다는 행위자의 진술이며, 그 자체가 신원 증명은 아니다.
    identity_verified: StrictBool

    @field_validator("identity_verified")
    @classmethod
    def require_identity_verification(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Identity verification is required before TOTP recovery.")
        return value
