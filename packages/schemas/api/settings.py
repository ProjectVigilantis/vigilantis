# ==============================================================================
# [파일 설명] 담당: 안성일
# T-7 고객 설정 공개 계약. 저장 · SNS 연결 · 런타임 반영 구현은 이 DTO를 소비한다.
#
# GET   /api/v1/tenants/{tenant_id}/settings → 200 TenantSettings
# PATCH /api/v1/tenants/{tenant_id}/settings
#       TenantSettingsUpdateRequest → 200 TenantSettings
#
# - 고객 ADMIN: 자기 테넌트 읽기·쓰기, 고객 OPERATOR/VIEWER: 자기 테넌트 읽기.
# - CENTRAL_ADMIN: 전체, TENANT_MANAGER: 배정 고객만. 배정 없음은 접근 없음.
#   계정·역할·범위는 #553의 인증 계약을 재사용한다. 기존 관리 router의 플랫폼
#   전용 의존성을 완화하지 않고, 저장 직전 현재 신원·범위를 다시 검사한다.
# - 미인증 401 AUTH_REQUIRED, 역할 거부 403 AUTH_FORBIDDEN, 범위 밖·없는 대상
#   404 TENANT_NOT_FOUND, 정지 고객 쓰기 409 TENANT_SUSPENDED, 입력 오류
#   422 REQUEST_VALIDATION_FAILED. 내부 실패는 공통 500 INTERNAL_ERROR.
# - PATCH 생략은 유지, protected_tags 배열은 전체 교체, []는 태그 보호 없음.
#   tags/notifications 자체의 null과 빈 PATCH는 거부한다. 채널의 null은 해제다.
# - 태그 키는 대소문자 무시, 값은 양끝 공백 제거·대소문자 무시 후 정확 일치.
#   여러 규칙은 OR, 한 규칙의 values도 OR. 저장 정책은 다음 평가부터 적용한다.
# - Slack은 쓰기 전용 비밀값이다. 응답 DTO는 URL·환경 변수명·secret 참조를 받지 않는다.
#   요청 모델 직렬화도 마스킹한다. 저장 구현만 get_secret_value()로 꺼내 사용한다.
# - email_status는 현재 주소의 연결 상태다. 새 주소는 SETUP_PENDING부터 시작한다.
#   주소 해제 시 UNSET. READY는 SNS 구독 확인·라우트 준비이며 수신함 도착은 아니다.
#   PATCH 성공은 목표 설정 저장이다. SNS 연결 실패는 SETUP_FAILED로 조회한다.
#   이전 주소의 READY를 새 주소에 승계하지 않는다.
#   SETUP_FAILED 상태에서 같은 주소를 명시한 PATCH는 연결 재시도 요청이며
#   SETUP_PENDING으로 전환한다. 다른 상태의 같은 주소는 진행 상태를 유지한다.
# - legacy_sns_configured는 기존 정적 SNS 경로의 설정 여부다. 수신자·확인·도착을
#   뜻하지 않으며 ARN은 공개하지 않는다. email_address null로 해제하되 외부 토픽은 삭제하지 않는다.
# - 응답 updated_at은 설정 변경 시각(UTC). 비동기 상태 확인 시각으로 사용하지 않는다.
# - 인증·영속 저장·공급자 처리는 이 계약만으로 구현되지 않는다.
# ==============================================================================

from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from ..slack import validate_slack_webhook_url
from .assets import UtcDateTime
from .auth import Email


class ProtectedTagRule(BaseModel):
    """키 하나와 허용 값 목록. 키는 공백을 포함한 원문을 유지한다."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    values: list[str] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def _distinct_values(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("보호 태그 값은 비교 기준으로 중복될 수 없습니다")
        return values


class EmailNotificationStatus(str, Enum):
    UNSET = "UNSET"
    SETUP_PENDING = "SETUP_PENDING"
    SETUP_FAILED = "SETUP_FAILED"
    CONFIRMATION_PENDING = "CONFIRMATION_PENDING"
    READY = "READY"


class NotificationSettings(BaseModel):
    """조회용 채널 상태. URL · SNS ARN · 구독 식별자는 공개하지 않는다."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    slack_configured: bool
    legacy_sns_configured: bool
    email_address: Email | None
    email_status: EmailNotificationStatus

    @model_validator(mode="after")
    def _email_state_matches_address(self):
        if self.legacy_sns_configured and self.email_address is not None:
            raise ValueError("기존 SNS 경로와 고객 이메일 주소는 함께 설정될 수 없습니다")
        if (self.email_address is None) != (
            self.email_status is EmailNotificationStatus.UNSET
        ):
            raise ValueError("이메일 주소가 없을 때만 UNSET이어야 합니다")
        return self


class NotificationSettingsUpdateRequest(BaseModel):
    """보낸 채널만 수정한다. null은 해제, 생략은 기존 설정 유지."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    slack_webhook_url: SecretStr | None = None
    email_address: Email | None = None

    @field_validator("slack_webhook_url")
    @classmethod
    def _valid_webhook(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            validate_slack_webhook_url(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def _has_change(self):
        if not self.model_fields_set:
            raise ValueError("변경할 알림 채널이 하나는 있어야 합니다")
        return self


def _distinct_tag_keys(rules: list[ProtectedTagRule]) -> list[ProtectedTagRule]:
    keys = [rule.key.lower() for rule in rules]
    if len(set(keys)) != len(keys):
        raise ValueError("보호 태그 키는 대소문자만 달리 중복될 수 없습니다")
    return rules


class TenantSettings(BaseModel):
    """저장된 설정 조회·수정 응답. 기본 정책도 실제 적용될 규칙으로 반환한다."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    tenant_id: UUID
    protected_tags: list[ProtectedTagRule]
    notifications: NotificationSettings
    updated_at: UtcDateTime

    _tag_keys = field_validator("protected_tags")(_distinct_tag_keys)


class TenantSettingsUpdateRequest(BaseModel):
    """테넌트·계정·공급자 식별자는 입력으로 받지 않는 부분 수정 요청."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    protected_tags: list[ProtectedTagRule] | SkipJsonSchema[None] = None
    notifications: NotificationSettingsUpdateRequest | SkipJsonSchema[None] = None

    @field_validator("protected_tags")
    @classmethod
    def _tag_keys(cls, value: list[ProtectedTagRule] | None):
        return None if value is None else _distinct_tag_keys(value)

    @model_validator(mode="after")
    def _has_changes_without_null(self):
        if not self.model_fields_set:
            raise ValueError("변경할 설정이 하나는 있어야 합니다")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field}는 null로 보낼 수 없습니다")
        return self
