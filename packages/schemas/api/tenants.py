# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine · API 계약)
# /api/v1/tenants — 고객 계정 연결 · 원격 회수(Post-MVP T-4)의 관리자 API 외부 DTO입니다.
# 관리자 대시보드(FE)와의 공개 계약이며, **구현은 이 계약 뒤에 붙는다**(T-17 SaaS 전환
# ADR `Accepted` 뒤 — #479).
#
# 연결 방식(#438 범위 확정 코멘트 §C-1): 플랫폼 ECS 태스크 역할이 고객 계정의 역할
# (기본 `vigilantis-access`)에 `sts:AssumeRole` + `ExternalId` 로 들어간다. 장기 키는 받지 않는다.
#
# 계약 원칙 (assets.py · metrics.py 와 동일)
#   - snake_case, UTC ISO 8601("Z") 문자열, nullable 은 null 반환, extra 금지.
#   - **값의 원천을 섞지 않는다** — 운영자가 넣은 값(이름 · 계정 ID · 리전 · VPC), 플랫폼이
#     만든 값(`tenant_id` · `external_id`), 관측한 값(연결 확인 결과)을 칸으로 가른다.
#     연결 확인 결과는 「지금 연결된다」가 아니라 **그 시각에 확인한 결과**다.
#   - 상태(`status`)는 수집 · AI 분석 · 실행의 관문이다. 화면 표시가 아니라 동작을 바꾼다.
#     `SUSPENDED` 가 「원격 회수」다 — 플랫폼이 그 계정에 **더 들어가지 않는다**는 뜻이고,
#     고객 계정의 역할을 지우는 것이 아니다(고객 계정 소유라 플랫폼이 지울 수 없다).
#   - `external_id` 는 플랫폼이 테넌트를 만들 때 한 번 정하고 바꾸지 않는다. 상세 응답에는
#     칸이 없고, 생성 응답 · 연결 전 상세의 온보딩 링크 · **재발급 경로**에서만 내보낸다.
#   - T-6(고객별 자동 조치 끄기) · T-16(구독 현황 모의)의 칸은 자리만 둔다 — 값의 뜻은
#     각 카드가 정한다.
#
# 경로 (전부 /api/v1)
#   GET    /tenants                  → TenantsResponse
#   POST   /tenants                  TenantCreateRequest → TenantCreateResponse(PENDING_CONNECTION)
#   GET    /tenants/{id}             → TenantDetail
#   PATCH  /tenants/{id}             TenantUpdateRequest → TenantDetail
#   DELETE /tenants/{id}             → 204. DELETABLE_STATUSES 밖이면 TENANT_STATE_CONFLICT
#   GET    /tenants/{id}/onboarding  → TenantOnboardingResponse. 모든 상태(역할을 지운 고객의 재연결)
#   POST   /tenants/{id}/verify      → TenantVerifyResponse. VERIFIABLE_STATUSES 밖이면 TENANT_STATE_CONFLICT
#   POST   /tenants/{id}/suspend     TenantSuspendRequest → TenantDetail
#   POST   /tenants/{id}/resume      → TenantVerifyResponse(연결 확인 성공 시 ACTIVE)
#   **고객 계정에 들어가는 경로는 verify · resume 둘뿐이고, 정지(SUSPENDED) 중에는 resume 하나다.**
# ==============================================================================

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..arns import build_iam_role_arn
from .assets import UtcDateTime

AwsAccountId = Annotated[str, Field(pattern=r"^\d{12}$")]
AwsRegion = Annotated[str, Field(pattern=r"^[a-z]{2}(-[a-z]+)+-\d+$")]
VpcId = Annotated[str, Field(pattern=r"^vpc-[0-9a-f]+$")]
# IAM 역할 이름은 ASCII 만 받는다 — `\w` 는 유니코드 글자까지 받아 `role/역할` 이 통과한다
RoleArn = Annotated[str, Field(pattern=r"^arn:aws:iam::\d{12}:role/[A-Za-z0-9_+=,.@/-]+$")]
TenantName = Annotated[str, Field(min_length=1, max_length=100)]

#: 고객 역할 이름의 기본값. 온보딩 템플릿이 이 이름으로 만든다(#438 §C-1).
DEFAULT_ACCESS_ROLE_NAME = "vigilantis-access"


