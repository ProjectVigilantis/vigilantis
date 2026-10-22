# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine · API 계약)
# /api/v1/tenants — 고객 계정 연결 · 원격 회수(Post-MVP T-4)의 관리자 API 외부 DTO입니다.
# 관리자 대시보드(FE)와의 공개 계약이며, **구현은 이 계약 뒤에 붙는다**. 큰 틀의 원천은
# ADR-0012(T-17 SaaS 전환 · #491)이고, 이 계약이 ADR 과 어긋나면 계약을 고친다.
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
#     `SUSPENDED` 가 「원격 회수」다 — 플랫폼이 그 계정에 **새로 들어가지 않는다**는 뜻이고(정지 전에
#     시작된 실행의 마무리는 예외 · 아래 경로 표), 고객 계정의 역할을 지우는 것이 아니다(고객 계정
#     소유라 플랫폼이 지울 수 없다).
#   - `external_id` 는 플랫폼이 테넌트를 만들 때 한 번 정하고 바꾸지 않는다. 상세 응답에는
#     칸이 없고, 생성 응답 · 연결 전 상세의 온보딩 링크 · **재발급 경로**에서만 내보낸다.
#   - T-6(고객별 자동 조치 끄기) · T-16(구독 현황 모의)의 칸은 자리만 둔다 — 값의 뜻은
#     각 카드가 정한다.
#
# 경로 (전부 /api/v1)
#   GET    /tenants                  → TenantsResponse
#   POST   /tenants                  TenantCreateRequest → 201 TenantCreateResponse(PENDING_CONNECTION). 같은 계정이 있으면 TENANT_ACCOUNT_CONFLICT
#   GET    /tenants/{id}             → TenantDetail
#   PATCH  /tenants/{id}             TenantUpdateRequest → TenantDetail
#   DELETE /tenants/{id}             → 204. DELETABLE_STATUSES 밖이거나 그 테넌트를 가리키는 사용자 · 기록이 있으면 TENANT_STATE_CONFLICT
#   GET    /tenants/{id}/onboarding  → TenantOnboardingResponse. 모든 상태(역할을 지운 고객의 재연결)
#   온보딩 링크를 싣는 응답(생성 · 연결 전 상세 · PATCH · 재발급)은 서버에 링크 설정이 없으면 503 TENANT_ONBOARDING_UNAVAILABLE —
#   쓰기는 응답을 만든 뒤에 확정하므로 503 이면 아무것도 바뀌지 않는다. 경로 전부 ADMIN 전용이다(T-1).
#   POST   /tenants/{id}/verify      → TenantVerifyResponse. VERIFIABLE_STATUSES 밖이면 TENANT_STATE_CONFLICT
#   POST   /tenants/{id}/suspend     TenantSuspendRequest → TenantDetail. SUSPENDABLE_STATUSES 밖이면 TENANT_STATE_CONFLICT
#   POST   /tenants/{id}/resume      → TenantVerifyResponse(연결 확인 성공 시 ACTIVE). RESUMABLE_STATUSES 밖이면 TENANT_STATE_CONFLICT
#   **이 라우터에서 고객 계정에 들어가는 경로는 verify · resume 둘뿐이고, 정지(SUSPENDED) 중에는 resume
#   하나다.** 라우터 밖의 예외는 하나 — 정지 전에 시작된 실행의 마무리(종료 판정 · 자동 원복)는 정지
#   뒤에도 끝까지 돈다(ADR-0012 §3). 멈추면 고객 자산이 반쯤 바뀐 채 남는다.
#
# 기존 경로의 테넌트 범위 (ADR-0012 §3 · §4) — 응답 모양은 그대로 두고 거절만 정한다
#   - **범위 밖은 없는 것이다.** 요청의 테넌트 범위 밖 인시던트는 목록에서 빠지고, 상세 · 실행 · 종료는
#     404 INCIDENT_NOT_FOUND 다. 403 은 그 ID 가 있다는 것을 알려 주고, AUTH_FORBIDDEN 은 「역할이 허용하지
#     않음」이라 뜻도 다르다. 플랫폼 운영자는 전부 본다. 범위를 요청에 싣는 방법은 T-1(#492)이 정한다.
#   - **정지된 테넌트** — 새 실행(POST /actions/execute)은 플랫폼 운영자를 포함해 409 TENANT_SUSPENDED 다
#     (새 실행은 고객 계정에 들어가는 일이다). 종료(POST /incidents/{id}/resolve)는 고객사 사용자에게
#     409 TENANT_SUSPENDED 이고, 플랫폼 운영자에게는 받는다(해지한 고객의 열린 인시던트 정리). 조회는 그대로다.
#   - **판정 순서** — 역할(403 AUTH_FORBIDDEN · 대상을 읽기 전) → 범위(404) → 같은 Key 재요청(실행만 ·
#     200 재생 · 다른 조치면 409 IDEMPOTENCY_KEY_CONFLICT) → 정지(409 TENANT_SUSPENDED)
#     → 인시던트 · 제안 상태(409). 범위를 정지보다 먼저 봐야 범위 밖 테넌트의 정지 여부가 새지 않고,
#     재요청을 정지보다 먼저 봐야 정지 전에 접수된 실행의 재요청이 거절로 읽히지 않는다(재요청은 새 예약을
#     만들지 않는다). 재요청을 범위보다 뒤에 두는 것은 다른 테넌트의 Key 로 남의 실행 응답이 재생되지 않게
#     하려는 것이다. 종료의 재요청(이미 종료면 200)은 Key 가 아니라 인시던트 상태로 판정하므로 정지 뒤 자리다.
#   - 자산 목록(GET /assets) · 메트릭 시계열(GET /metrics/timeseries)도 범위 밖은 빠진다 — ID 로 여는
#     경로가 없어 404 자리는 없다.
#   - 실시간 이벤트(WebSocket)도 범위 밖은 보내지 않는다.
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
    #: 원격 회수. 새 수집 · 새 분석 · 새 실행을 하지 않는다. 정지 전에 시작된 실행의 마무리(종료 판정 ·
    #: 자동 원복)는 끝까지 돈다. 기존 기록은 읽을 수 있다(고객사 사용자는 조회 전용 · 모듈 머리말).
    SUSPENDED = "SUSPENDED"


