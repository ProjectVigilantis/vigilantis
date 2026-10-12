# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# RUNBOOK_EC2_ENABLE_AUTOSCALING의 준비·실행·관측입니다. (Issue #446)
# 공개 진입점은 executor의 같은 이름 래퍼다 — 이 모듈이 executor의 단계 기록기·ARN 해석·
# 이름 규칙을 쓰므로 executor는 이 모듈을 호출 시점에만 import한다(순환 방지).
#
# 보장을 넷으로 나눈다. 약한 쪽의 성공을 강한 쪽의 성공으로 적지 않는다.
#   ① 계획(prepare)   — 원본 인스턴스를 읽기만 해 고정 의도를 만든다. AWS를 바꾸지 않는다.
#   ② 요청 접수(execute) — 단계 1–4가 AWS에 받아들여졌다. 단계 1만 AMI available을 기다린다.
#                       ExecutionOutcome 성공은 "구성이 갖춰졌다"가 아니다.
#   ③ 관측(inspect)   — 계획한 자원이 이 실행의 것(소유 태그)이고 요청한 설정 전부가 현물과
#                       같으며 ASG 인스턴스가 InService·2/2까지 갔다. 자원을 바꾸지 않는다.
#   ④ 미검증          — 애플리케이션 서비스·트래픽(ALB·TG·DNS를 붙이지 않는다), AMI의 파일시스템
#                       일관성(NoReboot 캡처). 어느 단계도 이것을 확인했다고 말하지 않는다.
#
# 원본 인스턴스는 멈추지도 붙이지도 지우지도 않는다. 같은 이름의 기존 자원은 재사용(채택)하지
# 않고, 실패해도 만든 것을 지우는 보상을 하지 않는다 — 남은 자원은 관측 요약이 식별해 사람에게
# 넘긴다. AMI는 원본 디스크를 그대로 복제한다 — authorized_keys·비밀값 파일 등 디스크에 있던
# 것은 새 인스턴스에도 있다. 시작 템플릿에 UserData·KeyName을 옮기지 않는 것은 그 복제를 정리하지
# 않는다. 시작 템플릿 데이터·응답 본문은 로그나 요약에 싣지 않는다(ID와 이름만 남긴다).
#
# 대기는 **조회 횟수**로 묶는다(config AUTOSCALING_*_WAIT_*). 벽시계 상한이 아니다 — 조회 호출
# 시간과 SDK 재시도가 그 위에 얹힌다. 잠드는 자리는 _sleep 하나라 테스트가 갈아 끼운다.
# ==============================================================================

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, PaginationError
from pydantic import TypeAdapter, ValidationError

from config import get_settings
from schemas.autoscaling import (
    CPU_TARGET_PERCENT,
    DEFAULT_INSTANCE_WARMUP_SECONDS,
    EXECUTION_TAG_KEY,
    LAUNCH_TEMPLATE_VERSION,
    SCALING_POLICY_NAME,
    SOURCE_INSTANCE_TAG_KEY,
    AutoscalingExecutionId,
    AutoscalingExecutionPlan,
    AutoscalingInspection,
    AutoscalingMetadataOptions,
    AutoscalingSourceIntent,
    autoscaling_image_name,
)
from schemas.executions import ExecutionEffect
from schemas.precheck import PrecheckReasonCode
from schemas.runbook_parameters import Ec2EnableAutoscalingCandidateParameters

from .client import aws_client
from .errors import aws_error_code, reason_code_for
from .executor import (
    _SUMMARY_LIMIT,
    STEP_CREATE_AUTO_SCALING_GROUP,
    STEP_CREATE_IMAGE,
    STEP_CREATE_LAUNCH_TEMPLATE,
    STEP_PUT_SCALING_POLICY,
    ExecutionOutcome,
    StepRecorder,
    _abort,
    _asg_name,
    _instance,
    _launch_template_name,
    _non_empty_str,
    _preserved_source_tags,
    _rejected,
    _StepLog,
    _summarize,
    escape_filter_value,
    parse_arn,
)

logger = logging.getLogger("vigilantis.aws")

R = PrecheckReasonCode

# 이 모듈이 직접 부르는 AWS 작업 전부 — 앱 IAM 정책 거울 테스트가 _OP_* 상수로 읽는다
_OP_DESCRIBE_INSTANCES = "ec2.describe_instances"
_OP_CREATE_IMAGE = "ec2.create_image"
_OP_DESCRIBE_IMAGES = "ec2.describe_images"
_OP_DESCRIBE_SNAPSHOTS = "ec2.describe_snapshots"
_OP_CREATE_LAUNCH_TEMPLATE = "ec2.create_launch_template"
_OP_DESCRIBE_LAUNCH_TEMPLATES = "ec2.describe_launch_templates"
_OP_DESCRIBE_LAUNCH_TEMPLATE_VERSIONS = "ec2.describe_launch_template_versions"
_OP_DESCRIBE_INSTANCE_STATUS = "ec2.describe_instance_status"
_OP_CREATE_ASG = "autoscaling.create_auto_scaling_group"
_OP_DESCRIBE_ASGS = "autoscaling.describe_auto_scaling_groups"
_OP_DESCRIBE_ASG_INSTANCES = "autoscaling.describe_auto_scaling_instances"
_OP_PUT_SCALING_POLICY = "autoscaling.put_scaling_policy"
_OP_DESCRIBE_POLICIES = "autoscaling.describe_policies"

_sleep: Callable[[float], None] = time.sleep

# 다시 available로 돌아오지 않는 AMI 상태 — 더 기다리지 않는다
_IMAGE_FAILED_STATES: frozenset[str] = frozenset(
    {"invalid", "deregistered", "failed", "error", "disabled"}
)
_POLICY_TYPE = "TargetTrackingScaling"
_CPU_METRIC = "ASGAverageCPUUtilization"
# (시작 템플릿 MetadataOptions 키, AutoscalingMetadataOptions 필드)
_METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("HttpTokens", "http_tokens"),
    ("HttpEndpoint", "http_endpoint"),
    ("HttpPutResponseHopLimit", "http_put_response_hop_limit"),
    ("HttpProtocolIpv6", "http_protocol_ipv6"),
    ("InstanceMetadataTags", "instance_metadata_tags"),
)
_LIMITS = (
    "미검증: 애플리케이션 서비스·트래픽(ALB·TG·DNS 미연결) · 원본 인스턴스 유지(정지·부착·삭제 없음)"
    " · AMI는 NoReboot 캡처라 파일시스템 일관성 미보장"
)
_EXECUTION_ID = TypeAdapter(AutoscalingExecutionId)