def default_access_role_arn(aws_account_id: str) -> str:
    return build_iam_role_arn(aws_account_id, DEFAULT_ACCESS_ROLE_NAME)


def _role_account(role_arn: str) -> str:
    return role_arn.split(":")[4]


class TenantStatus(str, Enum):
    """테넌트 상태 3종 — 수집 · AI 분석 · 실행의 관문이다.

    PENDING_CONNECTION → (연결 확인 성공) → ACTIVE ⇄ SUSPENDED.
    재개(SUSPENDED → ACTIVE)는 연결 확인이 먼저 성공해야 한다 — 정지 사이에 고객이
    역할을 지웠을 수 있기 때문이다.
    """

    #: 생성됐고 아직 연결 확인에 성공한 적이 없다. 수집 · 분석 · 실행 모두 하지 않는다.
    PENDING_CONNECTION = "PENDING_CONNECTION"
    #: 관제 중.
    ACTIVE = "ACTIVE"
    #: 원격 회수. 새 수집 · 새 분석 · 새 실행을 하지 않는다. 기존 기록은 읽을 수 있다.
    SUSPENDED = "SUSPENDED"


#: DELETE /tenants/{id} 를 받는 상태 — 연결된 적 없는 테넌트만 지운다. 계정 ID 오입력
#: (`ACCOUNT_MISMATCH`)을 고치는 길이 「지우고 새로 만든다」이기 때문이다. 연결된 적이 없으니
#: 수집 · 인시던트 기록도 없어 지워서 잃는 것이 없다. 다른 계정이면 다른 테넌트라는 원칙
#: (PATCH 가 계정 · 역할을 받지 않음)은 그대로다. 밖이면 TENANT_STATE_CONFLICT.
DELETABLE_STATUSES: frozenset[TenantStatus] = frozenset({TenantStatus.PENDING_CONNECTION})

#: POST /tenants/{id}/verify 를 받는 상태. 확인은 고객 계정에 들어가는 일이라 정지(SUSPENDED)
#: 중에는 받지 않는다 — 원격 회수가 「더 들어가지 않는다」이고, 재개 경로(resume)가 이미 확인을
#: 거친다. 정지 테넌트에 들어가는 길은 resume 하나다. 밖이면 TENANT_STATE_CONFLICT.
VERIFIABLE_STATUSES: frozenset[TenantStatus] = frozenset(
    {TenantStatus.PENDING_CONNECTION, TenantStatus.ACTIVE}
)


class TenantVerificationResult(str, Enum):
    """연결 확인 1회의 결과. 확인 순서대로 처음 실패한 자리를 싣는다.

    AssumeRole → `sts:GetCallerIdentity` → 계정 ID 대조 → 최소 읽기 1건.
    """

    OK = "OK"
    #: AssumeRole 거절 — 역할이 없거나, 신뢰 정책이 플랫폼 주체·ExternalId 를 허용하지 않는다.
    ASSUME_DENIED = "ASSUME_DENIED"
    #: assume 은 됐는데 받은 자격의 계정이 테넌트의 `aws_account_id` 와 다르다(오입력).
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    #: 계정은 맞는데 최소 읽기 권한이 없다 — 권한 정책이 붙지 않았다.
    READ_DENIED = "READ_DENIED"
    #: AWS 에 묻지 못했다(네트워크 · 스로틀링 · 5xx). 연결 여부를 판정하지 않는다.
    UNREACHABLE = "UNREACHABLE"


class TenantVerification(BaseModel):
    """마지막 연결 확인의 결과 — 관측값이다. 「지금 연결된다」로 읽지 않는다."""

    model_config = ConfigDict(extra="forbid")

    result: TenantVerificationResult
    verified_at: UtcDateTime
    #: 실패일 때 AWS 오류 코드 또는 예외 클래스명. OK 이면 null.
    reason_code: Optional[str] = Field(None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.result == TenantVerificationResult.OK and self.reason_code is not None:
            raise ValueError("OK 이면 reason_code 는 null 입니다")
        if self.result != TenantVerificationResult.OK and self.reason_code is None:
            raise ValueError("실패 결과에는 reason_code 가 필요합니다")
        return self


class TenantSubscription(BaseModel):
    """구독 현황(T-16 · 모의). 원천은 운영자 입력이다 — 결제 연동이 없다(#438 ③ 보류)."""

    model_config = ConfigDict(extra="forbid")

    plan: Optional[str] = Field(None, min_length=1, max_length=50)
    period_start: Optional[date] = None
    period_end: Optional[date] = None

    @model_validator(mode="after")
    def _enforce_contract(self):
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_end < self.period_start
        ):
            raise ValueError("period_end 는 period_start 보다 앞일 수 없습니다")
        return self


