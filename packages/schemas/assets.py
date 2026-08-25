# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# EC2/SG 자산 및 CloudWatch 메트릭의 공통 Pydantic 스키마입니다.
# collector(services/collector.py)가 원시 AWS 응답을 이 타입으로 '정형화'하고,
# rule_engine 과 routers/assets.py(GET /api/v1/assets)가 이 타입을 소비합니다.
#
# 설계 메모
#   - ARN 을 자산의 안정 키로 사용합니다(db.models.Asset.arn 과 정렬).
#   - Idle/미사용 판정·Skip 사유(SKIP_*)·health_score 는 rule_engine 의 몫이므로
#     여기서는 '수집·정형화'까지만 표현합니다. (판정 필드는 두지 않음)
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, computed_field

# 공개 계약(api/assets.py) 7종 Enum의 재노출 — 축소판(EC2·SG 2종) 중복 정의를
# 제거하고 단일 원천을 유지한다. 기존 `from schemas.assets import AssetType`
# 소비 경로는 그대로 동작한다. (Issue #48)
from .api.assets import AssetType as AssetType


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MetricName(str, Enum):
    CPU_UTILIZATION = "CPUUtilization"
    NETWORK_IN = "NetworkIn"
    NETWORK_OUT = "NetworkOut"


class MetricSeries(BaseModel):
    """단일 메트릭의 시계열. timestamps[i] 와 values[i] 가 짝을 이룬다."""
    metric_name: MetricName = Field(..., description="CloudWatch 메트릭 이름")
    timestamps: list[datetime] = Field(default_factory=list, description="정렬된 관측 시각(UTC)")
    values: list[float] = Field(default_factory=list, description="관측값(period 별 Average)")


class MetricSummary(BaseModel):
    """rule_engine 이 임계치 판정에 바로 쓰도록 뽑아둔 요약 통계."""
    cpu_datapoints: int = Field(0, description="CPU 데이터포인트 수(데이터부족 판정 근거)")
    cpu_avg: Optional[float] = Field(None, description="CPU 평균(%)")
    cpu_max: Optional[float] = Field(None, description="CPU 최대(%) — 스파이크 워크로드 식별용")
    net_in_avg: Optional[float] = Field(None, description="NetworkIn 평균(bytes)")
    net_out_avg: Optional[float] = Field(None, description="NetworkOut 평균(bytes)")


class Ec2Asset(BaseModel):
    asset_type: AssetType = Field(default=AssetType.EC2, frozen=True)
    arn: str = Field(..., description="인스턴스 ARN (안정 키)")
    instance_id: str = Field(..., description="i-xxxx")
    name: Optional[str] = Field(None, description="Name 태그")
    instance_type: Optional[str] = Field(None, description="예: t3.large")
    state: Optional[str] = Field(None, description="running/stopped 등")
    region: str = Field(..., description="리전")
    availability_zone: Optional[str] = None
    vpc_id: Optional[str] = None
    subnet_id: Optional[str] = None
    private_ip: Optional[str] = None
    launch_time: Optional[datetime] = None
    security_group_ids: list[str] = Field(default_factory=list, description="부착된 SG id")
    tags: dict[str, str] = Field(default_factory=dict)
    metrics: dict[MetricName, MetricSeries] = Field(
        default_factory=dict, description="메트릭 이름 → 시계열"
    )
    metric_summary: MetricSummary = Field(default_factory=MetricSummary)


class OpenPort(BaseModel):
    """0.0.0.0/0 또는 ::/0 로 열린 인그레스 규칙(전체개방 위협 판정 근거)."""
    protocol: str = Field(..., description="tcp/udp/all")
    from_port: Optional[int] = None
    to_port: Optional[int] = None
    ipv6: bool = Field(False, description="::/0 로 열렸는지 여부")


class SecurityGroupAsset(BaseModel):
    asset_type: AssetType = Field(default=AssetType.SG, frozen=True)
    arn: str = Field(..., description="SG ARN (안정 키)")
    group_id: str = Field(..., description="sg-xxxx")
    name: Optional[str] = Field(None, description="GroupName")
    description: Optional[str] = None
    region: str = Field(..., description="리전")
    vpc_id: Optional[str] = None
    attached: bool = Field(..., description="어떤 ENI/인스턴스에도 안 붙었으면 False(미사용 후보)")
    open_to_world: list[OpenPort] = Field(
        default_factory=list, description="전체개방 포트 목록. 비어있지 않으면 위협 후보"
    )


class NaclAsset(BaseModel):
    """Network ACL. 토폴로지 EC2→NACL(PROTECTED_BY, subnet 연관 기반) 산출용."""
    asset_type: AssetType = Field(default=AssetType.NACL, frozen=True)
    arn: str = Field(..., description="NACL ARN (안정 키)")
    nacl_id: str = Field(..., description="acl-xxxx")
    region: str = Field(..., description="리전")
    vpc_id: Optional[str] = None
    is_default: bool = Field(False, description="VPC 기본 NACL 여부")
    associated_subnet_ids: list[str] = Field(
        default_factory=list, description="이 NACL 이 연관된 subnet 목록"
    )


class EbsAsset(BaseModel):
    """EBS Volume. 토폴로지 EC2→EBS(ATTACHED_TO) 산출용이자 미부착 볼륨 정리 판정 대상.

    NACL 등과 달리 EBS 는 Rule 판정 대상(`_RULE_TARGET_TYPES`)이라, 미부착(=attached_instance_ids
    비어있음)이면 rule_engine 이 UNUSED, 부착이면 SKIP_ACTIVE 로 판정한다.
    """
    asset_type: AssetType = Field(default=AssetType.EBS, frozen=True)
    arn: str = Field(..., description="Volume ARN (안정 키)")
    volume_id: str = Field(..., description="vol-xxxx")
    region: str = Field(..., description="리전")
    volume_type: Optional[str] = Field(None, description="gp3/gp2/io1 등")
    size_gib: Optional[int] = Field(None, description="크기(GiB)")
    availability_zone: Optional[str] = None
    encrypted: Optional[bool] = None
    state: Optional[str] = Field(None, description="available(미부착)/in-use 등")
    attached_instance_ids: list[str] = Field(
        default_factory=list, description="부착된 인스턴스 목록. 비어있으면 미사용(UNUSED) 후보"
    )


class AssetInventory(BaseModel):
    """한 리전 1회 수집 결과. rule_engine 파이프라인의 입력 단위."""
    account_id: str = Field(..., description="AWS 계정 ID")
    region: str = Field(..., description="수집 리전")
    mode: str = Field(..., description="localstack | aws")
    collected_at: datetime = Field(default_factory=_utcnow, description="수집 시각(UTC)")
    lookback_days: int = Field(..., description="메트릭 조회 기간(일)")
    period_seconds: int = Field(..., description="메트릭 집계 주기(초)")
    ec2_instances: list[Ec2Asset] = Field(default_factory=list)
    security_groups: list[SecurityGroupAsset] = Field(default_factory=list)
    nacls: list[NaclAsset] = Field(default_factory=list)
    ebs_volumes: list[EbsAsset] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def orphan_sg_count(self) -> int:
        return sum(1 for s in self.security_groups if not s.attached)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def open_to_world_sg_count(self) -> int:
        return sum(1 for s in self.security_groups if s.open_to_world)