# ------------------------------------------------------------------ 공통
def _attempts(delay: int, max_attempts: int) -> Iterator[int]:
    """조회 시도 번호를 1부터 낸다. 두 시도 **사이**에만 잠든다 — 마지막 뒤에는 잠들지 않는다."""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            _sleep(delay)
        yield attempt


def _pages(operation: Callable[..., Any], result_key: str, **params: Any) -> list:
    """NextToken이 끝날 때까지 전부 읽는다. 같은 토큰이 다시 오면 조회 실패다(무한 반복 방지)."""
    items: list = []
    seen: set[str] = set()
    while True:
        page = operation(**params)
        items.extend(page.get(result_key) or ())
        token = page.get("NextToken")
        if not token:
            return items
        if token in seen:
            raise PaginationError(message=f"{result_key} 조회에서 NextToken이 반복됐습니다")
        seen.add(token)
        params = {**params, "NextToken": token}


def _tags(items: Any) -> dict[str, Any]:
    return {
        str(tag.get("Key")): tag.get("Value")
        for tag in items or ()
        if isinstance(tag, Mapping)
    }


def _aws_tags(plan: AutoscalingExecutionPlan) -> list[dict[str, str]]:
    return [{"Key": tag.key, "Value": tag.value} for tag in plan.resource_tags()]


def _owned(tags: Mapping[str, Any], plan: AutoscalingExecutionPlan) -> bool:
    """이 실행이 만든 자원인가 — 이름이 아니라 소유 태그 두 개로 가린다."""
    return (
        tags.get(EXECUTION_TAG_KEY) == plan.execution_id
        and tags.get(SOURCE_INSTANCE_TAG_KEY) == plan.source.instance_id
    )


def _tags_complete(tags: Mapping[str, Any], plan: AutoscalingExecutionPlan) -> bool:
    return all(tags.get(tag.key) == tag.value for tag in plan.resource_tags())


def _images_named(ec2: Any, name: str) -> list[dict]:
    """이 계정의 AMI 중 이름이 **글자 그대로** 같은 것. 필터는 이스케이프하고 결과도 다시 대조한다."""
    images = _pages(
        ec2.describe_images,
        "Images",
        Owners=["self"],
        IncludeDisabled=True,
        Filters=[{"Name": "name", "Values": [escape_filter_value(name)]}],
    )
    return [image for image in images if image.get("Name") == name]


def _launch_templates_named(ec2: Any, name: str) -> list[dict]:
    templates = _pages(
        ec2.describe_launch_templates,
        "LaunchTemplates",
        Filters=[{"Name": "launch-template-name", "Values": [escape_filter_value(name)]}],
    )
    return [item for item in templates if item.get("LaunchTemplateName") == name]


def _groups_named(autoscaling: Any, name: str) -> list[dict]:
    groups = _pages(
        autoscaling.describe_auto_scaling_groups,
        "AutoScalingGroups",
        AutoScalingGroupNames=[name],
    )
    return [group for group in groups if group.get("AutoScalingGroupName") == name]


def _plan_problem(plan: AutoscalingExecutionPlan, target_arn: str) -> str | None:
    """계약 모델이 보지 못하는 결속 — 실행 대상과 executor 이름 규칙."""
    if plan.target_arn != target_arn:
        return "계획의 대상이 실행 대상과 다릅니다"
    instance_id = plan.source.instance_id
    if (
        plan.launch_template_name != _launch_template_name(instance_id)
        or plan.asg_name != _asg_name(instance_id)
    ):
        return "계획의 시작 템플릿·ASG 이름이 이름 규칙과 다릅니다"
    return None


def _launch_template_data(plan: AutoscalingExecutionPlan, image_id: str) -> dict[str, Any]:
    """계획이 정한 시작 템플릿 데이터. UserData·KeyName은 싣지 않는다.

    기동 인스턴스·볼륨 태그를 여기서 단다 — ASG가 RunInstances로 띄울 때 요청 태그가 된다.
    """
    source = plan.source
    tags = _aws_tags(plan)
    interface: dict[str, Any] = {
        "DeviceIndex": 0,
        "Groups": list(source.security_group_ids),
        "DeleteOnTermination": True,
    }
    if source.associate_public_ip_address is not None:
        interface["AssociatePublicIpAddress"] = source.associate_public_ip_address
    data: dict[str, Any] = {
        "ImageId": image_id,
        "InstanceType": source.instance_type,
        "EbsOptimized": source.ebs_optimized,
        "Monitoring": {"Enabled": source.detailed_monitoring},
        "NetworkInterfaces": [interface],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
        ],
    }
    if source.iam_instance_profile_arn is not None:
        data["IamInstanceProfile"] = {"Arn": source.iam_instance_profile_arn}
    if source.dedicated_tenancy:
        data["Placement"] = {"Tenancy": "dedicated"}
    metadata = {
        key: getattr(source.metadata_options, field)
        for key, field in _METADATA_FIELDS
        if getattr(source.metadata_options, field) is not None
    }
    if metadata:
        data["MetadataOptions"] = metadata
    return data


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == ()


def _canonical(value: Any) -> Any:
    """비교용 정규형 — 키·목록 순서와 빈 값(None·""·[]·{}) 표기 차이만 없앤다. 값은 그대로다.

    시작 템플릿 데이터의 목록(인터페이스·SG·태그 명세·태그·블록 디바이스)은 순서가 뜻을 갖지 않는다.
    """
    if isinstance(value, Mapping):
        items = ((str(key), _canonical(item)) for key, item in value.items())
        return tuple(sorted((key, item) for key, item in items if not _empty(item)))
    if isinstance(value, (list, tuple)):
        return tuple(sorted((item for item in map(_canonical, value) if not _empty(item)), key=repr))
    return value


