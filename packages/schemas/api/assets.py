# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# GET /api/v1/assets 외부 응답 DTO입니다. Dashboard(FE)와의 공개 계약이며,
# 수집·정형화 계층(packages/schemas/assets.py)과 분리된 API 표현 계층입니다.
#
# 계약 원칙 (PROJECT_STATUS의 API 계약 + 확정 설계 4장)
#   - snake_case, UTC ISO 8601("Z") 문자열, nullable은 null 반환.
#   - ORM 객체·AWS 원본 응답을 그대로 노출하지 않는다.
#   - 교차 필드 불변식(판정 상태 ↔ verdict/skip 사유)은 DTO에서 강제한다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)


def _to_utc_z(v: datetime) -> str:
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 계약상 시각은 항상 "Z" 접미 문자열로 직렬화한다 (기본 +00:00 표기 금지).
UtcDateTime = Annotated[datetime, PlainSerializer(_to_utc_z, return_type=str, when_used="json")]


class CollectionStatus(str, Enum):
    NOT_COLLECTED = "NOT_COLLECTED"
    COLLECTING = "COLLECTING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class AssetType(str, Enum):
    EC2 = "EC2"
    SG = "SG"
    NACL = "NACL"
    EBS = "EBS"
    AUTO_SCALING_GROUP = "AUTO_SCALING_GROUP"
    LAUNCH_TEMPLATE = "LAUNCH_TEMPLATE"
    ALB_TARGET_GROUP = "ALB_TARGET_GROUP"


class ResourceRole(str, Enum):
    PRIMARY = "PRIMARY"                  # 주요 관제 자산: EC2·SG
    RUNBOOK_SUPPORT = "RUNBOOK_SUPPORT"  # 실행·ARN Match·토폴로지 지원 자산


