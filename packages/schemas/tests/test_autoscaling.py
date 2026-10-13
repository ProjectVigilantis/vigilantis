"""EC2_ENABLE_AUTOSCALING 고정 실행 계획·관측 결과 계약 (Issue #446).

계획은 첫 AWS 변경 전에 JSONB로 commit되고, 재시도·판정은 저장본만 읽는다. 그래서 여기서 굳히는
것은 셋이다 — 저장본이 그대로 되살아나는가(JSON 왕복), 빠지거나 바뀐 값이 조용히 기본값으로
채워지지 않는가(전 필드 필수·엄격 타입·불변), 관측의 세 갈래가 사유 코드로 섞이지 않는가.
"""

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from schemas.autoscaling import (
    EXECUTION_TAG_KEY,
    PRECHECK_DRY_RUN_EXECUTION_TAG_VALUE,
    SCALING_POLICY_NAME,
    SOURCE_INSTANCE_TAG_KEY,
    AutoscalingExecutionId,
    AutoscalingExecutionPlan,
    AutoscalingInspection,
    autoscaling_image_name,
)
from schemas.precheck import PrecheckReasonCode

INSTANCE = "i-0abc123456789def0"
EXECUTION = "3f1c2b8a-0d5e-4c1a-9b7e-2a6f8d4c1e90"
TARGET_ARN = f"arn:aws:ec2:ap-northeast-2:123456789012:instance/{INSTANCE}"

SOURCE = {
    "instance_id": INSTANCE,
    "instance_type": "t3.micro",
    "subnet_id": "subnet-0abc123456789def0",
    "security_group_ids": ["sg-0abc123456789def0"],
    "iam_instance_profile_arn": "arn:aws:iam::123456789012:instance-profile/app",
    "ebs_optimized": False,
    "detailed_monitoring": False,
    "dedicated_tenancy": False,
    "associate_public_ip_address": True,
    "metadata_options": {
        "http_tokens": "required",
        "http_endpoint": "enabled",
        "http_put_response_hop_limit": 2,
        "http_protocol_ipv6": None,
        "instance_metadata_tags": None,
    },
    "preserved_tags": [{"key": "vigilantis:smoke", "value": "true"}],
}

PLAN = {
    "target_arn": TARGET_ARN,
    "execution_id": EXECUTION,
    "min_size": 1,
    "max_size": 3,
    "desired_capacity": 1,
    "image_name": autoscaling_image_name(INSTANCE, EXECUTION),
    "launch_template_name": f"vigilantis-lt-{INSTANCE}",
    "launch_template_version": 1,
    "asg_name": f"vigilantis-asg-{INSTANCE}",
    "scaling_policy_name": SCALING_POLICY_NAME,
    "cpu_target_percent": 50,
    "default_instance_warmup_seconds": 300,
    "source": SOURCE,
}


def plan(**overrides) -> AutoscalingExecutionPlan:
    return AutoscalingExecutionPlan.model_validate({**PLAN, **overrides})


def with_source(**overrides) -> dict:
    return {**PLAN, "source": {**SOURCE, **overrides}}


# ------------------------------------------------------------------ 저장본 왕복


def test_stored_json_restores_the_same_plan():
    """JSONB에 넣은 그대로 다시 읽혀야 재시도·판정이 같은 의도를 본다."""
    original = plan()
    stored = json.loads(json.dumps(original.model_dump(mode="json")))

    assert AutoscalingExecutionPlan.model_validate(stored) == original
    assert AutoscalingExecutionPlan.model_validate_json(original.model_dump_json()) == original


def test_plan_is_immutable_including_nested_intent():
    original = plan()
    with pytest.raises(ValidationError):
        original.min_size = 2
    with pytest.raises(ValidationError):
        original.source.instance_type = "t3.large"
    with pytest.raises(ValidationError):
        original.source.metadata_options.http_tokens = "optional"


@pytest.mark.parametrize("key", sorted(PLAN))
def test_every_plan_field_is_required(key):
    """기본값이 있으면 저장본에서 빠진 키가 조용히 다른 의도가 된다."""
    stored = {**PLAN}
    stored.pop(key)
    with pytest.raises(ValidationError):
        AutoscalingExecutionPlan.model_validate(stored)


@pytest.mark.parametrize("key", sorted(SOURCE))
def test_every_source_field_is_required(key):
    source = {**SOURCE}
    source.pop(key)
    with pytest.raises(ValidationError):
        AutoscalingExecutionPlan.model_validate({**PLAN, "source": source})


@pytest.mark.parametrize("key", sorted(SOURCE["metadata_options"]))
def test_every_metadata_field_is_required(key):
    metadata = {**SOURCE["metadata_options"]}
    metadata.pop(key)
    with pytest.raises(ValidationError):
        AutoscalingExecutionPlan.model_validate(with_source(metadata_options=metadata))