class TenantListItem(BaseModel):
    """GET /tenants 의 한 줄. 상세(TenantDetail)의 부분집합이다."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    name: TenantName
    status: TenantStatus
    aws_account_id: AwsAccountId
    last_verification: Optional[TenantVerification] = None
    #: 소멸하지 않은 자산 수 · 종료되지 않은 인시던트 수 — 목록에서 고객을 고르는 근거.
    asset_count: int = Field(ge=0)
    open_incident_count: int = Field(ge=0)


class TenantsResponse(BaseModel):
    """GET /api/v1/tenants 응답. 정렬은 created_at 오름차순(라우터 계약)."""

    model_config = ConfigDict(extra="forbid")

    tenants: list[TenantListItem] = Field(default_factory=list)


class TenantDetail(TenantListItem):
    """GET /api/v1/tenants/{tenant_id} 응답 · 변경 API 의 응답 본체."""

    access_role_arn: RoleArn
    regions: list[AwsRegion] = Field(min_length=1)
    #: 빈 목록 = 계정 안 VPC 전체. 전역 COLLECT_VPC_IDS 를 테넌트 칸으로 옮긴 것이다.
    collect_vpc_ids: list[VpcId] = Field(default_factory=list)
    #: T-6 — 끄면 자동 발동(선차단 · 1분 자동 격리)만 하지 않는다. 수집 · 분석 · 승인 실행은 그대로다.
    auto_action_enabled: bool = True
    #: PENDING_CONNECTION 일 때만 있다 — 고객이 역할을 만드는 CloudFormation 빠른 생성 링크.
    onboarding_url: Optional[str] = Field(None, pattern=r"^https://")
    suspended_at: Optional[UtcDateTime] = None
    suspended_reason: Optional[str] = Field(None, min_length=1, max_length=500)
    subscription: Optional[TenantSubscription] = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _enforce_contract(self):
        if _role_account(self.access_role_arn) != self.aws_account_id:
            raise ValueError("access_role_arn 의 계정이 aws_account_id 와 다릅니다")
        suspended = self.status == TenantStatus.SUSPENDED
        if suspended != (self.suspended_at is not None):
            raise ValueError("suspended_at 은 SUSPENDED 일 때만, 그리고 반드시 있습니다")
        if suspended != (self.suspended_reason is not None):
            raise ValueError("suspended_reason 은 SUSPENDED 일 때만, 그리고 반드시 있습니다")
        pending = self.status == TenantStatus.PENDING_CONNECTION
        if pending != (self.onboarding_url is not None):
            raise ValueError("onboarding_url 은 PENDING_CONNECTION 일 때만, 그리고 반드시 있습니다")
        # ACTIVE 는 연결 확인을 거쳐서만 된다. 그 뒤의 확인은 실패할 수 있고(고객이 역할을
        # 지움 · 일시 장애) 그것으로 상태를 자동으로 내리지 않으므로, 막는 것은
        # 「확인 기록이 아예 없는 ACTIVE」뿐이다.
        if self.status == TenantStatus.ACTIVE and self.last_verification is None:
            raise ValueError("ACTIVE 는 연결 확인 기록이 있어야 합니다")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 은 created_at 보다 앞일 수 없습니다")
        return self


class TenantCreateRequest(BaseModel):
    """POST /api/v1/tenants 요청. 만들면 PENDING_CONNECTION 이다."""

    model_config = ConfigDict(extra="forbid")

    name: TenantName
    aws_account_id: AwsAccountId
    #: 생략하면 `arn:aws:iam::<계정>:role/vigilantis-access`(온보딩 템플릿 기본값).
    access_role_arn: Optional[RoleArn] = None
    regions: list[AwsRegion] = Field(min_length=1)
    collect_vpc_ids: list[VpcId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.access_role_arn is not None and _role_account(self.access_role_arn) != self.aws_account_id:
            raise ValueError("access_role_arn 의 계정이 aws_account_id 와 다릅니다")
        if len(set(self.regions)) != len(self.regions):
            raise ValueError("regions 는 중복될 수 없습니다")
        if len(set(self.collect_vpc_ids)) != len(self.collect_vpc_ids):
            raise ValueError("collect_vpc_ids 는 중복될 수 없습니다")
        return self


class TenantCreateResponse(BaseModel):
    """POST /api/v1/tenants 응답. `external_id` 는 여기와 온보딩 링크에서만 나간다."""

    model_config = ConfigDict(extra="forbid")

    tenant: TenantDetail
    external_id: str = Field(min_length=16, max_length=128)

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.tenant.status != TenantStatus.PENDING_CONNECTION:
            raise ValueError("새 테넌트는 PENDING_CONNECTION 입니다")
        return self


class TenantOnboardingResponse(BaseModel):
    """GET /api/v1/tenants/{tenant_id}/onboarding 응답 — 온보딩 링크와 ExternalId 재발급.

    **모든 상태에서 받는다.** 고객이 역할을 지우면(관제 중이든 정지 중이든) 다시 연결하려면 같은
    `external_id` 로 역할을 다시 만들어야 하는데, 상세 응답은 연결 뒤 그 값을 싣지 않는다.
    노출이 관리자가 일부러 부르는 이 한 곳으로 모이므로 「상세에는 내보내지 않는다」는 그대로다.
    `external_id` 는 재발급이지 재생성이 아니다 — 생성 때 정한 값을 그대로 돌려준다.
    """

    model_config = ConfigDict(extra="forbid")

    onboarding_url: str = Field(pattern=r"^https://")
    external_id: str = Field(min_length=16, max_length=128)


class TenantVerifyResponse(BaseModel):
    """POST /api/v1/tenants/{tenant_id}/verify · /resume 응답 — 이번 확인 결과와 바뀐 테넌트.

    verify: PENDING_CONNECTION 에서 OK 면 ACTIVE 가 된다. 그 밖에는 상태를 바꾸지 않는다 —
    ACTIVE 의 확인 실패로 자동 정지하지 않는다(정지는 사람의 판단이다). **SUSPENDED 에서는
    받지 않는다**(VERIFIABLE_STATUSES) — 정지 중 계정에 들어가는 길은 resume 하나다.
    resume: SUSPENDED 에서 확인을 먼저 하고, OK 면 ACTIVE · 실패면 SUSPENDED 그대로다.
    """

    model_config = ConfigDict(extra="forbid")

    tenant: TenantDetail
    verification: TenantVerification

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.tenant.last_verification != self.verification:
            raise ValueError("tenant.last_verification 은 이번 확인 결과와 같아야 합니다")
        return self


class TenantSuspendRequest(BaseModel):
    """POST /api/v1/tenants/{tenant_id}/suspend 요청. 사유 없는 정지는 받지 않는다."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class TenantUpdateRequest(BaseModel):
    """PATCH /api/v1/tenants/{tenant_id} 요청 — 보낸 칸만 바꾼다. 하나는 있어야 한다.

    `aws_account_id` · `access_role_arn` 은 바꾸지 않는다 — 다른 계정이면 다른 테넌트다.
    """

    model_config = ConfigDict(extra="forbid")

    name: Optional[TenantName] = None
    regions: Optional[list[AwsRegion]] = Field(None, min_length=1)
    collect_vpc_ids: Optional[list[VpcId]] = None
    auto_action_enabled: Optional[bool] = None
    subscription: Optional[TenantSubscription] = None

    @model_validator(mode="after")
    def _enforce_contract(self):
        if not self.model_fields_set:
            raise ValueError("바꿀 칸이 하나는 있어야 합니다")
        for field in self.model_fields_set:
            if field != "subscription" and getattr(self, field) is None:
                raise ValueError(f"{field} 는 null 로 보낼 수 없습니다 — 바꾸지 않으려면 빼고 보냅니다")
        for field in ("regions", "collect_vpc_ids"):
            values = getattr(self, field)
            if values is not None and len(set(values)) != len(values):
                raise ValueError(f"{field} 는 중복될 수 없습니다")
        return self
