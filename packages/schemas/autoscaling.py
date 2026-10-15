# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# RUNBOOK_EC2_ENABLE_AUTOSCALING의 고정 실행 계획과 읽기 전용 관측 결과 계약입니다. (Issue #446)
#
#   생산: apps/core-api/services/aws/autoscaling.py prepare (원본 인스턴스 읽기 전용 조회)
#   저장: db.models.ActionExecution.autoscaling_plan (JSONB, 첫 AWS 변경 전에 commit, 불변)
#   소비: 같은 모듈의 execute(단계 1–4)·inspect(판정 관측)
#
# 계획은 **조치 시점의 의도**다. 실행·판정은 원본 인스턴스를 다시 읽어 의도로 삼지 않는다 —
# 원본이 나중에 바뀌어도 이 실행이 무엇을 만들기로 했는지는 바뀌지 않는다. 그래서 만들
# 자원의 이름·크기·정책 값과 시작 템플릿에 옮길 원본 구성을 전부 필수 필드로 고정한다
# (기본값을 두면 저장본에서 빠진 키가 조용히 AWS 기본값으로 바뀐다).
#
# 관측 결과(AutoscalingInspection)는 세 갈래이며 서로 섞지 않는다.
#   ready        — 계획한 자원이 이 실행의 것이고 요청한 설정 전부가 현물과 같으며 ASG
#                  인스턴스가 InService·2/2까지 갔다. 애플리케이션 서비스는 여전히 미검증이다.
#   incomplete   — AWS가 답했고, 그 답이 미완·불일치·부팅 미확인이다. 잔여 자원은 summary가 식별한다.
#   query_failed — AWS에 묻지 못했다. 성공도 부재도 아니다 — reason_code가 재시도 여부를 가른다.
# ==============================================================================

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .guardrails import PrecheckReasonCode
from .runbook_parameters import AutoScalingSize, InstanceId, SecurityGroupId

# 새 자원(AMI·스냅숏·시작 템플릿·ASG·기동 인스턴스·볼륨)에 다는 소유 표지. 이 실행과 원본
# 인스턴스를 가리킨다 — 판정은 이름이 아니라 이 두 태그로 "이 실행이 만든 것"을 가린다.
EXECUTION_TAG_KEY = "vigilantis:execution-id"
SOURCE_INSTANCE_TAG_KEY = "vigilantis:source-instance"
# 원본에 있으면 새 자원에 그대로 옮기는 태그 키. 운영 구분(Environment)과 스모크 울타리
# (vigilantis:smoke — 실 AWS 스모크 IAM 정책이 요청 태그로 요구한다)다.
PRESERVED_SOURCE_TAG_KEYS: tuple[str, ...] = ("Environment", "environment", "vigilantis:smoke")
# precheck DryRun이 EXECUTION_TAG_KEY에 싣는 값. 실행 ID가 아니라는 표지다 — 콜론이 들어 있어
# AutoscalingExecutionId 형식으로는 만들 수 없으므로, 어떤 계획도 이 값을 소유 표지로 갖지 못한다.
PRECHECK_DRY_RUN_EXECUTION_TAG_VALUE = "vigilantis:precheck-dry-run"

CPU_TARGET_PERCENT = 50
DEFAULT_INSTANCE_WARMUP_SECONDS = 300
# 새로 만드는 시작 템플릿의 첫 버전. ASG는 이 번호를 명시로 고정한다($Latest·$Default 아님)
LAUNCH_TEMPLATE_VERSION = 1
SCALING_POLICY_NAME = "vigilantis-cpu-target-50"

_Frozen = ConfigDict(extra="forbid", frozen=True, strict=True)

# ActionExecution.execution_id(UUID 문자열). AMI 이름 허용 문자 안에 들도록 영숫자·하이픈만 받는다
AutoscalingExecutionId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
]
SubnetId = Annotated[str, StringConstraints(pattern=r"^subnet-[a-f0-9]{8,17}$")]
_Text = Annotated[str, StringConstraints(min_length=1, max_length=255)]
_Enabled = Literal["enabled", "disabled"] | None


def autoscaling_image_name(instance_id: str, execution_id: str) -> str:
    """AMI 이름 — 원본과 실행으로 결정된다. 같은 실행은 늘 같은 이름, 다른 실행과는 겹치지 않는다.

    AMI 이름은 계정·리전 안에서 유일하므로, 응답이 유실돼 ImageId를 받지 못해도 판정이 이
    이름으로 그 실행의 AMI를 찾는다.
    """
    return f"vigilantis-ami-{instance_id}-{execution_id}"


class AutoscalingTag(BaseModel):
    model_config = _Frozen

    key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    value: Annotated[str, StringConstraints(max_length=256)]


class AutoscalingMetadataOptions(BaseModel):
    """원본 인스턴스의 IMDS 설정. None은 "원본이 값을 주지 않음"이며 시작 템플릿에서 뺀다."""

    model_config = _Frozen

    http_tokens: Literal["optional", "required"] | None
    http_endpoint: _Enabled
    http_put_response_hop_limit: Annotated[int, Field(ge=1, le=64)] | None
    http_protocol_ipv6: _Enabled
    instance_metadata_tags: _Enabled