@pytest.mark.parametrize("key", ["user_data", "key_name"])
def test_uncopied_launch_settings_have_no_place_in_the_plan(key):
    """UserData·키 페어는 옮기지 않는 설정이다 — 저장본에 실려 와도 받지 않는다."""
    with pytest.raises(ValidationError):
        plan(**{key: "x"})
    with pytest.raises(ValidationError):
        AutoscalingExecutionPlan.model_validate(with_source(**{key: "x"}))


# ------------------------------------------------------------------ 크기·고정 설정


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_size": 3, "max_size": 2, "desired_capacity": 3},  # min > max
        {"desired_capacity": 2},                                 # desired ≠ min
        {"max_size": 5},                                         # 상한 4
        {"min_size": 0, "desired_capacity": 0},                  # 하한 1
        {"min_size": True, "desired_capacity": True},            # bool은 정수가 아니다
        {"max_size": "3"},                                       # 문자열 숫자를 받지 않는다
    ],
)
def test_size_contract(overrides):
    with pytest.raises(ValidationError):
        plan(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"launch_template_version": 2},
        {"cpu_target_percent": 70},
        {"default_instance_warmup_seconds": 60},
        {"scaling_policy_name": "someone-elses-policy"},
    ],
)
def test_fixed_settings_cannot_drift_in_the_stored_plan(overrides):
    with pytest.raises(ValidationError):
        plan(**overrides)


def test_target_arn_must_be_the_source_instance():
    with pytest.raises(ValidationError):
        plan(target_arn=TARGET_ARN.replace(INSTANCE, "i-0fff123456789def0"))
    with pytest.raises(ValidationError):
        plan(target_arn="arn:aws:ec2:ap-northeast-2:123456789012:volume/vol-0abc123456789def0")


def test_image_name_is_derived_from_source_and_execution():
    """응답 유실 뒤에도 판정이 이 이름으로 그 실행의 AMI를 찾는다 — 다른 이름을 담을 수 없다."""
    assert plan().image_name == f"vigilantis-ami-{INSTANCE}-{EXECUTION}"
    with pytest.raises(ValidationError):
        plan(image_name=autoscaling_image_name(INSTANCE, "another-execution"))


def test_dry_run_marker_can_never_be_a_plan_execution_id():
    """precheck DryRun의 실행 태그 값은 실행 ID가 될 수 없다 — 소유 표지가 섞이지 않는다."""
    with pytest.raises(ValidationError):
        TypeAdapter(AutoscalingExecutionId).validate_python(PRECHECK_DRY_RUN_EXECUTION_TAG_VALUE)
    with pytest.raises(ValidationError):
        plan(
            execution_id=PRECHECK_DRY_RUN_EXECUTION_TAG_VALUE,
            image_name=autoscaling_image_name(INSTANCE, PRECHECK_DRY_RUN_EXECUTION_TAG_VALUE),
        )


# ------------------------------------------------------------------ 원본 의도


@pytest.mark.parametrize(
    "overrides",
    [
        {"security_group_ids": []},
        {"security_group_ids": ["sg-0abc123456789def0", "sg-0abc123456789def0"]},
        {"preserved_tags": [{"key": "Name", "value": "app"}]},
        {"preserved_tags": [{"key": "Environment", "value": "a"}, {"key": "Environment", "value": "b"}]},
        {"subnet_id": "subnet-XYZ"},
        {"ebs_optimized": "false"},
        {"associate_public_ip_address": 1},
    ],
)
def test_source_intent_contract(overrides):
    with pytest.raises(ValidationError):
        AutoscalingExecutionPlan.model_validate(with_source(**overrides))


def test_resource_tags_are_ownership_markers_plus_preserved_source_tags():
    tags = [(tag.key, tag.value) for tag in plan().resource_tags()]
    assert tags == [
        (EXECUTION_TAG_KEY, EXECUTION),
        (SOURCE_INSTANCE_TAG_KEY, INSTANCE),
        ("vigilantis:smoke", "true"),
    ]


# ------------------------------------------------------------------ 관측 결과


@pytest.mark.parametrize("status", ["ready", "incomplete"])
def test_observed_results_carry_no_query_reason(status):
    """미완은 AWS가 답한 관측이다 — 조회 실패 사유를 실으면 재시도 정책이 관측을 다시 묻는다."""
    assert AutoscalingInspection(status=status, summary="관측").reason_code is None
    with pytest.raises(ValidationError):
        AutoscalingInspection(
            status=status, summary="관측", reason_code=PrecheckReasonCode.PRECHECK_AWS_ERROR
        )


def test_query_failure_requires_a_reason_code():
    with pytest.raises(ValidationError):
        AutoscalingInspection(status="query_failed", summary="조회 실패")
    failed = AutoscalingInspection(
        status="query_failed",
        summary="조회 실패",
        reason_code=PrecheckReasonCode.PRECHECK_UNAUTHORIZED,
    )
    assert failed.reason_code is PrecheckReasonCode.PRECHECK_UNAUTHORIZED


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "pending", "summary": "관측"},
        {"status": "ready", "summary": ""},
        {"status": "ready", "summary": "관측", "residuals": []},
    ],
)
def test_inspection_contract(payload):
    with pytest.raises(ValidationError):
        AutoscalingInspection(**payload)