#: DELETE /tenants/{id} 를 받는 상태 — 연결된 적 없는 테넌트만 지운다. 계정 ID 오입력
#: (연결 확인에서 `ASSUME_DENIED` 로 나타난다)을 고치는 길이 「지우고 새로 만든다」이기 때문이다. 연결된 적이 없으니
#: 수집 · 인시던트 기록도 없어 지워서 잃는 것이 없다. 다른 계정이면 다른 테넌트라는 원칙
#: (PATCH 가 계정 · 역할을 받지 않음)은 그대로다. 밖이면 TENANT_STATE_CONFLICT.
DELETABLE_STATUSES: frozenset[TenantStatus] = frozenset({TenantStatus.PENDING_CONNECTION})

#: POST /tenants/{id}/verify 를 받는 상태. 확인은 고객 계정에 들어가는 일이라 정지(SUSPENDED)
#: 중에는 받지 않는다 — 원격 회수가 「더 들어가지 않는다」이고, 재개 경로(resume)가 이미 확인을
#: 거친다. 정지 테넌트에 새로 들어가는 길은 resume 하나다. 밖이면 TENANT_STATE_CONFLICT.
VERIFIABLE_STATUSES: frozenset[TenantStatus] = frozenset(
    {TenantStatus.PENDING_CONNECTION, TenantStatus.ACTIVE}
)

#: POST /tenants/{id}/suspend 를 받는 상태 — 관제 중(ACTIVE)일 때만 정지한다. 연결 전(PENDING_CONNECTION)은
#: 들어간 적이 없어 회수할 것이 없고(지우면 된다), 이미 정지면 할 일이 없다. 밖이면 TENANT_STATE_CONFLICT.
SUSPENDABLE_STATUSES: frozenset[TenantStatus] = frozenset({TenantStatus.ACTIVE})

#: POST /tenants/{id}/resume 을 받는 상태 — 정지(SUSPENDED)일 때만 재개한다. 연결 전 테넌트를 ACTIVE 로
#: 올리는 길은 verify 다(같은 연결 확인이지만 「재개」가 아니다). 밖이면 TENANT_STATE_CONFLICT.
RESUMABLE_STATUSES: frozenset[TenantStatus] = frozenset({TenantStatus.SUSPENDED})


class TenantVerificationResult(str, Enum):
    """연결 확인 1회의 결과. 확인 순서대로 처음 실패한 자리를 싣는다.

    AssumeRole → `sts:GetCallerIdentity` → 계정 ID 대조 → 최소 읽기 1건.
    """

    OK = "OK"
    #: AssumeRole 거절 — 역할이 없거나, 신뢰 정책이 플랫폼 주체·ExternalId 를 허용하지 않는다.
    #: **계정 ID 오입력도 여기로 나온다** — 역할 ARN 이 그 틀린 계정을 가리켜, 그 계정에 역할이 없거나
    #: 우리를 신뢰하지 않기 때문이다. 연결 전이면 지우고 새로 만드는 것(DELETABLE_STATUSES)이 고치는 길이다.
    ASSUME_DENIED = "ASSUME_DENIED"
    #: assume 은 됐는데 받은 자격의 계정이 테넌트의 `aws_account_id` 와 다르다. **정상 경로에서는 나오지
    #: 않는 방어 확인이다** — 계약이 역할 ARN 의 계정 = `aws_account_id` 를 강제하므로(TenantDetail ·
    #: TenantCreateRequest), assume 해서 받은 자격의 계정도 같다. 이 값이 나오면 오입력이 아니라
    #: 저장값 손상이나 확인 구현 결함을 의심한다. 값은 FE · DB 와 묶여 있어 지우지 않는다.
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
    받지 않는다**(VERIFIABLE_STATUSES) — 정지 중 계정에 새로 들어가는 길은 resume 하나다.
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