def _lt_canonical(data: Mapping[str, Any]) -> Any:
    """시작 템플릿 데이터 **전체**의 정규형. 우리가 정한 값과 같아야 하고, 그 밖의 값은 없어야 한다.

    요청하지 않은 설정(BlockDeviceMappings·InstanceMarketOptions·인터페이스의 SubnetId 등)이나
    비어 있지 않은 UserData가 하나라도 있으면 다르다. 조회 응답의 상태 표기(MetadataOptions.State)만
    설정이 아니라서 뺀다.
    """
    data = dict(data)
    metadata = data.get("MetadataOptions")
    if isinstance(metadata, Mapping):
        data["MetadataOptions"] = {key: item for key, item in metadata.items() if key != "State"}
    return _canonical(data)


def _fields(exc: ValidationError) -> str:
    """검증 오류의 위치만 — 값은 원본 응답에서 온 것이라 싣지 않는다."""
    return ", ".join(
        sorted({".".join(str(part) for part in err["loc"]) or "모델" for err in exc.errors()})
    )


# ------------------------------------------------------------------ ① 준비 (읽기 전용)
def _primary_interface(instance: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for interface in instance.get("NetworkInterfaces") or ():
        if isinstance(interface, Mapping) and (interface.get("Attachment") or {}).get(
            "DeviceIndex"
        ) == 0:
            return interface
    return None


def _source_problem(instance: Mapping[str, Any]) -> str | None:
    state = (instance.get("State") or {}).get("Name")
    if state != "running":
        return f"원본 인스턴스가 running이 아닙니다(state={state or '알 수 없음'})"
    if instance.get("RootDeviceType") != "ebs":
        return "EBS 기반 인스턴스가 아니라 AMI로 디스크를 옮길 수 없습니다"
    if not _non_empty_str(instance.get("SubnetId")):
        return "서브넷이 없는 인스턴스입니다"
    if (instance.get("Placement") or {}).get("Tenancy") == "host":
        return "전용 호스트(host) 테넌시는 시작 템플릿으로 옮기지 않습니다"
    if _primary_interface(instance) is None:
        return "주 네트워크 인터페이스(DeviceIndex 0)를 찾지 못했습니다"
    return None


def _source_intent(instance_id: str, instance: Mapping[str, Any]) -> AutoscalingSourceIntent:
    primary = _primary_interface(instance) or {}
    association = primary.get("Association")
    if not association:
        public: bool | None = False
    elif association.get("IpOwnerId") == "amazon":
        public = True
    else:
        # EIP다 — 새 인스턴스로 옮길 수 없으므로 자동 할당 여부는 서브넷 기본에 맡긴다
        public = None
    metadata = instance.get("MetadataOptions") or {}
    return AutoscalingSourceIntent(
        instance_id=instance_id,
        instance_type=instance.get("InstanceType"),
        subnet_id=instance.get("SubnetId"),
        security_group_ids=[group.get("GroupId") for group in primary.get("Groups") or ()],
        iam_instance_profile_arn=(instance.get("IamInstanceProfile") or {}).get("Arn"),
        ebs_optimized=instance.get("EbsOptimized") is True,
        detailed_monitoring=(instance.get("Monitoring") or {}).get("State")
        in ("enabled", "pending"),
        dedicated_tenancy=(instance.get("Placement") or {}).get("Tenancy") == "dedicated",
        associate_public_ip_address=public,
        metadata_options=AutoscalingMetadataOptions(
            **{field: metadata.get(key) for key, field in _METADATA_FIELDS}
        ),
        preserved_tags=_preserved_source_tags(instance),
    )


def _not_prepared(code: PrecheckReasonCode, detail: str):
    logger.warning("autoscaling_plan_not_prepared", extra={"reason_code": code.value})
    return None, code, detail[:_SUMMARY_LIMIT]


def prepare_ec2_autoscaling_plan(
    target_arn: str, *, execution_id: str, min_size: int, max_size: int
) -> tuple[AutoscalingExecutionPlan | None, PrecheckReasonCode | None, str | None]:
    """원본 인스턴스를 읽어 고정 계획을 만든다. **AWS를 바꾸지 않는다.**

    대상은 어느 ASG에도 속하지 않은 EC2여야 한다. 소속은 이름 추정이나 원본 태그가 아니라
    describe_auto_scaling_instances의 답으로 가린다 — 다른 이름의 ASG에 이미 속한 인스턴스도
    거절한다. 만들 이름(AMI·시작 템플릿·ASG)이 이미 있어도 계획을 만들지 않는다 — 그 자원을 이
    실행의 것으로 쓰지 않는다. 조회 실패는 기존 오류 표(errors.reason_code_for)로 분류한다.
    """
    target = parse_arn(target_arn)
    if target is None or target.service != "ec2" or target.resource_type != "instance":
        return _not_prepared(R.PRECHECK_PARAM_INVALID, "target_arn이 EC2 인스턴스 ARN이 아닙니다")
    try:
        Ec2EnableAutoscalingCandidateParameters(min_size=min_size, max_size=max_size)
        _EXECUTION_ID.validate_python(execution_id)
    except ValidationError as exc:
        return _not_prepared(R.PRECHECK_PARAM_INVALID, f"계획 입력 형식 위반: {_fields(exc)}")

    instance_id = target.resource_id
    instance, code = _instance(instance_id, target.region)
    if code is not None:
        return _not_prepared(code, f"원본 인스턴스 조회 실패: {code.value}")
    problem = _source_problem(instance)
    if problem is not None:
        return _not_prepared(R.PRECHECK_INVALID_STATE, problem)

    image_name = autoscaling_image_name(instance_id, execution_id)
    template_name = _launch_template_name(instance_id)
    group_name = _asg_name(instance_id)
    try:
        autoscaling = aws_client("autoscaling", target.region)
        memberships = sorted(
            {
                str(item.get("AutoScalingGroupName"))
                for item in _pages(
                    autoscaling.describe_auto_scaling_instances,
                    "AutoScalingInstances",
                    InstanceIds=[instance_id],
                )
                if item.get("InstanceId") == instance_id
            }
        )
    except (ClientError, BotoCoreError) as exc:
        code = reason_code_for(exc)
        return _not_prepared(code, f"원본 인스턴스의 ASG 소속 조회 실패: {code.value}")
    if memberships:
        return _not_prepared(
            R.PRECHECK_INVALID_STATE,
            f"원본 인스턴스가 이미 ASG에 속해 있습니다: {', '.join(memberships)}",
        )

    try:
        ec2 = aws_client("ec2", target.region)
        taken = [
            label
            for label, exists in (
                (f"AMI {image_name}", bool(_images_named(ec2, image_name))),
                (f"시작 템플릿 {template_name}", bool(_launch_templates_named(ec2, template_name))),
                (f"ASG {group_name}", bool(_groups_named(autoscaling, group_name))),
            )
            if exists
        ]
    except (ClientError, BotoCoreError) as exc:
        code = reason_code_for(exc)
        return _not_prepared(code, f"이름 선점 조회 실패: {code.value}")
    if taken:
        return _not_prepared(
            R.PRECHECK_INVALID_STATE,
            f"같은 이름의 자원이 이미 있어 재사용하지 않습니다: {', '.join(taken)}",
        )

    try:
        plan = AutoscalingExecutionPlan(
            target_arn=target_arn,
            execution_id=execution_id,
            min_size=min_size,
            max_size=max_size,
            desired_capacity=min_size,
            image_name=image_name,
            launch_template_name=template_name,
            launch_template_version=LAUNCH_TEMPLATE_VERSION,
            asg_name=group_name,
            scaling_policy_name=SCALING_POLICY_NAME,
            cpu_target_percent=CPU_TARGET_PERCENT,
            default_instance_warmup_seconds=DEFAULT_INSTANCE_WARMUP_SECONDS,
            source=_source_intent(instance_id, instance),
        )
    except ValidationError as exc:
        return _not_prepared(
            R.PRECHECK_INVALID_STATE, f"원본 구성을 계획으로 옮길 수 없습니다: {_fields(exc)}"
        )
    return plan, None, None


# ------------------------------------------------------------------ ② 실행 (단계 1–4)
def _unconfirmed(
    log: _StepLog, code: PrecheckReasonCode, detail: str, cause: str
) -> ExecutionOutcome:
    """요청은 접수됐는데 결과를 확정하지 못했다 — 자산이 바뀌었을 수 있다(effect UNKNOWN)."""
    log.fail(RuntimeError(cause), detail=detail)
    logger.warning(
        "execution_step_failed",
        extra={"aws_operation": log.steps[-1].aws_operation, "detail": detail},
    )
    return ExecutionOutcome(
        steps=tuple(log.steps),
        reason_code=code,
        error_summary=f"{detail}: {cause}"[:_SUMMARY_LIMIT],
    )


def _wait_image_available(ec2: Any, image_id: str) -> tuple[PrecheckReasonCode, str] | None:
    """AMI가 available이 될 때까지 조회 횟수 상한 안에서 기다린다. 성공이면 None."""
    settings = get_settings()
    max_attempts = settings.AUTOSCALING_IMAGE_WAIT_MAX_ATTEMPTS
    state = "조회 전"
    for attempt in _attempts(settings.AUTOSCALING_IMAGE_WAIT_DELAY_SECONDS, max_attempts):
        try:
            images = ec2.describe_images(ImageIds=[image_id]).get("Images") or []
        except (ClientError, BotoCoreError) as exc:
            code = reason_code_for(exc)
            if code is R.PRECHECK_TARGET_NOT_FOUND:
                # 생성 직후의 조회 전파 지연 — InvalidAMIID.NotFound는 "아직"이다
                state = "조회 전파 대기"
                continue
            return code, f"AMI 상태 조회 실패({attempt}회째): {_summarize(exc)}"
        image = next((item for item in images if item.get("ImageId") == image_id), None)
        state = str(image.get("State")) if image else "응답에 없음"
        if state == "available":
            return None
        if state in _IMAGE_FAILED_STATES:
            reason = (image.get("StateReason") or {}).get("Code")
            return R.PRECHECK_INVALID_STATE, f"AMI 상태 {state}" + (f"({reason})" if reason else "")
    return (
        R.PRECHECK_AWS_ERROR,
        f"{max_attempts}회 조회 동안 available이 아님(마지막 상태 {state}) — 조회 횟수 상한 소진",
    )


def execute_ec2_enable_autoscaling(
    target_arn: str,
    *,
    plan: AutoscalingExecutionPlan,
    record_step: StepRecorder | None = None,
) -> ExecutionOutcome:
    """`RUNBOOK_EC2_ENABLE_AUTOSCALING` 실행 — AMI → 시작 템플릿 → ASG → CPU 정책. (Issue #446)

    **계획이 commit된 뒤에만 부른다**(workflows). 값은 계획에서만 온다 — 원본을 다시 읽지 않는다.

    단계마다 호출 직전 IN_PROGRESS를 기록하고, 앞 단계가 성립할 때만 뒤로 간다. 기존 자원을
    채택하지 않는다 — 같은 이름이 있으면 AWS가 거절(4xx·NOT_APPLIED)하고 그대로 실패로 닫는다.
    응답 유실(5xx·연결 끊김)은 effect UNKNOWN이며 여기서 되묻거나 지우지 않는다 — 현물 판정은
    inspect 몫이다. record_step이 던지는 예외는 막지 않는다(executor 실행 규약).

    성공은 **요청 4건이 접수됐다**까지다. ASG 인스턴스 기동·2/2·설정 일치는 inspect가 본다.
    """
    problem = _plan_problem(plan, target_arn)
    if problem is not None:
        return _rejected(problem)
    target = parse_arn(target_arn)
    source = plan.source
    tags = _aws_tags(plan)
    ec2 = aws_client("ec2", target.region)
    autoscaling = aws_client("autoscaling", target.region)
    log = _StepLog(target_arn, record_step)

    # ① AMI — 원본은 재부팅하지 않는다(NoReboot). available까지 기다려야 시작 템플릿이 쓸 수 있다
    log.begin(1, STEP_CREATE_IMAGE, _OP_CREATE_IMAGE)
    try:
        created = ec2.create_image(
            InstanceId=source.instance_id,
            Name=plan.image_name,
            Description=f"vigilantis EC2_ENABLE_AUTOSCALING {source.instance_id} {plan.execution_id}",
            NoReboot=True,
            TagSpecifications=[
                {"ResourceType": "image", "Tags": tags},
                {"ResourceType": "snapshot", "Tags": tags},
            ],
        )
    except (ClientError, BotoCoreError) as exc:
        return _abort(log, exc, detail="AMI 생성 요청 실패")
    image_id = created.get("ImageId")
    if not _non_empty_str(image_id):
        return _unconfirmed(
            log, R.PRECHECK_AWS_ERROR, "AMI 생성 결과 확인 실패", "응답에 ImageId가 없습니다"
        )
    waited = _wait_image_available(ec2, image_id)
    if waited is not None:
        code, cause = waited
        return _unconfirmed(log, code, f"AMI {image_id} available 확인 실패", cause)
    log.succeed(
        ExecutionEffect.APPLIED,
        f"AMI {image_id} available 확인({plan.image_name} · NoReboot 캡처 · 원본 {source.instance_id} 유지)",
        response=created,
    )

    # ② 시작 템플릿 — 버전 1을 ASG가 명시로 고정한다
    log.begin(2, STEP_CREATE_LAUNCH_TEMPLATE, _OP_CREATE_LAUNCH_TEMPLATE)
    try:
        created = ec2.create_launch_template(
            LaunchTemplateName=plan.launch_template_name,
            VersionDescription=f"vigilantis {plan.execution_id}",
            LaunchTemplateData=_launch_template_data(plan, image_id),
            TagSpecifications=[{"ResourceType": "launch-template", "Tags": tags}],
        )
    except (ClientError, BotoCoreError) as exc:
        return _abort(log, exc, detail="시작 템플릿 생성 실패")
    template = created.get("LaunchTemplate") or {}
    template_id = template.get("LaunchTemplateId")
    if (
        not _non_empty_str(template_id)
        or template.get("LatestVersionNumber") != plan.launch_template_version
    ):
        return _unconfirmed(
            log,
            R.PRECHECK_AWS_ERROR,
            "시작 템플릿 생성 결과 확인 실패",
            f"응답에 템플릿 ID 또는 버전 {plan.launch_template_version}이 없습니다",
        )
    warnings = [
        str(error.get("Code"))
        for error in (created.get("Warning") or {}).get("Errors") or ()
        if isinstance(error, Mapping)
    ]
    if warnings:
        # 템플릿은 만들어졌지만 AWS가 일부 값을 유효하지 않다고 알렸다 — ASG로 넘기지 않는다
        return _unconfirmed(
            log,
            R.PRECHECK_PARAM_INVALID,
            f"시작 템플릿 {template_id} 데이터 경고",
            f"AWS가 유효하지 않은 값을 알림: {', '.join(warnings)}",
        )
    log.succeed(
        ExecutionEffect.APPLIED,
        f"시작 템플릿 {template_id} v{plan.launch_template_version} 생성"
        f"({plan.launch_template_name} · AMI {image_id} · UserData 없음)",
        response=created,
    )

    # ③ ASG — 원본 서브넷, desired=min. 인스턴스 기동은 요청 접수까지만 본다
    log.begin(3, STEP_CREATE_AUTO_SCALING_GROUP, _OP_CREATE_ASG)
    try:
        response = autoscaling.create_auto_scaling_group(
            AutoScalingGroupName=plan.asg_name,
            LaunchTemplate={
                "LaunchTemplateId": template_id,
                "Version": str(plan.launch_template_version),
            },
            MinSize=plan.min_size,
            MaxSize=plan.max_size,
            DesiredCapacity=plan.desired_capacity,
            VPCZoneIdentifier=source.subnet_id,
            DefaultInstanceWarmup=plan.default_instance_warmup_seconds,
            Tags=[
                {
                    "ResourceId": plan.asg_name,
                    "ResourceType": "auto-scaling-group",
                    "Key": tag["Key"],
                    "Value": tag["Value"],
                    "PropagateAtLaunch": False,
                }
                for tag in tags
            ],
        )
    except (ClientError, BotoCoreError) as exc:
        return _abort(log, exc, detail="ASG 생성 실패")
    log.succeed(
        ExecutionEffect.APPLIED,
        f"ASG {plan.asg_name} 생성 접수(min {plan.min_size}/max {plan.max_size}"
        f"/desired {plan.desired_capacity} · 서브넷 {source.subnet_id} · 시작 템플릿 {template_id}"
        f" v{plan.launch_template_version} · 워밍업 {plan.default_instance_warmup_seconds}초)"
        " — 인스턴스 기동은 판정 몫",
        response=response,
    )

    # ④ CPU 목표 추적 정책
    log.begin(4, STEP_PUT_SCALING_POLICY, _OP_PUT_SCALING_POLICY)
    try:
        response = autoscaling.put_scaling_policy(
            AutoScalingGroupName=plan.asg_name,
            PolicyName=plan.scaling_policy_name,
            PolicyType=_POLICY_TYPE,
            TargetTrackingConfiguration={
                "PredefinedMetricSpecification": {"PredefinedMetricType": _CPU_METRIC},
                "TargetValue": float(plan.cpu_target_percent),
            },
        )
    except (ClientError, BotoCoreError) as exc:
        return _abort(log, exc, detail="CPU 목표 추적 정책 생성 실패")
    log.succeed(
        ExecutionEffect.APPLIED,
        f"CPU {plan.cpu_target_percent}% 목표 추적 정책 접수({plan.scaling_policy_name})"
        " — 구성·부팅 확인은 판정 몫, 애플리케이션 서비스는 미검증",
        response=response,
    )
    logger.info(
        "autoscaling_requested",
        extra={
            "execution_id": plan.execution_id,
            "image_id": image_id,
            "launch_template_id": template_id,
            "asg_name": plan.asg_name,
        },
    )
    return ExecutionOutcome(steps=tuple(log.steps))


# ------------------------------------------------------------------ ③ 관측 (읽기 전용)
class _Findings:
    """관측 누적 — 미완 사유와 이 실행의 잔여 자원을 따로 모은다. 남의 자원은 잔여로 세지 않는다.

    잔여 자원은 주 자원(AMI·시작 템플릿·ASG — 과금과 수동 정리의 기준)과 딸린 자원(AMI의
    스냅숏·ASG의 인스턴스)으로 나눠 모은다. 딸린 자원은 개수가 정해져 있지 않다.
    """

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.resources: list[str] = []
        self.image_id: str | None = None
        self.snapshots: list[str] = []
        self.instances: list[str] = []


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# 딸린 자원은 앞의 몇 개만 이름을 싣고 나머지는 개수와 전체 목록의 출처로 줄인다
_MEMBERS_SHOWN = 3
_RESIDUAL_LIMIT = 400


def _members(label: str, ids: list[str], whole: str) -> str:
    text = f"{label} {', '.join(ids[:_MEMBERS_SHOWN])}"
    if len(ids) > _MEMBERS_SHOWN:
        text += f" 외 {len(ids) - _MEMBERS_SHOWN}개(전체 {len(ids)}개 목록은 {whole})"
    return text


def _residual_text(found: _Findings) -> str:
    """주 자원 식별자를 먼저 전부 싣는다 — 한도가 잘라내는 것은 딸린 자원 요약의 꼬리뿐이다."""
    parts = list(found.resources)
    if found.snapshots:
        parts.append(_members("스냅숏", found.snapshots, f"AMI {found.image_id}의 블록 디바이스 매핑"))
    if found.instances:
        parts.append(_members("인스턴스", found.instances, "ASG 소속 인스턴스 목록"))
    return _clip(", ".join(parts) or "없음", _RESIDUAL_LIMIT)


def _incomplete(found: _Findings) -> AutoscalingInspection:
    return AutoscalingInspection(
        status="incomplete",
        summary=(
            f"미완: {_clip('; '.join(found.problems), 360)}"
            f" | 이 실행의 잔여 자원(보상 삭제 없음): {_residual_text(found)} | {_LIMITS}"
        ),
    )


def _inspect_image(ec2: Any, plan: AutoscalingExecutionPlan, account: str, found: _Findings):
    """이 실행의 AMI ID. 없거나 남의 것이면 None — 이유는 found에 남는다."""
    images = _images_named(ec2, plan.image_name)
    if not images:
        found.problems.append(f"AMI {plan.image_name} 없음")
        return None
    if len(images) > 1:
        found.problems.append(f"같은 이름 AMI {len(images)}개 — 하나로 특정할 수 없음")
        return None
    image = images[0]
    image_id = image.get("ImageId")
    tags = _tags(image.get("Tags"))
    if image.get("OwnerId") != account or not _owned(tags, plan):
        found.problems.append(f"AMI {image_id}는 이 실행의 것이 아님(소유자·소유 태그 불일치)")
        return None
    found.resources.append(f"AMI {image_id}")
    found.image_id = image_id
    if not _tags_complete(tags, plan):
        found.problems.append(f"AMI {image_id} 태그 불완전")
    if image.get("State") != "available":
        found.problems.append(f"AMI {image_id} 상태 {image.get('State')}")

    snapshot_ids = [
        str(mapping["Ebs"]["SnapshotId"])
        for mapping in image.get("BlockDeviceMappings") or ()
        if isinstance(mapping, Mapping)
        and isinstance(mapping.get("Ebs"), Mapping)
        and _non_empty_str(mapping["Ebs"].get("SnapshotId"))
    ]
    if not snapshot_ids:
        found.problems.append(f"AMI {image_id}에 EBS 스냅숏이 없음")
        return image_id
    snapshots = {
        item.get("SnapshotId"): item
        for item in _pages(
            ec2.describe_snapshots,
            "Snapshots",
            OwnerIds=["self"],
            Filters=[
                {"Name": "snapshot-id", "Values": [escape_filter_value(s) for s in snapshot_ids]}
            ],
        )
    }
    for snapshot_id in snapshot_ids:
        snapshot = snapshots.get(snapshot_id)
        if snapshot is None:
            found.problems.append(f"스냅숏 {snapshot_id} 없음")
            continue
        snapshot_tags = _tags(snapshot.get("Tags"))
        if not _owned(snapshot_tags, plan):
            found.problems.append(f"스냅숏 {snapshot_id}는 이 실행의 것이 아님")
            continue
        found.snapshots.append(snapshot_id)
        if not _tags_complete(snapshot_tags, plan):
            found.problems.append(f"스냅숏 {snapshot_id} 태그 불완전")
    return image_id


def _template_version_data(ec2: Any, template_id: str, version: int) -> Mapping | None:
    try:
        versions = _pages(
            ec2.describe_launch_template_versions,
            "LaunchTemplateVersions",
            LaunchTemplateId=template_id,
            Versions=[str(version)],
        )
    except ClientError as exc:
        if aws_error_code(exc).endswith("VersionNotFound"):
            return None
        raise
    for item in versions:
        if item.get("LaunchTemplateId") == template_id and item.get("VersionNumber") == version:
            return item.get("LaunchTemplateData") or {}
    return None


def _inspect_launch_template(
    ec2: Any, plan: AutoscalingExecutionPlan, image_id: str | None, found: _Findings
) -> str | None:
    """이 실행의 시작 템플릿 ID. 없거나 남의 것이면 None."""
    templates = _launch_templates_named(ec2, plan.launch_template_name)
    if not templates:
        found.problems.append(f"시작 템플릿 {plan.launch_template_name} 없음")
        return None
    template = templates[0]
    template_id = template.get("LaunchTemplateId")
    tags = _tags(template.get("Tags"))
    if not _owned(tags, plan):
        found.problems.append(
            f"시작 템플릿 {template_id}({plan.launch_template_name})는 이 실행의 것이 아님 — 선점 자원, 재사용하지 않음"
        )
        return None
    found.resources.append(f"시작 템플릿 {template_id}")
    if not _tags_complete(tags, plan):
        found.problems.append(f"시작 템플릿 {template_id} 태그 불완전")
    version = plan.launch_template_version
    data = _template_version_data(ec2, template_id, version)
    if data is None:
        found.problems.append(f"시작 템플릿 {template_id} v{version} 없음")
    elif image_id is None:
        found.problems.append(f"시작 템플릿 {template_id} v{version}의 AMI를 대조할 이 실행의 AMI가 없음")
    elif _lt_canonical(data) != _lt_canonical(_launch_template_data(plan, image_id)):
        found.problems.append(f"시작 템플릿 {template_id} v{version} 설정이 계획과 다름")
    return template_id


def _group_mismatches(
    group: Mapping[str, Any], plan: AutoscalingExecutionPlan, template_id: str | None
) -> list[str]:
    """요청한 ASG 설정 중 현물과 다른 축. desired는 min–max 안이면 정상이다(정책이 움직인다)."""
    mismatches = []
    if group.get("MinSize") != plan.min_size:
        mismatches.append(f"MinSize {group.get('MinSize')}")
    if group.get("MaxSize") != plan.max_size:
        mismatches.append(f"MaxSize {group.get('MaxSize')}")
    desired = group.get("DesiredCapacity")
    if not (isinstance(desired, int) and plan.min_size <= desired <= plan.max_size):
        mismatches.append(f"DesiredCapacity {desired}")
    subnets = {
        item.strip() for item in str(group.get("VPCZoneIdentifier") or "").split(",") if item.strip()
    }
    if subnets != {plan.source.subnet_id}:
        mismatches.append("서브넷")
    spec = group.get("LaunchTemplate") or {}
    if (
        group.get("MixedInstancesPolicy")
        or template_id is None
        or spec.get("LaunchTemplateId") != template_id
        or spec.get("Version") != str(plan.launch_template_version)
    ):
        mismatches.append("시작 템플릿·버전")
    if group.get("DefaultInstanceWarmup") != plan.default_instance_warmup_seconds:
        mismatches.append(f"DefaultInstanceWarmup {group.get('DefaultInstanceWarmup')}")
    observed = {
        (tag.get("Key"), tag.get("Value"))
        for tag in group.get("Tags") or ()
        if isinstance(tag, Mapping) and tag.get("PropagateAtLaunch") is False
    }
    if not {(tag.key, tag.value) for tag in plan.resource_tags()} <= observed:
        mismatches.append("태그")
    if group.get("Status"):
        mismatches.append(f"상태 {group.get('Status')}")
    return mismatches


def _group_problem(
    group: Mapping[str, Any], plan: AutoscalingExecutionPlan, template_id: str | None
) -> tuple[bool, str | None]:
    """(이 실행의 ASG인가, 문제 한 줄). 설정 대조와 부팅 대기의 매 조회가 같은 기준을 쓴다."""
    if not _owned(_tags(group.get("Tags")), plan):
        return False, f"ASG {plan.asg_name}는 이 실행의 것이 아님 — 선점 자원, 재사용하지 않음"
    mismatches = _group_mismatches(group, plan, template_id)
    if mismatches:
        return True, f"ASG {plan.asg_name} 설정 불일치: {', '.join(mismatches)}"
    return True, None


def _inspect_group(
    autoscaling: Any, plan: AutoscalingExecutionPlan, template_id: str | None, found: _Findings
) -> bool:
    """이 실행의 ASG가 있는가(설정 불일치여도 있으면 True — 잔여 자원이다)."""
    groups = _groups_named(autoscaling, plan.asg_name)
    if not groups:
        found.problems.append(f"ASG {plan.asg_name} 없음")
        return False
    owned, problem = _group_problem(groups[0], plan, template_id)
    if owned:
        found.resources.append(f"ASG {plan.asg_name}")
    if problem is not None:
        found.problems.append(problem)
    return owned


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _inspect_policy(autoscaling: Any, plan: AutoscalingExecutionPlan, found: _Findings) -> None:
    policies = [
        item
        for item in _pages(
            autoscaling.describe_policies,
            "ScalingPolicies",
            AutoScalingGroupName=plan.asg_name,
            PolicyNames=[plan.scaling_policy_name],
        )
        if item.get("PolicyName") == plan.scaling_policy_name
        and item.get("AutoScalingGroupName") == plan.asg_name
    ]
    if not policies:
        found.problems.append(f"정책 {plan.scaling_policy_name} 없음")
        return
    policy = policies[0]
    config = policy.get("TargetTrackingConfiguration") or {}
    metric = (config.get("PredefinedMetricSpecification") or {}).get("PredefinedMetricType")
    if not (
        policy.get("PolicyType") == _POLICY_TYPE
        and metric == _CPU_METRIC
        and not config.get("CustomizedMetricSpecification")
        and _number(config.get("TargetValue")) == float(plan.cpu_target_percent)
        and config.get("DisableScaleIn") is not True
        and policy.get("Enabled") is not False
    ):
        found.problems.append(
            f"정책 {plan.scaling_policy_name} 설정이 CPU {plan.cpu_target_percent}% 목표 추적과 다름"
        )


def _boot_state(
    ec2: Any, group: Mapping[str, Any], instances: list[Mapping[str, Any]]
) -> str | None:
    """부팅이 끝났으면 None, 아니면 마지막 관측 한 줄. 조회 실패는 예외로 올린다."""
    desired = group.get("DesiredCapacity")
    if len(instances) != desired:
        return f"소속 {len(instances)}대/희망 {desired}대"
    unsettled = [
        f"{item.get('InstanceId')}:{item.get('LifecycleState')}/{item.get('HealthStatus')}"
        for item in instances
        if item.get("LifecycleState") != "InService" or item.get("HealthStatus") != "Healthy"
    ]
    if unsettled:
        return ", ".join(unsettled)
    instance_ids = [str(item.get("InstanceId")) for item in instances]
    try:
        statuses = _pages(
            ec2.describe_instance_status,
            "InstanceStatuses",
            InstanceIds=instance_ids,
            IncludeAllInstances=True,
        )
    except ClientError as exc:
        if aws_error_code(exc) == "InvalidInstanceID.NotFound":
            # 막 뜬 인스턴스의 조회 전파 지연 — ASG가 이미 소속으로 보고한 인스턴스다
            return "인스턴스 상태 조회 전파 대기"
        raise
    by_id = {item.get("InstanceId"): item for item in statuses}
    pending = []
    for instance_id in instance_ids:
        status = by_id.get(instance_id)
        if status is None:
            pending.append(f"{instance_id}:상태 없음")
            continue
        state = (status.get("InstanceState") or {}).get("Name")
        checks = tuple(
            (status.get(key) or {}).get("Status") for key in ("SystemStatus", "InstanceStatus")
        )
        if state != "running" or checks != ("ok", "ok"):
            pending.append(f"{instance_id}:{state}/{'/'.join(map(str, checks))}")
    return ", ".join(pending) or None


def _inspect_boot(
    ec2: Any,
    autoscaling: Any,
    plan: AutoscalingExecutionPlan,
    image_id: str,
    template_id: str,
    found: _Findings,
) -> AutoscalingInspection:
    """ASG 소속 인스턴스가 전부 이 실행의 시작 템플릿 v1로 떴고 InService·Healthy·2/2인가.

    매 조회에서 새로 읽은 ASG를 **설정 대조와 같은 기준으로 다시 본다** — 대기 중에 소유 태그·
    크기·서브넷·시작 템플릿·워밍업이 바뀐 ASG로 ready를 내지 않는다.
    """
    settings = get_settings()
    max_attempts = settings.AUTOSCALING_READY_WAIT_MAX_ATTEMPTS
    last = "관측 전"
    instance_ids: list[str] = []
    for _ in _attempts(settings.AUTOSCALING_READY_WAIT_DELAY_SECONDS, max_attempts):
        groups = _groups_named(autoscaling, plan.asg_name)
        if not groups:
            found.problems.append(f"부팅 확인 중 ASG {plan.asg_name}가 사라짐")
            return _incomplete(found)
        group = groups[0]
        owned, problem = _group_problem(group, plan, template_id)
        if problem is not None:
            if not owned:
                # 처음 대조 때는 이 실행의 것이었다 — 지금은 소유를 확인할 수 없어 잔여로 세지 않는다
                found.resources.remove(f"ASG {plan.asg_name}")
            found.problems.append(f"부팅 확인 중 {problem}")
            return _incomplete(found)
        instances =[item for item in group.get("Instances") or () if isinstance(item, Mapping)]
        instance_ids = [str(item.get("InstanceId")) for item in instances]
        foreign = [
            str(item.get("InstanceId"))
            for item in instances
            if (item.get("LaunchTemplate") or {}).get("LaunchTemplateId") != template_id
            or (item.get("LaunchTemplate") or {}).get("Version") != str(plan.launch_template_version)
        ]
        if foreign:
            found.problems.append(
                f"이 실행의 시작 템플릿 v{plan.launch_template_version}로 뜨지 않은 소속 인스턴스: {', '.join(foreign)}"
            )
            return _incomplete(found)
        last = _boot_state(ec2, group, instances)
        if last is None:
            # 정책은 대기 전에 대조했다 — ready를 말하기 직전에 다시 읽는다. 조회 실패는 예외로
            # 올라가 inspect의 경계에서 query_failed가 된다
            recheck = _Findings()
            _inspect_policy(autoscaling, plan, recheck)
            if recheck.problems:
                found.problems.extend(f"부팅 확인 뒤 {problem}" for problem in recheck.problems)
                return _incomplete(found)
            return AutoscalingInspection(
                status="ready",
                summary=(
                    f"구성 확인 — AMI {image_id} · 시작 템플릿 {template_id} v{plan.launch_template_version}"
                    f" · ASG {plan.asg_name}(min {plan.min_size}/max {plan.max_size}"
                    f"/desired {group.get('DesiredCapacity')} · 서브넷 {plan.source.subnet_id}"
                    f" · 워밍업 {plan.default_instance_warmup_seconds}초)"
                    f" · CPU {plan.cpu_target_percent}% 목표 추적"
                    f" · InService·Healthy·2/2 {len(instance_ids)}대({', '.join(instance_ids)})"
                    " · 관측 시점: AMI·시작 템플릿은 대기 전, ASG·정책·인스턴스는 판정 직전"
                    f" | {_LIMITS}"
                ),
            )
    found.instances.extend(instance_ids)
    found.problems.append(
        f"부팅 미확인 — {max_attempts}회 조회 동안 InService·2/2에 이르지 않음(마지막 관측: {_clip(last, 160)})"
    )
    return _incomplete(found)


def inspect_ec2_autoscaling(plan: AutoscalingExecutionPlan) -> AutoscalingInspection:
    """계획이 지은 자원을 읽기만 해 ready·incomplete·query_failed를 가른다. (Issue #446)

    원본 인스턴스는 읽지 않는다 — 비교 기준은 계획(조치 시점의 의도)이다. 이름이 맞아도 소유
    태그가 다르면 이 실행의 자원이 아니다(선점 자원은 잔여로 세지 않는다). 조회가 실패하면
    성공도 부재도 아닌 query_failed다.
    """
    found = _Findings()
    problem = _plan_problem(plan, plan.target_arn)
    if problem is not None:
        found.problems.append(problem)
        return _incomplete(found)
    target = parse_arn(plan.target_arn)
    try:
        ec2 = aws_client("ec2", target.region)
        autoscaling = aws_client("autoscaling", target.region)
        image_id = _inspect_image(ec2, plan, target.account_id, found)
        template_id = _inspect_launch_template(ec2, plan, image_id, found)
        if _inspect_group(autoscaling, plan, template_id, found):
            _inspect_policy(autoscaling, plan, found)
        if found.problems:
            inspection = _incomplete(found)
        else:
            inspection = _inspect_boot(ec2, autoscaling, plan, image_id, template_id, found)
    except (ClientError, BotoCoreError) as exc:
        code = reason_code_for(exc)
        inspection = AutoscalingInspection(
            status="query_failed",
            reason_code=code,
            summary=(
                f"조회 실패로 판정 불가(성공·부재 아님): {_clip(_summarize(exc), 200)}"
                f" | 그때까지 확인한 이 실행의 자원: {_residual_text(found)}"
            ),
        )
    logger.info(
        "autoscaling_inspected",
        extra={"execution_id": plan.execution_id, "status": inspection.status},
    )
    return inspection