class EvaluationStatus(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Verdict(str, Enum):
    COST_CANDIDATE = "COST_CANDIDATE"
    THREAT = "THREAT"
    UNUSED = "UNUSED"
    SKIP = "SKIP"


class SkipReasonCode(str, Enum):
    SKIP_INSUFFICIENT_DATA = "SKIP_INSUFFICIENT_DATA"
    SKIP_PROD_PROTECTED = "SKIP_PROD_PROTECTED"
    SKIP_LOW_UTIL = "SKIP_LOW_UTIL"
    SKIP_WHITELISTED = "SKIP_WHITELISTED"
    SKIP_ACTIVE = "SKIP_ACTIVE"


class RelationType(str, Enum):
    SECURED_BY = "SECURED_BY"          # EC2 → SG
    ATTACHED_TO = "ATTACHED_TO"        # EC2 → EBS
    MEMBER_OF = "MEMBER_OF"            # EC2 → Auto Scaling Group
    USES = "USES"                      # ASG → Launch Template
    REGISTERED_IN = "REGISTERED_IN"    # EC2 → ALB Target Group
    PROTECTED_BY = "PROTECTED_BY"      # EC2 → NACL (subnet 일치 기반 파생 관계)


class OpenPortRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: str
    from_port: Optional[int] = None
    to_port: Optional[int] = None
    ipv6: bool = False


class Ec2Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_type: Optional[str] = None
    availability_zone: Optional[str] = None
    vpc_id: Optional[str] = None
    subnet_id: Optional[str] = None
    private_ip: Optional[str] = None
    tags: dict[str, str] = Field(default_factory=dict)


class SgSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: Optional[str] = None
    vpc_id: Optional[str] = None
    attached: bool
    open_to_world: list[OpenPortRule] = Field(default_factory=list)


class NaclSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vpc_id: Optional[str] = None
    is_default: bool
    associated_subnet_ids: list[str] = Field(default_factory=list)


class EbsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_type: Optional[str] = None
    size_gib: Optional[int] = None
    availability_zone: Optional[str] = None
    encrypted: Optional[bool] = None
    attached_instance_ids: list[str] = Field(default_factory=list)


class AsgSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_size: int
    max_size: int
    desired_capacity: int
    health_check_type: Optional[str] = None


class LaunchTemplateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latest_version: Optional[int] = None
    default_version: Optional[int] = None


class AlbTargetGroupSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Optional[str] = None
    port: Optional[int] = None
    target_type: Optional[str] = None
    health_check_path: Optional[str] = None


AssetSpec = Union[
    Ec2Spec, SgSpec, NaclSpec, EbsSpec, AsgSpec, LaunchTemplateSpec, AlbTargetGroupSpec
]

_SPEC_BY_TYPE: dict[AssetType, type[BaseModel]] = {
    AssetType.EC2: Ec2Spec,
    AssetType.SG: SgSpec,
    AssetType.NACL: NaclSpec,
    AssetType.EBS: EbsSpec,
    AssetType.AUTO_SCALING_GROUP: AsgSpec,
    AssetType.LAUNCH_TEMPLATE: LaunchTemplateSpec,
    AssetType.ALB_TARGET_GROUP: AlbTargetGroupSpec,
}

_PRIMARY_TYPES = {AssetType.EC2, AssetType.SG}
# Rule 판정 대상. 나머지 자산 유형은 항상 NOT_APPLICABLE이어야 한다.
_RULE_TARGET_TYPES = {AssetType.EC2, AssetType.SG, AssetType.EBS}


class AssetRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation_type: RelationType
    target_arn: str


class AssetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arn: str
    resource_id: str
    asset_type: AssetType
    resource_role: ResourceRole
    name: Optional[str] = None
    account_id: str
    region: str
    state: Optional[str] = None
    spec: AssetSpec
    relationships: list[AssetRelationship] = Field(default_factory=list)
    evaluation_status: EvaluationStatus
    health_score: Optional[int] = Field(
        None, ge=0, le=100,
        description="EC2 전용. 현재 스펙 대비 이용률 적정성(0~100 정수, 낮을수록 최적화 후보).",
    )
    verdict: Optional[Verdict] = None
    skip_reason_code: Optional[SkipReasonCode] = None
    collected_at: UtcDateTime

    @model_validator(mode="before")
    @classmethod
    def _bind_spec_to_asset_type(cls, data):
        # smart-union 오매칭 방지: asset_type이 지정한 spec 모델로만 검증한다.
        if isinstance(data, dict) and isinstance(data.get("spec"), dict):
            try:
                asset_type = AssetType(data.get("asset_type"))
            except (ValueError, TypeError):
                return data  # asset_type 오류는 필드 검증이 보고한다
            data = dict(data)
            data["spec"] = _SPEC_BY_TYPE[asset_type].model_validate(data["spec"])
        return data

    @model_validator(mode="after")
    def _enforce_contract(self):
        expected_spec = _SPEC_BY_TYPE[self.asset_type]
        if not isinstance(self.spec, expected_spec):
            raise ValueError(
                f"spec은 {self.asset_type.value}에서 {expected_spec.__name__}이어야 합니다"
            )

        expected_role = (
            ResourceRole.PRIMARY
            if self.asset_type in _PRIMARY_TYPES
            else ResourceRole.RUNBOOK_SUPPORT
        )
        if self.resource_role != expected_role:
            raise ValueError(
                f"{self.asset_type.value}의 resource_role은 {expected_role.value}이어야 합니다"
            )

        is_rule_target = self.asset_type in _RULE_TARGET_TYPES
        if is_rule_target and self.evaluation_status == EvaluationStatus.NOT_APPLICABLE:
            raise ValueError("Rule 판정 대상(EC2·SG·EBS)은 NOT_APPLICABLE일 수 없습니다")
        if not is_rule_target and self.evaluation_status != EvaluationStatus.NOT_APPLICABLE:
            raise ValueError("판정 비대상 자산의 evaluation_status는 NOT_APPLICABLE이어야 합니다")

        if self.evaluation_status == EvaluationStatus.COMPLETED:
            if self.verdict is None:
                raise ValueError("COMPLETED이면 verdict가 필수입니다")
        else:
            if (
                self.verdict is not None
                or self.health_score is not None
                or self.skip_reason_code is not None
            ):
                raise ValueError(
                    "COMPLETED가 아니면 health_score·verdict·skip_reason_code는 null이어야 합니다"
                )

        if self.verdict == Verdict.SKIP and self.skip_reason_code is None:
            raise ValueError("verdict=SKIP이면 skip_reason_code가 필수입니다")
        if self.verdict is not None and self.verdict != Verdict.SKIP and self.skip_reason_code is not None:
            raise ValueError("SKIP이 아닌 판정에서 skip_reason_code는 null이어야 합니다")

        if self.asset_type != AssetType.EC2 and self.health_score is not None:
            raise ValueError("health_score는 EC2 외 자산에서 null이어야 합니다")
        return self


class AssetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection_status: CollectionStatus
    last_collected_at: Optional[UtcDateTime] = None
    items: list[AssetItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.collection_status == CollectionStatus.NOT_COLLECTED:
            if self.last_collected_at is not None or self.items:
                raise ValueError("NOT_COLLECTED이면 last_collected_at과 items는 비어 있어야 합니다")
        return self