class AutoscalingSourceIntent(BaseModel):
    """시작 템플릿에 옮길 원본 인스턴스 구성 — prepare 시점의 값이다.

    옮기는 것은 서비스 기동에 필요한 구성뿐이다 — 타입·서브넷·주 ENI의 SG와 퍼블릭 IP 할당·
    인스턴스 프로파일·IMDS·모니터링·테넌시. UserData(다시 돌리지 않는다)·키 페어(KeyName)·보조
    ENI·EIP·T 계열 크레딧 설정은 옮기지 않는다. **이 생략은 디스크를 정리하지 않는다** — AMI는 원본
    디스크를 그대로 복제하므로 authorized_keys·비밀값 파일 등 디스크에 있던 것은 새 인스턴스에도 있다.
    """

    model_config = _Frozen

    instance_id: InstanceId
    instance_type: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    subnet_id: SubnetId
    # 주 ENI(DeviceIndex 0)의 SG — 시작 템플릿 주 인터페이스의 Groups가 된다
    security_group_ids: tuple[SecurityGroupId, ...] = Field(min_length=1, strict=False)
    iam_instance_profile_arn: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None
    ebs_optimized: bool
    detailed_monitoring: bool
    dedicated_tenancy: bool
    # 주 ENI의 자동 퍼블릭 IPv4 — True(AWS 할당)·False(없음)·None(EIP라 서브넷 기본에 맡김)
    associate_public_ip_address: bool | None
    metadata_options: AutoscalingMetadataOptions
    preserved_tags: tuple[AutoscalingTag, ...] = Field(strict=False)

    @model_validator(mode="after")
    def _distinct_values(self):
        if len(set(self.security_group_ids)) != len(self.security_group_ids):
            raise ValueError("security_group_ids에 중복이 있습니다")
        keys = [tag.key for tag in self.preserved_tags]
        if len(set(keys)) != len(keys):
            raise ValueError("preserved_tags 키가 중복됩니다")
        if any(key not in PRESERVED_SOURCE_TAG_KEYS for key in keys):
            raise ValueError("preserved_tags는 정해진 원본 태그 키만 담습니다")
        return self


_TARGET_ARN = re.compile(r"^arn:aws[a-z-]*:ec2:[a-z0-9-]+:\d{12}:instance/(?P<instance_id>.+)$")


class AutoscalingExecutionPlan(BaseModel):
    """EC2_ENABLE_AUTOSCALING 실행 1건의 고정 계획. 첫 AWS 변경 전에 commit되고 바뀌지 않는다.

    시작 템플릿·ASG 이름은 executor의 이름 규칙(_launch_template_name·_asg_name)이 정하고,
    실행이 그 규칙과 대조한다 — 규칙을 여기에 다시 적지 않는다.
    """

    model_config = _Frozen

    target_arn: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    execution_id: AutoscalingExecutionId
    min_size: AutoScalingSize
    max_size: AutoScalingSize
    desired_capacity: AutoScalingSize
    image_name: Annotated[str, StringConstraints(min_length=3, max_length=128)]
    launch_template_name: Annotated[str, StringConstraints(min_length=3, max_length=128)]
    launch_template_version: Literal[1]
    asg_name: _Text
    scaling_policy_name: _Text
    cpu_target_percent: Literal[50]
    default_instance_warmup_seconds: Literal[300]
    source: AutoscalingSourceIntent

    @model_validator(mode="after")
    def _consistent(self):
        if self.min_size > self.max_size:
            raise ValueError("min_size는 max_size보다 클 수 없습니다")
        if self.desired_capacity != self.min_size:
            raise ValueError("desired_capacity는 min_size와 같아야 합니다")
        match = _TARGET_ARN.match(self.target_arn)
        if match is None or match.group("instance_id") != self.source.instance_id:
            raise ValueError("target_arn이 원본 인스턴스 ARN이 아닙니다")
        if self.image_name != autoscaling_image_name(self.source.instance_id, self.execution_id):
            raise ValueError("image_name이 원본·실행으로 정해지는 이름과 다릅니다")
        if self.scaling_policy_name != SCALING_POLICY_NAME:
            raise ValueError("scaling_policy_name이 고정 정책 이름과 다릅니다")
        return self

    def resource_tags(self) -> tuple[AutoscalingTag, ...]:
        """새 자원 전부에 다는 태그 — 소유 표지 2개 + 원본에서 옮긴 태그."""
        return (
            AutoscalingTag(key=EXECUTION_TAG_KEY, value=self.execution_id),
            AutoscalingTag(key=SOURCE_INSTANCE_TAG_KEY, value=self.source.instance_id),
            *self.source.preserved_tags,
        )


class AutoscalingInspection(BaseModel):
    """판정 관측 1건. 자원을 바꾸지 않고 읽은 결과다.

    reason_code는 query_failed에만 있다 — 미완(incomplete)은 AWS가 답한 관측이라 조회 실패
    사유와 섞으면 재시도 정책이 관측된 미완을 다시 묻게 된다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready", "incomplete", "query_failed"]
    summary: str = Field(min_length=1)
    reason_code: PrecheckReasonCode | None = None

    @model_validator(mode="after")
    def _reason_code_only_for_query_failure(self):
        if (self.status == "query_failed") != (self.reason_code is not None):
            raise ValueError("reason_code는 query_failed에만, 그리고 반드시 있어야 합니다")
        return self
