"""EC2_ENABLE_AUTOSCALING 준비·실행·관측 단위 테스트 (Issue #446).

AWS 불필요. 대역은 둘이다.
  - FakeAws: 허용한 작업만 받는 상태형 대역. 표에 없는 작업(삭제·정지·부착·수정·waiter)은
    AssertionError로 끊고, 픽스처 정리 단계가 생성 4종과 조회 말고는 아무것도 부르지 않았는지
    다시 본다 — 보상 삭제·원본 조작이 없다는 것을 구조로 확인한다.
  - botocore Stubber: 실제 클라이언트에 요청 모양(파라미터 이름·형식)을 서비스 모델로 대조한다.

굳히는 경계는 보장 넷이다 — 계획(읽기 전용), 요청 접수(실행 성공), 관측(구성·부팅 확인),
미검증(애플리케이션·트래픽). 실행 성공을 관측 ready로 읽지 않는지, 조회 실패를 부재·성공으로
읽지 않는지, 같은 이름의 남의 자원을 이 실행의 것으로 읽지 않는지를 본다.

실 AWS 검증은 범위 밖이다 — autoscaling은 LocalStack Community에 없다(ADR-0006 §4).
"""

import copy
import importlib.util
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from botocore.session import get_session
from botocore.stub import Stubber
from botocore.validate import validate_parameters

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.autoscaling import (  # noqa: E402
    EXECUTION_TAG_KEY,
    PRECHECK_DRY_RUN_EXECUTION_TAG_VALUE,
    SCALING_POLICY_NAME,
    SOURCE_INSTANCE_TAG_KEY,
    AutoscalingExecutionPlan,
    autoscaling_image_name,
)
from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import autoscaling as asg  # noqa: E402
from services.aws import executor as ex  # noqa: E402

_CREATE_LT_INPUT = (
    get_session().get_service_model("ec2").operation_model("CreateLaunchTemplate").input_shape
)

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
SOURCE = "i-0abc123456789def0"
TARGET_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{SOURCE}"
EXECUTION = "3f1c2b8a-0d5e-4c1a-9b7e-2a6f8d4c1e90"
OTHER_EXECUTION = "9e9e9e9e-0000-4000-8000-000000000000"
SUBNET = "subnet-0abc123456789def0"
VPC = "vpc-0abc123456789def0"
SG = "sg-0abc123456789def0"
PROFILE_ARN = f"arn:aws:iam::{ACCOUNT}:instance-profile/app"
KEY_NAME = "ops-key"
LT_NAME = f"vigilantis-lt-{SOURCE}"
ASG_NAME = f"vigilantis-asg-{SOURCE}"
IMAGE_NAME = autoscaling_image_name(SOURCE, EXECUTION)

# 대기는 조회 횟수로 묶인다 — 값이 작아 테스트가 상한 소진까지 그대로 밟는다. 잠들지는 않는다.
SETTINGS = SimpleNamespace(
    AUTOSCALING_IMAGE_WAIT_DELAY_SECONDS=7,
    AUTOSCALING_IMAGE_WAIT_MAX_ATTEMPTS=3,
    AUTOSCALING_READY_WAIT_DELAY_SECONDS=11,
    AUTOSCALING_READY_WAIT_MAX_ATTEMPTS=3,
)

TAGS = [
    {"Key": EXECUTION_TAG_KEY, "Value": EXECUTION},
    {"Key": SOURCE_INSTANCE_TAG_KEY, "Value": SOURCE},
    {"Key": "Environment", "Value": "staging"},
    {"Key": "vigilantis:smoke", "Value": "true"},
]
FOREIGN_TAGS = [
    {"Key": EXECUTION_TAG_KEY, "Value": OTHER_EXECUTION},
    {"Key": SOURCE_INSTANCE_TAG_KEY, "Value": SOURCE},
]

PLAN = AutoscalingExecutionPlan.model_validate(
    {
        "target_arn": TARGET_ARN,
        "execution_id": EXECUTION,
        "min_size": 1,
        "max_size": 3,
        "desired_capacity": 1,
        "image_name": IMAGE_NAME,
        "launch_template_name": LT_NAME,
        "launch_template_version": 1,
        "asg_name": ASG_NAME,
        "scaling_policy_name": SCALING_POLICY_NAME,
        "cpu_target_percent": 50,
        "default_instance_warmup_seconds": 300,
        "source": {
            "instance_id": SOURCE,
            "instance_type": "t3.micro",
            "subnet_id": SUBNET,
            "security_group_ids": [SG],
            "iam_instance_profile_arn": PROFILE_ARN,
            "ebs_optimized": False,
            "detailed_monitoring": False,
            "dedicated_tenancy": False,
            "associate_public_ip_address": True,
            "metadata_options": {
                "http_tokens": "required",
                "http_endpoint": "enabled",
                "http_put_response_hop_limit": 2,
                "http_protocol_ipv6": "disabled",
                "instance_metadata_tags": "disabled",
            },
            "preserved_tags": [
                {"key": "Environment", "value": "staging"},
                {"key": "vigilantis:smoke", "value": "true"},
            ],
        },
    }
)


def source_instance(**overrides) -> dict:
    instance = {
        "InstanceId": SOURCE,
        "InstanceType": "t3.micro",
        "State": {"Name": "running"},
        "RootDeviceType": "ebs",
        "SubnetId": SUBNET,
        "VpcId": VPC,
        "EbsOptimized": False,
        "Monitoring": {"State": "disabled"},
        "Placement": {"AvailabilityZone": "ap-northeast-2a", "Tenancy": "default"},
        "IamInstanceProfile": {"Arn": PROFILE_ARN, "Id": "AIPAEXAMPLEPROFILE"},
        "KeyName": KEY_NAME,
        "MetadataOptions": {
            "State": "applied",
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 2,
            "HttpEndpoint": "enabled",
            "HttpProtocolIpv6": "disabled",
            "InstanceMetadataTags": "disabled",
        },
        "NetworkInterfaces": [
            {
                "NetworkInterfaceId": "eni-0abc123456789def0",
                "Attachment": {"DeviceIndex": 0},
                "Groups": [{"GroupId": SG, "GroupName": "app"}],
                "Association": {"IpOwnerId": "amazon", "PublicIp": "203.0.113.10"},
            }
        ],
        "Tags": [
            {"Key": "Name", "Value": "app"},
            {"Key": "Environment", "Value": "staging"},
            {"Key": "vigilantis:smoke", "Value": "true"},
        ],
    }
    instance.update(overrides)
    return instance


def expected_template_data(image_id: str) -> dict:
    return {
        "ImageId": image_id,
        "InstanceType": "t3.micro",
        "EbsOptimized": False,
        "Monitoring": {"Enabled": False},
        "NetworkInterfaces": [
            {
                "DeviceIndex": 0,
                "Groups": [SG],
                "DeleteOnTermination": True,
                "AssociatePublicIpAddress": True,
            }
        ],
        "TagSpecifications": [
            {"ResourceType": "instance", "Tags": TAGS},
            {"ResourceType": "volume", "Tags": TAGS},
        ],
        "IamInstanceProfile": {"Arn": PROFILE_ARN},
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpEndpoint": "enabled",
            "HttpPutResponseHopLimit": 2,
            "HttpProtocolIpv6": "disabled",
            "InstanceMetadataTags": "disabled",
        },
    }


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"RequestId": "req-err", "HTTPStatusCode": status},
        },
        "Op",
    )


def lost_response() -> ReadTimeoutError:
    """요청은 닿았는데 응답을 받지 못했다 — 적용 여부를 모른다."""
    return ReadTimeoutError(endpoint_url="https://ec2.ap-northeast-2.amazonaws.com/")


def asg_instance(instance_id: str, template_id: str, lifecycle="InService", version="1") -> dict:
    return {
        "InstanceId": instance_id,
        "AvailabilityZone": "ap-northeast-2a",
        "LifecycleState": lifecycle,
        "HealthStatus": "Healthy",
        "ProtectedFromScaleIn": False,
        "LaunchTemplate": {
            "LaunchTemplateId": template_id,
            "LaunchTemplateName": LT_NAME,
            "Version": version,
        },
    }


def group_record(*, template_id: str, tags: list, instances: list, name=ASG_NAME, **settings) -> dict:
    return {
        "AutoScalingGroupName": name,
        "LaunchTemplate": {
            "LaunchTemplateId": template_id,
            "LaunchTemplateName": LT_NAME,
            "Version": settings.get("version", "1"),
        },
        "MinSize": settings.get("min_size", 1),
        "MaxSize": settings.get("max_size", 3),
        "DesiredCapacity": settings.get("desired", len(instances)),
        "DefaultCooldown": 300,
        "AvailabilityZones": ["ap-northeast-2a"],
        "HealthCheckType": "EC2",
        "CreatedTime": datetime(2026, 9, 30, tzinfo=UTC),
        "VPCZoneIdentifier": settings.get("subnet", SUBNET),
        "DefaultInstanceWarmup": settings.get("warmup", 300),
        "Instances": instances,
        "Tags": [
            {
                "ResourceId": name,
                "ResourceType": "auto-scaling-group",
                "Key": tag["Key"],
                "Value": tag["Value"],
                "PropagateAtLaunch": False,
            }
            for tag in tags
        ],
    }


def _filter_values(filters, name) -> list:
    for item in filters:
        if item["Name"] == name:
            return list(item["Values"])
    raise AssertionError(f"필터 {name}가 없다: {filters}")


# ------------------------------------------------------------------ 상태형 대역
CREATE_OPERATIONS = frozenset(
    {"create_image", "create_launch_template", "create_auto_scaling_group", "put_scaling_policy"}
)


class _FakeClient:
    def __init__(self, world: "FakeAws", service: str) -> None:
        self._world = world
        self._service = service

    def __getattr__(self, operation):
        if operation not in FakeAws.ALLOWED[self._service]:
            raise AssertionError(f"계약 밖 AWS 작업: {self._service}.{operation}")

        def call(**kwargs):
            return self._world.invoke(self._service, operation, kwargs)

        return call


class FakeAws:
    """허용한 작업만 받는 AWS. 생성은 상태를 남기고 조회는 그 상태를 읽는다.

    이름 필터는 **접두 일치**로 넓게 돌려준다 — 결과를 글자 그대로 다시 대조하지 않는 코드는
    비슷한 이름의 자원을 집는다.
    """

    ALLOWED: ClassVar[dict[str, frozenset[str]]] = {
        "ec2": frozenset(
            {
                "describe_instances",
                "create_image",
                "describe_images",
                "describe_snapshots",
                "create_launch_template",
                "describe_launch_templates",
                "describe_launch_template_versions",
                "describe_instance_status",
            }
        ),
        "autoscaling": frozenset(
            {
                "describe_auto_scaling_instances",
                "create_auto_scaling_group",
                "describe_auto_scaling_groups",
                "put_scaling_policy",
                "describe_policies",
            }
        ),
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.sleeps: list[float] = []
        self.instances = {SOURCE: source_instance()}
        self.memberships: list[dict] = []
        self.images: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        self.templates: dict[str, dict] = {}
        self.template_data: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.policies: dict[tuple[str, str], dict] = {}
        self.statuses: dict[str, tuple[str, str, str]] = {}
        self.faults: dict[str, list[tuple[BaseException, bool]]] = {}
        self.image_states = ["available"]
        self.snapshots_per_image = 1
        self.launch_lifecycle = "InService"
        self.lt_warning = None
        self.page_size = None
        self.repeat_token = False
        self.on_group_describe = None
        self.group_describes = 0
        self.client_tokens: list[str] = []
        self._ids = 0

    # --- 테스트 조작
    def client(self, service, region=None):
        assert region == REGION
        return _FakeClient(self, service)

    def fail(self, operation, exc, *, applied=False, after=0):
        """after건을 통과시킨 다음 호출 1건을 실패시킨다. applied면 상태를 바꾼 뒤 응답만 잃는다."""
        self.faults.setdefault(operation, []).extend([None] * after + [(exc, applied)])

    def operations(self) -> list[str]:
        return [f"{service}.{operation}" for service, operation, _ in self.calls]

    def kwargs(self, operation) -> list[dict]:
        return [kwargs for _, name, kwargs in self.calls if name == operation]

    def invoke(self, service, operation, kwargs):
        self.calls.append((service, operation, copy.deepcopy(kwargs)))
        queued = self.faults.get(operation)
        fault = queued.pop(0) if queued else None
        if fault is not None and not fault[1]:
            raise fault[0]
        result = getattr(self, f"_{operation}")(**kwargs)
        if fault is not None:
            raise fault[0]
        return result

    def _new_id(self, prefix):
        self._ids += 1
        return f"{prefix}-{self._ids:017x}"

    def _page(self, key, items, token):
        if self.page_size is None:
            return {key: items}
        start = int(token or 0)
        page = {key: items[start : start + self.page_size]}
        if start + self.page_size < len(items):
            page["NextToken"] = "0" if self.repeat_token else str(start + self.page_size)
        return page

    def launch(self, template_id, lifecycle=None):
        instance_id = self._new_id("i")
        self.statuses[instance_id] = ("running", "ok", "ok")
        return asg_instance(instance_id, template_id, lifecycle or self.launch_lifecycle)

    # --- EC2
    def _describe_instances(self, InstanceIds):
        found = [copy.deepcopy(self.instances[i]) for i in InstanceIds if i in self.instances]
        if not found:
            raise client_error("InvalidInstanceID.NotFound")
        return {"Reservations": [{"Instances": found}]}

    def _create_image(self, InstanceId, Name, Description, NoReboot, TagSpecifications):
        if any(image["Name"] == Name for image in self.images.values()):
            raise client_error("InvalidAMIName.Duplicate")
        tags = {spec["ResourceType"]: spec["Tags"] for spec in TagSpecifications}
        image_id = self._new_id("ami")
        mappings = []
        for index in range(self.snapshots_per_image):
            snapshot_id = self._new_id("snap")
            self.snapshots[snapshot_id] = {
                "SnapshotId": snapshot_id,
                "OwnerId": ACCOUNT,
                "State": "completed",
                "Tags": copy.deepcopy(tags.get("snapshot", [])),
            }
            device = "/dev/xvda" if index == 0 else f"/dev/sd{chr(ord('f') + index - 1)}"
            mappings.append({"DeviceName": device, "Ebs": {"SnapshotId": snapshot_id}})
        self.images[image_id] = {
            "ImageId": image_id,
            "Name": Name,
            "OwnerId": ACCOUNT,
            "State": "pending",
            "BlockDeviceMappings": mappings,
            "Tags": copy.deepcopy(tags.get("image", [])),
        }
        return {"ImageId": image_id, "ResponseMetadata": {"RequestId": "req-image"}}

    def _describe_images(self, ImageIds=None, Owners=None, IncludeDisabled=None, Filters=None, NextToken=None):
        if ImageIds is not None:
            state = self.image_states.pop(0) if len(self.image_states) > 1 else self.image_states[0]
            found = []
            for image_id in ImageIds:
                if image_id in self.images:
                    self.images[image_id]["State"] = state
                    found.append(copy.deepcopy(self.images[image_id]))
            return {"Images": found}
        assert Owners == ["self"] and IncludeDisabled is True
        (prefix,) = _filter_values(Filters, "name")
        items = [copy.deepcopy(i) for i in self.images.values() if i["Name"].startswith(prefix)]
        return self._page("Images", items, NextToken)

    def _describe_snapshots(self, OwnerIds, Filters, NextToken=None):
        assert OwnerIds == ["self"]
        ids = _filter_values(Filters, "snapshot-id")
        items = [copy.deepcopy(self.snapshots[s]) for s in ids if s in self.snapshots]
        return self._page("Snapshots", items, NextToken)

    def _describe_launch_templates(self, Filters, NextToken=None):
        (prefix,) = _filter_values(Filters, "launch-template-name")
        items = [
            copy.deepcopy(t)
            for t in self.templates.values()
            if t["LaunchTemplateName"].startswith(prefix)
        ]
        return self._page("LaunchTemplates", items, NextToken)

    def _create_launch_template(
        self, LaunchTemplateName, LaunchTemplateData, TagSpecifications=None,
        VersionDescription=None, DryRun=False, ClientToken=None,
    ):
        if DryRun:
            raise client_error("DryRunOperation", 412)
        # 실행 요청은 토큰이 필수이고 실제 SDK 요청 검증을 통과해야 한다(idempotencyToken 문자열)
        assert ClientToken is not None
        validate_parameters(
            {"LaunchTemplateName": LaunchTemplateName, "LaunchTemplateData": {}, "ClientToken": ClientToken},
            _CREATE_LT_INPUT,
        )
        self.client_tokens.append(ClientToken)
        if any(t["LaunchTemplateName"] == LaunchTemplateName for t in self.templates.values()):
            raise client_error("InvalidLaunchTemplateName.AlreadyExistsException")
        template_id = self._new_id("lt")
        self.templates[template_id] = {
            "LaunchTemplateId": template_id,
            "LaunchTemplateName": LaunchTemplateName,
            "LatestVersionNumber": 1,
            "DefaultVersionNumber": 1,
            "Tags": copy.deepcopy(TagSpecifications[0]["Tags"]),
        }
        self.template_data[template_id] = copy.deepcopy(LaunchTemplateData)
        response = {
            "LaunchTemplate": copy.deepcopy(self.templates[template_id]),
            "ResponseMetadata": {"RequestId": "req-lt"},
        }
        if self.lt_warning:
            response["Warning"] = {"Errors": self.lt_warning}
        return response

    def _describe_launch_template_versions(self, LaunchTemplateId, Versions):
        assert Versions == ["1"]
        if LaunchTemplateId not in self.template_data:
            raise client_error("InvalidLaunchTemplateId.VersionNotFound")
        data = copy.deepcopy(self.template_data[LaunchTemplateId])
        # AWS가 조회 응답에 덧붙이는 표기 — 비교가 이것을 불일치로 읽으면 안 된다
        if "MetadataOptions" in data:
            data["MetadataOptions"]["State"] = "applied"
        return {
            "LaunchTemplateVersions": [
                {"LaunchTemplateId": LaunchTemplateId, "VersionNumber": 1, "LaunchTemplateData": data}
            ]
        }

    def _describe_instance_status(self, InstanceIds, IncludeAllInstances, NextToken=None):
        assert IncludeAllInstances is True
        items = [
            {
                "InstanceId": instance_id,
                "InstanceState": {"Name": status[0]},
                "SystemStatus": {"Status": status[1]},
                "InstanceStatus": {"Status": status[2]},
            }
            for instance_id in InstanceIds
            if (status := self.statuses.get(instance_id)) is not None
        ]
        return self._page("InstanceStatuses", items, NextToken)

    # --- Auto Scaling
    def _describe_auto_scaling_instances(self, InstanceIds, NextToken=None):
        items = [copy.deepcopy(m) for m in self.memberships if m["InstanceId"] in InstanceIds]
        return self._page("AutoScalingInstances", items, NextToken)

    def _create_auto_scaling_group(
        self, AutoScalingGroupName, LaunchTemplate, MinSize, MaxSize, DesiredCapacity,
        VPCZoneIdentifier, DefaultInstanceWarmup, Tags,
    ):
        if AutoScalingGroupName in self.groups:
            raise client_error("AlreadyExists")
        template_id = LaunchTemplate["LaunchTemplateId"]
        self.groups[AutoScalingGroupName] = group_record(
            template_id=template_id,
            tags=[{"Key": t["Key"], "Value": t["Value"]} for t in Tags],
            instances=[self.launch(template_id) for _ in range(DesiredCapacity)],
            name=AutoScalingGroupName,
            version=LaunchTemplate["Version"],
            min_size=MinSize,
            max_size=MaxSize,
            desired=DesiredCapacity,
            subnet=VPCZoneIdentifier,
            warmup=DefaultInstanceWarmup,
        )
        return {"ResponseMetadata": {"RequestId": "req-asg"}}

    def _describe_auto_scaling_groups(self, AutoScalingGroupNames, NextToken=None):
        self.group_describes += 1
        if self.on_group_describe is not None:
            self.on_group_describe(self, self.group_describes)
        items = [copy.deepcopy(self.groups[n]) for n in AutoScalingGroupNames if n in self.groups]
        return self._page("AutoScalingGroups", items, NextToken)

    def _put_scaling_policy(self, AutoScalingGroupName, PolicyName, PolicyType, TargetTrackingConfiguration):
        if AutoScalingGroupName not in self.groups:
            raise client_error("ValidationError")
        arn = f"arn:aws:autoscaling:{REGION}:{ACCOUNT}:scalingPolicy:x:autoScalingGroupName/{AutoScalingGroupName}:policyName/{PolicyName}"
        self.policies[(AutoScalingGroupName, PolicyName)] = {
            "AutoScalingGroupName": AutoScalingGroupName,
            "PolicyName": PolicyName,
            "PolicyARN": arn,
            "PolicyType": PolicyType,
            "TargetTrackingConfiguration": copy.deepcopy(TargetTrackingConfiguration),
            "Enabled": True,
        }
        return {"PolicyARN": arn, "Alarms": [], "ResponseMetadata": {"RequestId": "req-policy"}}

    def _describe_policies(self, AutoScalingGroupName, PolicyNames, NextToken=None):
        items = [
            copy.deepcopy(policy)
            for (group, name), policy in self.policies.items()
            if group == AutoScalingGroupName and name in PolicyNames
        ]
        return self._page("ScalingPolicies", items, NextToken)

    # --- 상태 조회 지름길
    def group(self) -> dict:
        return self.groups[ASG_NAME]

    def only(self, mapping) -> dict:
        (item,) = mapping.values()
        return item


@pytest.fixture
def aws(monkeypatch):
    world = FakeAws()

    def factory(service, region=None, **_):
        return world.client(service, region)

    monkeypatch.setattr(ex, "aws_client", factory)
    monkeypatch.setattr(asg, "aws_client", factory)
    monkeypatch.setattr(asg, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(asg, "_sleep", world.sleeps.append)
    yield world
    # 어떤 시나리오에서도 생성 4종과 조회 말고는 부르지 않았다 — 삭제·정지·부착·수정 없음
    stray = [op for _, op, _ in world.calls if op not in CREATE_OPERATIONS and not op.startswith("describe_")]
    assert not stray, stray


def prepare(*, execution_id=EXECUTION, min_size=1, max_size=3, target_arn=TARGET_ARN):
    return ex.prepare_ec2_autoscaling_plan(
        target_arn, execution_id=execution_id, min_size=min_size, max_size=max_size
    )


def make_plan(world: FakeAws) -> AutoscalingExecutionPlan:
    plan, code, detail = prepare()
    assert (code, detail) == (None, None)
    world.calls.clear()
    return plan


def execute(plan, record=None, *, target_arn=TARGET_ARN):
    return ex.execute_ec2_enable_autoscaling(target_arn, plan=plan, record_step=record)


def executed(world: FakeAws):
    plan = make_plan(world)
    outcome = execute(plan)
    assert outcome.succeeded, outcome.error_summary
    world.calls.clear()
    world.sleeps.clear()
    world.group_describes = 0
    return plan, outcome


def steps(outcome) -> list[tuple]:
    return [(s.sequence, s.step_type, s.status, s.effect) for s in outcome.steps]


def residual_part(summary: str) -> str:
    return summary.split("잔여 자원", 1)[1]


# ------------------------------------------------------------------ 대역 자체


@pytest.mark.parametrize(
    "service, operation",
    [
        ("ec2", "deregister_image"),
        ("ec2", "delete_snapshot"),
        ("ec2", "delete_launch_template"),
        ("ec2", "stop_instances"),
        ("ec2", "terminate_instances"),
        ("ec2", "get_waiter"),
        ("autoscaling", "delete_auto_scaling_group"),
        ("autoscaling", "attach_instances"),
        ("autoscaling", "update_auto_scaling_group"),
    ],
)
def test_strict_fake_rejects_operations_outside_the_contract(service, operation):
    with pytest.raises(AssertionError):
        getattr(FakeAws().client(service, REGION), operation)


# ------------------------------------------------------------------ ① 준비


def test_prepare_freezes_source_intent_with_reads_only(aws):
    plan, code, detail = prepare()

    assert (code, detail) == (None, None)
    assert plan == PLAN
    assert aws.operations() == [
        "ec2.describe_instances",
        "autoscaling.describe_auto_scaling_instances",
        "ec2.describe_images",
        "ec2.describe_launch_templates",
        "autoscaling.describe_auto_scaling_groups",
    ]
    # Name은 옮기지 않는다 — 정해진 원본 태그만
    assert [tag.key for tag in plan.source.preserved_tags] == ["Environment", "vigilantis:smoke"]
    assert AutoscalingExecutionPlan.model_validate(plan.model_dump(mode="json")) == plan


def test_prepare_rejects_a_source_already_in_a_differently_named_asg(aws):
    """소속은 이름 추정이 아니라 AWS의 답으로 가린다 — 규칙 밖 이름의 ASG도 거절한다."""
    aws.memberships.append(
        {
            "InstanceId": SOURCE,
            "AutoScalingGroupName": "legacy-web-asg",
            "AvailabilityZone": "ap-northeast-2a",
            "LifecycleState": "InService",
            "HealthStatus": "HEALTHY",
            "ProtectedFromScaleIn": False,
        }
    )

    plan, code, detail = prepare()

    assert plan is None and code is R.PRECHECK_INVALID_STATE
    assert "legacy-web-asg" in detail
    assert aws.kwargs("describe_auto_scaling_instances") == [{"InstanceIds": [SOURCE]}]
    assert aws.operations()[-1] == "autoscaling.describe_auto_scaling_instances"


@pytest.mark.parametrize(
    "error, code",
    [
        (client_error("AccessDenied", 403), R.PRECHECK_UNAUTHORIZED),
        (client_error("Throttling", 400), R.PRECHECK_AWS_ERROR),
        (EndpointConnectionError(endpoint_url="https://autoscaling"), R.PRECHECK_AWS_ERROR),
    ],
)
def test_membership_query_failure_never_reaches_create_image(aws, error, code):
    """소속을 묻지 못했으면 '소속 없음'이 아니다 — 계획이 없으니 AMI도 만들지 않는다."""
    aws.fail("describe_auto_scaling_instances", error)

    plan, got, detail = prepare()

    assert plan is None and got is code
    assert "소속 조회 실패" in detail
    assert not [op for op in aws.operations() if "create" in op or "put" in op]


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"State": {"Name": "stopped"}}, "running"),
        ({"RootDeviceType": "instance-store"}, "EBS"),
        ({"Placement": {"Tenancy": "host"}}, "host"),
        ({"NetworkInterfaces": []}, "DeviceIndex 0"),
    ],
)
def test_prepare_rejects_sources_it_cannot_carry_over(aws, overrides, fragment):
    aws.instances[SOURCE] = source_instance(**overrides)

    plan, code, detail = prepare()

    assert plan is None and code is R.PRECHECK_INVALID_STATE
    assert fragment in detail


def test_prepare_classifies_source_lookup_failures(aws):
    aws.instances.clear()
    assert prepare()[:2] == (None, R.PRECHECK_TARGET_NOT_FOUND)

    aws.instances[SOURCE] = source_instance()
    aws.fail("describe_instances", client_error("UnauthorizedOperation", 403))
    assert prepare()[:2] == (None, R.PRECHECK_UNAUTHORIZED)


@pytest.mark.parametrize("taken", ["image", "launch_template", "group"])
def test_prepare_refuses_names_that_already_exist(aws, taken):
    """같은 이름의 자원은 이 실행의 것으로 쓰지 않는다 — 계획부터 세우지 않는다."""
    if taken == "image":
        aws.images["ami-0fff"] = {"ImageId": "ami-0fff", "Name": IMAGE_NAME, "OwnerId": ACCOUNT, "Tags": TAGS}
    elif taken == "launch_template":
        aws.templates["lt-0fff"] = {"LaunchTemplateId": "lt-0fff", "LaunchTemplateName": LT_NAME, "Tags": FOREIGN_TAGS}
    else:
        aws.groups[ASG_NAME] = group_record(template_id="lt-0fff", tags=FOREIGN_TAGS, instances=[])

    plan, code, detail = prepare()

    assert plan is None and code is R.PRECHECK_INVALID_STATE
    assert "재사용하지 않습니다" in detail


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_size": 3, "max_size": 2},
        {"max_size": 5},
        {"min_size": True},
        {"execution_id": PRECHECK_DRY_RUN_EXECUTION_TAG_VALUE},
        {"target_arn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0abc123456789def0"},
    ],
)
def test_prepare_rejects_bad_inputs_before_any_aws_call(aws, kwargs):
    plan, code, _ = prepare(**kwargs)

    assert plan is None and code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


@pytest.mark.parametrize(
    "association, expected",
    [
        ({"IpOwnerId": "amazon", "PublicIp": "203.0.113.10"}, True),
        ({"IpOwnerId": ACCOUNT, "PublicIp": "203.0.113.10"}, None),  # EIP — 서브넷 기본에 맡긴다
        (None, False),
    ],
)
def test_public_ip_intent_follows_the_primary_interface(aws, association, expected):
    interface = copy.deepcopy(source_instance()["NetworkInterfaces"][0])
    interface.pop("Association")
    if association is not None:
        interface["Association"] = association
    aws.instances[SOURCE] = source_instance(NetworkInterfaces=[interface])

    plan, _, _ = prepare()

    assert plan.source.associate_public_ip_address is expected
    data = asg._launch_template_data(plan, "ami-0abc")
    assert data["NetworkInterfaces"][0].get("AssociatePublicIpAddress") is expected


# ------------------------------------------------------------------ ② 실행


def test_execute_creates_image_template_group_and_policy_in_order(aws):
    plan = make_plan(aws)
    aws.image_states = ["pending", "available"]
    recorded = []

    outcome = execute(plan, recorded.append)

    assert outcome.succeeded
    assert steps(outcome) == [
        (1, ex.STEP_CREATE_IMAGE, S.SUCCESS, E.APPLIED),
        (2, ex.STEP_CREATE_LAUNCH_TEMPLATE, S.SUCCESS, E.APPLIED),
        (3, ex.STEP_CREATE_AUTO_SCALING_GROUP, S.SUCCESS, E.APPLIED),
        (4, ex.STEP_PUT_SCALING_POLICY, S.SUCCESS, E.APPLIED),
    ]
    # 호출 직전 IN_PROGRESS, 직후 종료 — 단계마다 두 번
    assert [(s.sequence, s.status) for s in recorded] == [
        (n, status) for n in (1, 2, 3, 4) for status in (S.IN_PROGRESS, S.SUCCESS)
    ]
    assert aws.operations() == [
        "ec2.create_image",
        "ec2.describe_images",
        "ec2.describe_images",
        "ec2.create_launch_template",
        "autoscaling.create_auto_scaling_group",
        "autoscaling.put_scaling_policy",
    ]
    assert aws.sleeps == [7]

    (image_call,) = aws.kwargs("create_image")
    assert image_call["NoReboot"] is True and image_call["InstanceId"] == SOURCE
    image_id = aws.only(aws.images)["ImageId"]
    template_id = aws.only(aws.templates)["LaunchTemplateId"]
    (template_call,) = aws.kwargs("create_launch_template")
    assert template_call["LaunchTemplateData"] == expected_template_data(image_id)
    # 원본에 KeyName이 있어도 옮기지 않는다(서비스 기동에 필요 없다). UserData도 다시 돌리지 않는다
    assert "UserData" not in template_call["LaunchTemplateData"]
    assert "KeyName" not in template_call["LaunchTemplateData"]
    (group_call,) = aws.kwargs("create_auto_scaling_group")
    assert group_call["LaunchTemplate"] == {"LaunchTemplateId": template_id, "Version": "1"}
    assert (group_call["MinSize"], group_call["MaxSize"], group_call["DesiredCapacity"]) == (1, 3, 1)
    assert group_call["VPCZoneIdentifier"] == SUBNET
    assert group_call["DefaultInstanceWarmup"] == 300
    (policy_call,) = aws.kwargs("put_scaling_policy")
    assert policy_call["TargetTrackingConfiguration"] == {
        "PredefinedMetricSpecification": {"PredefinedMetricType": "ASGAverageCPUUtilization"},
        "TargetValue": 50.0,
    }
    # 원본 인스턴스는 읽지도 바꾸지도 않는다 — 값은 계획에서만 온다
    assert "describe_instances" not in [op for _, op, _ in aws.calls]


def test_request_acceptance_is_not_readiness(aws):
    """실행 성공은 요청 4건 접수다. 인스턴스가 뜨지 않으면 관측은 ready가 아니다."""
    aws.launch_lifecycle = "Pending"
    plan = make_plan(aws)

    outcome = execute(plan)
    inspection = ex.inspect_ec2_autoscaling(plan)

    assert outcome.succeeded
    assert all(step.effect is E.APPLIED for step in outcome.steps)
    assert inspection.status == "incomplete"
    assert "부팅 미확인" in inspection.summary


def test_image_wait_is_bounded_by_attempts_not_wall_time(aws):
    plan = make_plan(aws)
    aws.image_states = ["pending"]

    outcome = execute(plan)

    assert steps(outcome) == [(1, ex.STEP_CREATE_IMAGE, S.FAILED, E.UNKNOWN)]
    assert outcome.reason_code is R.PRECHECK_AWS_ERROR
    assert "3회" in outcome.error_summary
    assert len(aws.kwargs("describe_images")) == 3
    assert aws.sleeps == [7, 7]  # 시도 사이에만 — 마지막 뒤에는 잠들지 않는다
    assert "create_launch_template" not in [op for _, op, _ in aws.calls]


def test_failed_image_stops_the_wait(aws):
    plan = make_plan(aws)
    aws.image_states = ["failed"]

    outcome = execute(plan)

    assert steps(outcome) == [(1, ex.STEP_CREATE_IMAGE, S.FAILED, E.UNKNOWN)]
    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert aws.sleeps == []


def test_image_status_query_failure_is_not_read_as_not_applied(aws):
    """AMI 요청은 접수됐다 — 상태 조회 거절(4xx)을 '변경 없음'으로 적지 않는다."""
    plan = make_plan(aws)
    aws.fail("describe_images", client_error("UnauthorizedOperation", 403))

    outcome = execute(plan)

    assert steps(outcome) == [(1, ex.STEP_CREATE_IMAGE, S.FAILED, E.UNKNOWN)]
    assert outcome.reason_code is R.PRECHECK_UNAUTHORIZED


def test_image_lookup_propagation_delay_is_retried(aws):
    plan = make_plan(aws)
    aws.fail("describe_images", client_error("InvalidAMIID.NotFound"))

    outcome = execute(plan)

    assert outcome.succeeded
    assert aws.sleeps == [7]


REJECTIONS = [
    ("create_image", "InvalidParameterValue", ex.STEP_CREATE_IMAGE, 1),
    ("create_launch_template", "InvalidParameterCombination", ex.STEP_CREATE_LAUNCH_TEMPLATE, 2),
    ("create_auto_scaling_group", "ValidationError", ex.STEP_CREATE_AUTO_SCALING_GROUP, 3),
    ("put_scaling_policy", "ValidationError", ex.STEP_PUT_SCALING_POLICY, 4),
]


@pytest.mark.parametrize("operation, code, step_type, sequence", REJECTIONS)
def test_rejected_request_is_not_applied_and_stops(aws, operation, code, step_type, sequence):
    plan = make_plan(aws)
    aws.fail(operation, client_error(code, 400))

    outcome = execute(plan)

    assert not outcome.succeeded
    assert outcome.steps[-1].sequence == sequence
    assert (outcome.steps[-1].step_type, outcome.steps[-1].status, outcome.steps[-1].effect) == (
        step_type, S.FAILED, E.NOT_APPLIED,
    )
    assert outcome.reason_code is R.PRECHECK_AWS_ERROR
    assert aws.operations()[-1].endswith(operation)


@pytest.mark.parametrize(
    "operation, sequence, status, residual",
    [
        ("create_image", 1, "incomplete", "AMI ami-"),
        ("create_launch_template", 2, "incomplete", "시작 템플릿 lt-"),
        ("create_auto_scaling_group", 3, "incomplete", f"ASG {ASG_NAME}"),
        ("put_scaling_policy", 4, "ready", None),
    ],
)
@pytest.mark.parametrize("error", [lost_response(), client_error("InternalError", 500)])
def test_lost_response_is_unknown_and_left_to_inspection(aws, operation, sequence, status, residual, error):
    """적용됐는데 응답만 잃었다. 실행은 되묻거나 채택하거나 지우지 않고 UNKNOWN으로 멈춘다 —
    현물은 관측이 계획·소유 태그·설정 전부로 판정한다."""
    plan = make_plan(aws)
    aws.fail(operation, error, applied=True)

    outcome = execute(plan)

    assert (outcome.steps[-1].sequence, outcome.steps[-1].status, outcome.steps[-1].effect) == (
        sequence, S.FAILED, E.UNKNOWN,
    )
    assert aws.operations()[-1].endswith(operation)

    inspection = ex.inspect_ec2_autoscaling(plan)
    assert inspection.status == status
    if residual is not None:
        assert residual in residual_part(inspection.summary)


def _primary_ids(world) -> tuple[str, str]:
    return world.only(world.images)["ImageId"], world.only(world.templates)["LaunchTemplateId"]


def _assert_primary_residuals(world, summary, image_id, template_id, marker="잔여 자원"):
    """과금되는 주 자원(AMI·시작 템플릿·ASG)은 딸린 자원이 많아도 잘리지 않고 이 실행의 잔여로 남는다."""
    residuals = summary.split(marker, 1)[1]
    for primary in (f"AMI {image_id}", f"시작 템플릿 {template_id}", f"ASG {ASG_NAME}"):
        assert primary in residuals, primary
    shown = list(world.snapshots)[:3]  # AMI 블록 디바이스 매핑 순서 = 만든 순서
    assert f"스냅숏 {', '.join(shown)} 외 8개(전체 11개 목록은 AMI {image_id}의 블록 디바이스 매핑)" in residuals
    assert len(summary) <= 1024


def test_lost_group_response_with_many_snapshots_keeps_the_owned_group_in_residuals(aws):
    """ASG가 만들어진 뒤 응답을 잃고(정책 없음) AMI 스냅숏이 11개여도 ASG 이름이 요약에 남는다."""
    aws.snapshots_per_image = 11
    plan = make_plan(aws)
    aws.fail("create_auto_scaling_group", lost_response(), applied=True)

    outcome = execute(plan)
    inspection = ex.inspect_ec2_autoscaling(plan)

    assert steps(outcome)[-1] == (3, ex.STEP_CREATE_AUTO_SCALING_GROUP, S.FAILED, E.UNKNOWN)
    assert "put_scaling_policy" not in [op for _, op, _ in aws.calls]
    assert inspection.status == "incomplete"
    assert f"정책 {SCALING_POLICY_NAME} 없음" in inspection.summary
    _assert_primary_residuals(aws, inspection.summary, *_primary_ids(aws))


def test_query_failure_after_many_snapshots_keeps_the_owned_group_in_the_summary(aws):
    aws.snapshots_per_image = 11
    plan = make_plan(aws)
    aws.fail("create_auto_scaling_group", lost_response(), applied=True)
    execute(plan)
    aws.fail("describe_policies", lost_response())

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert (inspection.status, inspection.reason_code) == ("query_failed", R.PRECHECK_AWS_ERROR)
    _assert_primary_residuals(aws, inspection.summary, *_primary_ids(aws), marker="확인한 이 실행의 자원")


def test_long_problems_and_many_snapshots_stay_bounded_with_primary_residuals(aws):
    """스냅숏 11개 전부 태그가 빠져 미완 사유가 한도를 넘어도 주 자원은 남고 요약은 1024자 안이다."""
    aws.snapshots_per_image = 11
    plan, _ = executed(aws)
    for snapshot in aws.snapshots.values():
        _drop_tag(snapshot["Tags"], "Environment")

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete"
    assert "태그 불완전" in inspection.summary
    _assert_primary_residuals(aws, inspection.summary, *_primary_ids(aws))


def test_boot_timeout_with_full_group_and_many_snapshots_stays_bounded(aws):
    aws.snapshots_per_image = 11
    aws.launch_lifecycle = "Pending"
    plan, code, detail = prepare(min_size=4, max_size=4)  # 최대 크기 4대가 다 떠 있다
    assert (code, detail) == (None, None)
    execute(plan)
    members = [item["InstanceId"] for item in aws.group()["Instances"]]

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete" and "부팅 미확인" in inspection.summary
    _assert_primary_residuals(aws, inspection.summary, *_primary_ids(aws))
    assert f"인스턴스 {', '.join(members[:3])} 외 1개(전체 4개" in residual_part(inspection.summary)


def test_template_name_taken_after_prepare_is_rejected_not_adopted(aws):
    plan = make_plan(aws)
    aws.templates["lt-0fff00000000000001"] = {
        "LaunchTemplateId": "lt-0fff00000000000001",
        "LaunchTemplateName": LT_NAME,
        "LatestVersionNumber": 4,
        "Tags": FOREIGN_TAGS,
    }

    outcome = execute(plan)
    inspection = ex.inspect_ec2_autoscaling(plan)

    assert steps(outcome)[-1] == (2, ex.STEP_CREATE_LAUNCH_TEMPLATE, S.FAILED, E.NOT_APPLIED)
    assert "create_auto_scaling_group" not in [op for _, op, _ in aws.calls]
    assert inspection.status == "incomplete"
    assert "lt-0fff00000000000001" in inspection.summary
    assert "이 실행의 것이 아님" in inspection.summary
    residuals = residual_part(inspection.summary)
    assert "lt-0fff00000000000001" not in residuals
    assert "AMI ami-" in residuals and "스냅숏 snap-" in residuals


def test_group_name_taken_after_prepare_is_rejected_not_adopted(aws):
    plan = make_plan(aws)
    aws.groups[ASG_NAME] = group_record(template_id="lt-0fff", tags=FOREIGN_TAGS, instances=[])

    outcome = execute(plan)
    inspection = ex.inspect_ec2_autoscaling(plan)

    # AlreadyExists는 SDK 재시도 뒤 중복일 수 있어 효과를 모른다 — 그래도 남의 그룹은 소유로 읽지 않는다
    assert steps(outcome)[-1] == (3, ex.STEP_CREATE_AUTO_SCALING_GROUP, S.FAILED, E.UNKNOWN)
    assert "put_scaling_policy" not in [op for _, op, _ in aws.calls]
    assert inspection.status == "incomplete"
    assert f"ASG {ASG_NAME}" not in residual_part(inspection.summary)
    assert "시작 템플릿 lt-" in residual_part(inspection.summary)
    assert aws.groups[ASG_NAME]["Tags"] == [
        {"ResourceId": ASG_NAME, "ResourceType": "auto-scaling-group", "PropagateAtLaunch": False, **t}
        for t in FOREIGN_TAGS
    ]


def test_launch_template_request_carries_the_execution_id_as_a_deterministic_client_token(aws):
    plan = make_plan(aws)

    assert execute(plan).succeeded

    assert [c["ClientToken"] for c in aws.kwargs("create_launch_template")] == [EXECUTION]
    assert aws.client_tokens == [plan.execution_id] == [EXECUTION]


@pytest.mark.parametrize("execution_id", ["a", "A" + "-".join(["9"] * 31), "a" * 64])
def test_client_token_boundary_ids_pass_sdk_request_validation(aws, execution_id):
    plan, code, detail = prepare(execution_id=execution_id)
    assert (code, detail) == (None, None)
    aws.calls.clear()

    assert execute(plan).succeeded
    assert aws.client_tokens == [execution_id]


def test_duplicate_ami_name_after_server_side_creation_is_unknown_and_inspected(aws):
    """SDK 재시도가 중복 오류를 받았다 — 첫 요청은 서버에서 AMI를 만들었다. 닫지 않고 UNKNOWN."""
    plan = make_plan(aws)
    aws.fail("create_image", client_error("InvalidAMIName.Duplicate"), applied=True)

    outcome = execute(plan)
    inspection = ex.inspect_ec2_autoscaling(plan)

    assert steps(outcome)[-1] == (1, ex.STEP_CREATE_IMAGE, S.FAILED, E.UNKNOWN)
    assert aws.operations().count("ec2.create_image") == 1  # 재시도·이어 쓰기 없음
    assert "create_launch_template" not in [op for _, op, _ in aws.calls]
    assert inspection.status == "incomplete"
    assert "AMI ami-" in residual_part(inspection.summary)
    assert "스냅숏 snap-" in residual_part(inspection.summary)


def test_duplicate_asg_after_server_side_creation_is_unknown_and_keeps_the_owned_group(aws):
    plan = make_plan(aws)
    aws.fail("create_auto_scaling_group", client_error("AlreadyExists"), applied=True)

    outcome = execute(plan)
    inspection = ex.inspect_ec2_autoscaling(plan)

    assert steps(outcome)[-1] == (3, ex.STEP_CREATE_AUTO_SCALING_GROUP, S.FAILED, E.UNKNOWN)
    assert aws.operations().count("autoscaling.create_auto_scaling_group") == 1
    assert "put_scaling_policy" not in [op for _, op, _ in aws.calls]
    assert inspection.status == "incomplete"
    assert f"ASG {ASG_NAME}" in residual_part(inspection.summary)


def test_other_ami_and_group_errors_keep_their_classification(aws):
    plan = make_plan(aws)
    aws.fail("create_image", client_error("InvalidAMIName.Malformed"))
    assert steps(execute(plan))[-1] == (1, ex.STEP_CREATE_IMAGE, S.FAILED, E.NOT_APPLIED)

    plan = make_plan(aws)
    aws.fail("create_auto_scaling_group", client_error("ValidationError"))
    assert steps(execute(plan))[-1][2:] == (S.FAILED, E.NOT_APPLIED)


def test_recorder_failure_propagates_before_the_next_aws_call(aws):
    plan = make_plan(aws)

    class RecorderDown(Exception):
        pass

    def record(step):
        if step.status is S.SUCCESS:
            raise RecorderDown("단계 기록 실패")

    with pytest.raises(RecorderDown):
        execute(plan, record)
    assert [op for _, op, _ in aws.calls if not op.startswith("describe_")] == ["create_image"]

    aws.calls.clear()

    def refuse(step):
        raise RecorderDown("IN_PROGRESS 기록 실패")

    with pytest.raises(RecorderDown):
        execute(plan, refuse)
    assert aws.calls == []


def test_plan_bound_to_another_target_or_names_is_rejected_without_aws(aws):
    plan = make_plan(aws)
    other = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0fff123456789def0"

    assert execute(plan, target_arn=other).reason_code is R.PRECHECK_PARAM_INVALID
    tampered = plan.model_copy(update={"asg_name": "someone-elses-asg"})
    rejected = execute(tampered)
    observed = ex.inspect_ec2_autoscaling(tampered)

    assert (rejected.reason_code, rejected.steps) == (R.PRECHECK_PARAM_INVALID, ())
    assert observed.status == "incomplete"
    assert aws.calls == []


def test_launch_template_warning_stops_before_the_group(aws):
    plan = make_plan(aws)
    aws.lt_warning = [{"Code": "InvalidParameterCombination", "Message": "x"}]

    outcome = execute(plan)

    assert steps(outcome)[-1] == (2, ex.STEP_CREATE_LAUNCH_TEMPLATE, S.FAILED, E.UNKNOWN)
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert "create_auto_scaling_group" not in [op for _, op, _ in aws.calls]


def test_logs_and_results_never_carry_launch_data(aws, caplog):
    """AMI가 원본 디스크(비밀값 포함)를 복제한다. 시작 템플릿 데이터·응답 본문은 어디에도 싣지 않는다."""
    caplog.set_level(logging.DEBUG, logger="vigilantis.aws")
    plan = make_plan(aws)

    outcome = execute(plan)
    inspection = ex.inspect_ec2_autoscaling(plan)

    texts = [str(record.__dict__) for record in caplog.records]
    texts += [s.result_summary or "" for s in outcome.steps] + [inspection.summary]
    for secret_shaped in (PROFILE_ARN, KEY_NAME, "HttpTokens", "TagSpecifications", "req-lt"):
        assert not [text for text in texts if secret_shaped in text], secret_shaped


# ------------------------------------------------------------------ ③ 관측


def test_inspect_ready_after_execute_reads_only(aws):
    plan, _ = executed(aws)

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "ready" and inspection.reason_code is None
    image_id = aws.only(aws.images)["ImageId"]
    template_id = aws.only(aws.templates)["LaunchTemplateId"]
    for fragment in (image_id, template_id, ASG_NAME, "InService", "애플리케이션", "원본 인스턴스 유지", "NoReboot"):
        assert fragment in inspection.summary
    assert all(op.startswith("describe_") for _, op, _ in aws.calls)
    # 원본은 기준이 아니다 — 읽지 않는다
    assert "describe_instances" not in [op for _, op, _ in aws.calls]
    assert aws.sleeps == []


def test_later_source_changes_are_not_the_intent(aws):
    plan, _ = executed(aws)
    aws.instances[SOURCE] = source_instance(
        InstanceType="t3.large", State={"Name": "stopped"}, KeyName="rotated-key"
    )

    assert ex.inspect_ec2_autoscaling(plan).status == "ready"


def test_desired_may_scale_within_bounds(aws):
    plan, _ = executed(aws)
    group = aws.group()
    template_id = group["LaunchTemplate"]["LaunchTemplateId"]
    group["Instances"] += [aws.launch(template_id), aws.launch(template_id)]
    group["DesiredCapacity"] = 3

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "ready"
    assert "desired 3" in inspection.summary


def test_boot_is_waited_for_with_bounded_attempts(aws):
    plan, _ = executed(aws)
    (instance,) = aws.group()["Instances"]
    instance["LifecycleState"] = "Pending"

    def progress(world, count):
        if count >= 3:  # 1회는 설정 대조, 2회부터 부팅 대기
            world.group()["Instances"][0]["LifecycleState"] = "InService"

    aws.on_group_describe = progress

    assert ex.inspect_ec2_autoscaling(plan).status == "ready"
    assert aws.sleeps == [11]


@pytest.mark.parametrize(
    "stall",
    [
        lambda world: world.group()["Instances"][0].update(LifecycleState="Pending"),
        lambda world: world.statuses.update(
            {world.group()["Instances"][0]["InstanceId"]: ("running", "initializing", "initializing")}
        ),
        lambda world: world.statuses.update(
            {world.group()["Instances"][0]["InstanceId"]: ("running", "ok", "impaired")}
        ),
        lambda world: world.group().update(Instances=[]),
    ],
    ids=["pending", "initializing", "impaired", "missing-member"],
)
def test_known_boot_timeout_is_incomplete_with_residuals(aws, stall):
    plan, _ = executed(aws)
    instance_id = aws.group()["Instances"][0]["InstanceId"]
    stall(aws)

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete"
    assert "부팅 미확인 — 3회 조회" in inspection.summary
    assert aws.sleeps == [11, 11]
    residuals = residual_part(inspection.summary)
    assert f"ASG {ASG_NAME}" in residuals and "AMI ami-" in residuals
    if aws.group()["Instances"]:
        assert instance_id in residuals


def test_instance_status_propagation_delay_is_not_a_query_failure(aws):
    plan, _ = executed(aws)
    aws.fail("describe_instance_status", client_error("InvalidInstanceID.NotFound"))

    assert ex.inspect_ec2_autoscaling(plan).status == "ready"
    assert aws.sleeps == [11]


def test_member_launched_from_another_template_is_not_ours(aws):
    plan, _ = executed(aws)
    aws.group()["Instances"].append(asg_instance("i-0fff123456789def0", "lt-0fffffffffffffff0"))
    aws.group()["DesiredCapacity"] = 2

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete"
    assert "i-0fff123456789def0" in inspection.summary


def _drop_tag(tags, key):
    tags[:] = [tag for tag in tags if tag["Key"] != key]


DRIFTS = {
    "max-size": (lambda w: w.group().update(MaxSize=4), "MaxSize 4"),
    "min-size": (lambda w: w.group().update(MinSize=2), "MinSize 2"),
    "desired-out-of-bounds": (lambda w: w.group().update(DesiredCapacity=4), "DesiredCapacity 4"),
    "subnet": (lambda w: w.group().update(VPCZoneIdentifier=f"{SUBNET},subnet-0fff123456789def0"), "서브넷"),
    "warmup": (lambda w: w.group().update(DefaultInstanceWarmup=60), "DefaultInstanceWarmup 60"),
    "template-version": (lambda w: w.group()["LaunchTemplate"].update(Version="$Latest"), "시작 템플릿·버전"),
    "group-tags": (lambda w: _drop_tag(w.group()["Tags"], "vigilantis:smoke"), "태그"),
    "policy-target": (
        lambda w: w.only(w.policies)["TargetTrackingConfiguration"].update(TargetValue=70.0),
        "정책",
    ),
    "policy-scale-in": (
        lambda w: w.only(w.policies)["TargetTrackingConfiguration"].update(DisableScaleIn=True),
        "정책",
    ),
    "policy-missing": (lambda w: w.policies.clear(), "없음"),
    "image-tags": (lambda w: _drop_tag(w.only(w.images)["Tags"], "Environment"), "태그 불완전"),
    "image-state": (lambda w: w.only(w.images).update(State="deregistered"), "상태 deregistered"),
    "snapshot-missing": (lambda w: w.snapshots.clear(), "없음"),
    "snapshot-foreign": (lambda w: w.only(w.snapshots).update(Tags=FOREIGN_TAGS), "이 실행의 것이 아님"),
    "template-user-data": (
        lambda w: w.only(w.template_data).update(UserData="IyEvYmluL2Jhc2g="),
        "설정이 계획과 다름",
    ),
    "template-type": (lambda w: w.only(w.template_data).update(InstanceType="t3.large"), "설정이 계획과 다름"),
    "template-key": (lambda w: w.only(w.template_data).update(KeyName=KEY_NAME), "설정이 계획과 다름"),
    # 요청하지 않은 설정이 시작 템플릿에 살아 있으면 계획한 구성이 아니다
    "template-block-devices": (
        lambda w: w.only(w.template_data).update(
            BlockDeviceMappings=[{"DeviceName": "/dev/xvdb", "Ebs": {"VolumeSize": 100}}]
        ),
        "설정이 계획과 다름",
    ),
    "template-spot": (
        lambda w: w.only(w.template_data).update(InstanceMarketOptions={"MarketType": "spot"}),
        "설정이 계획과 다름",
    ),
    "template-interface-subnet": (
        lambda w: w.only(w.template_data)["NetworkInterfaces"][0].update(SubnetId="subnet-0fff123456789def0"),
        "설정이 계획과 다름",
    ),
    "template-interface-id": (
        lambda w: w.only(w.template_data)["NetworkInterfaces"][0].update(NetworkInterfaceId="eni-0fff123456789def0"),
        "설정이 계획과 다름",
    ),
    "template-version-gone": (lambda w: w.template_data.clear(), "v1 없음"),
}


@pytest.mark.parametrize("drift", sorted(DRIFTS))
def test_settings_drift_is_incomplete_with_residuals(aws, drift):
    mutate, fragment = DRIFTS[drift]
    plan, _ = executed(aws)
    mutate(aws)

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete", inspection.summary
    assert fragment in inspection.summary
    assert f"ASG {ASG_NAME}" in residual_part(inspection.summary)
    assert aws.sleeps == []  # 설정이 어긋나면 부팅을 기다리지 않는다


def test_equivalent_template_output_is_not_drift(aws):
    """목록 순서·빈 값·상태 표기(MetadataOptions.State)는 설정이 아니다 — 같은 구성으로 읽는다."""
    plan, _ = executed(aws)
    data = aws.only(aws.template_data)
    data["TagSpecifications"].reverse()
    for spec in data["TagSpecifications"]:
        spec["Tags"] = list(reversed(spec["Tags"]))
    data.update(UserData="", BlockDeviceMappings=[], ElasticGpuSpecifications=[])
    data["NetworkInterfaces"][0].update(Ipv6Addresses=[], PrivateIpAddresses=[])

    assert ex.inspect_ec2_autoscaling(plan).status == "ready"


def _foreign_group_tags() -> list:
    return group_record(template_id="lt-0fff", tags=FOREIGN_TAGS, instances=[])["Tags"]


POLL_DRIFTS = {
    "max-size": (lambda g: g.update(MaxSize=4), "MaxSize 4"),
    "subnet": (lambda g: g.update(VPCZoneIdentifier="subnet-0fff123456789def0"), "서브넷"),
    "template": (lambda g: g["LaunchTemplate"].update(LaunchTemplateId="lt-0fffffffffffffff0"), "시작 템플릿·버전"),
    "warmup": (lambda g: g.update(DefaultInstanceWarmup=0), "DefaultInstanceWarmup 0"),
    "group-tags": (lambda g: _drop_tag(g["Tags"], "Environment"), "태그"),
    "ownership": (lambda g: g.update(Tags=_foreign_group_tags()), "이 실행의 것이 아님"),
}


@pytest.mark.parametrize("drift", sorted(POLL_DRIFTS))
def test_group_drift_during_the_boot_wait_is_not_ready(aws, drift):
    """설정 대조를 통과한 뒤 대기 중에 바뀐 ASG로 ready를 내지 않는다 — 매 조회를 다시 대조한다."""
    mutate, fragment = POLL_DRIFTS[drift]
    plan, _ = executed(aws)
    aws.group()["Instances"][0]["LifecycleState"] = "Pending"

    def drift_then_boot(world, count):
        if count == 3:  # 1회는 설정 대조(통과), 2회는 부팅 대기 첫 조회(Pending), 3회에 바뀐다
            mutate(world.group())
            world.group()["Instances"][0]["LifecycleState"] = "InService"

    aws.on_group_describe = drift_then_boot

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete", inspection.summary
    assert "구성 확인" not in inspection.summary
    assert f"부팅 확인 중 ASG {ASG_NAME}" in inspection.summary and fragment in inspection.summary
    assert aws.sleeps == [11]
    owned_residual = f"ASG {ASG_NAME}" in residual_part(inspection.summary)
    assert owned_residual is (drift != "ownership")


def _pending_until_third_group_read(world, then=lambda world: None):
    """1회는 설정 대조, 2회는 부팅 대기 첫 조회(Pending), 3회에 부팅이 끝나며 then이 일어난다."""
    world.group()["Instances"][0]["LifecycleState"] = "Pending"

    def hook(w, count):
        if count == 3:
            w.group()["Instances"][0]["LifecycleState"] = "InService"
            then(w)

    world.on_group_describe = hook


POLICY_DRIFTS = {
    "target": lambda w: w.only(w.policies)["TargetTrackingConfiguration"].update(TargetValue=70.0),
    "scale-in": lambda w: w.only(w.policies)["TargetTrackingConfiguration"].update(DisableScaleIn=True),
    "disabled": lambda w: w.only(w.policies).update(Enabled=False),
    "missing": lambda w: w.policies.clear(),
}


@pytest.mark.parametrize("drift", sorted(POLICY_DRIFTS))
def test_policy_drift_during_the_boot_wait_is_not_ready(aws, drift):
    """정책은 대기 전에 대조했어도 ready 직전에 다시 읽는다 — 대기 중 바뀌거나 사라지면 미완이다."""
    plan, _ = executed(aws)
    _pending_until_third_group_read(aws, POLICY_DRIFTS[drift])

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete", inspection.summary
    assert f"부팅 확인 뒤 정책 {SCALING_POLICY_NAME}" in inspection.summary
    assert "구성 확인" not in inspection.summary
    assert len(aws.kwargs("describe_policies")) == 2
    assert aws.sleeps == [11]


@pytest.mark.parametrize(
    "error, code",
    [
        (client_error("AccessDenied", 403), R.PRECHECK_UNAUTHORIZED),
        (client_error("Throttling", 400), R.PRECHECK_AWS_ERROR),
        (EndpointConnectionError(endpoint_url="https://autoscaling"), R.PRECHECK_AWS_ERROR),
    ],
)
def test_lost_final_policy_read_is_a_query_failure_not_ready(aws, error, code):
    """부팅까지 확인했어도 ready 직전 정책을 묻지 못했으면 ready도 미완도 아니다."""
    plan, _ = executed(aws)
    _pending_until_third_group_read(aws)
    aws.fail("describe_policies", error, after=1)

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert (inspection.status, inspection.reason_code) == ("query_failed", code)
    assert "구성 확인" not in inspection.summary
    assert len(aws.kwargs("describe_policies")) == 2


def test_ready_rereads_the_policy_right_before_claiming_it(aws):
    plan, _ = executed(aws)

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "ready"
    operations = [op for _, op, _ in aws.calls]
    assert operations[-1] == "describe_policies"
    assert operations.count("describe_policies") == 2
    assert "판정 직전" in inspection.summary


def test_nothing_created_is_incomplete_without_residuals(aws):
    plan = make_plan(aws)

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete"
    for fragment in (f"AMI {IMAGE_NAME} 없음", f"시작 템플릿 {LT_NAME} 없음", f"ASG {ASG_NAME} 없음"):
        assert fragment in inspection.summary
    assert residual_part(inspection.summary).startswith("(보상 삭제 없음): 없음")


def test_same_named_resources_of_another_execution_are_never_ours(aws):
    plan = make_plan(aws)
    aws.images["ami-0fff"] = {"ImageId": "ami-0fff", "Name": IMAGE_NAME, "OwnerId": ACCOUNT, "State": "available", "Tags": FOREIGN_TAGS}
    aws.templates["lt-0fff"] = {"LaunchTemplateId": "lt-0fff", "LaunchTemplateName": LT_NAME, "Tags": FOREIGN_TAGS}
    aws.groups[ASG_NAME] = group_record(template_id="lt-0fff", tags=FOREIGN_TAGS, instances=[])

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete"
    assert residual_part(inspection.summary).startswith("(보상 삭제 없음): 없음")


def test_image_of_another_account_is_not_ours(aws):
    plan, _ = executed(aws)
    aws.only(aws.images)["OwnerId"] = "999999999999"

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "incomplete"
    assert "소유자" in inspection.summary


QUERY_OPERATIONS = [
    "describe_images",
    "describe_snapshots",
    "describe_launch_templates",
    "describe_launch_template_versions",
    "describe_auto_scaling_groups",
    "describe_policies",
    "describe_instance_status",
]


@pytest.mark.parametrize("operation", QUERY_OPERATIONS)
@pytest.mark.parametrize(
    "error, code",
    [
        (client_error("UnauthorizedOperation", 403), R.PRECHECK_UNAUTHORIZED),
        (client_error("RequestLimitExceeded", 503), R.PRECHECK_AWS_ERROR),
        (EndpointConnectionError(endpoint_url="https://ec2"), R.PRECHECK_AWS_ERROR),
    ],
)
def test_query_failure_is_neither_success_nor_absence(aws, operation, error, code):
    plan, _ = executed(aws)
    aws.fail(operation, error)

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "query_failed"
    assert inspection.reason_code is code
    assert "미완" not in inspection.summary and "구성 확인" not in inspection.summary


def test_pages_are_followed_and_names_matched_exactly(aws):
    """비슷한 이름이 앞 페이지에 와도 집지 않고, 정확한 이름은 뒤 페이지에서 찾는다."""
    aws.images["ami-0aaa"] = {"ImageId": "ami-0aaa", "Name": IMAGE_NAME + "-old", "OwnerId": ACCOUNT, "Tags": TAGS}
    aws.templates["lt-0aaa"] = {"LaunchTemplateId": "lt-0aaa", "LaunchTemplateName": LT_NAME + "-old", "Tags": TAGS}
    aws.page_size = 1
    plan, _ = executed(aws)

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert inspection.status == "ready", inspection.summary
    tokens = [kwargs.get("NextToken") for kwargs in aws.kwargs("describe_images")]
    assert tokens == [None, "1"]
    assert "ami-0aaa" not in inspection.summary and "lt-0aaa" not in inspection.summary


def test_repeated_page_token_is_a_query_failure(aws):
    plan, _ = executed(aws)
    aws.images["ami-0aaa"] = {"ImageId": "ami-0aaa", "Name": IMAGE_NAME + "-old", "OwnerId": ACCOUNT, "Tags": TAGS}
    aws.page_size = 1
    aws.repeat_token = True

    inspection = ex.inspect_ec2_autoscaling(plan)

    assert (inspection.status, inspection.reason_code) == ("query_failed", R.PRECHECK_AWS_ERROR)


# ------------------------------------------------------------------ precheck ↔ 앱 IAM 정책


@pytest.fixture(scope="module")
def smoke_policy():
    script = REPO_ROOT / "scripts" / "provision_smoke_aws.py"
    spec = importlib.util.spec_from_file_location("provision_smoke_aws_for_autoscaling", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.build_app_policy(ACCOUNT, REGION, VPC, subnet_ids=[SUBNET])


def _resources(statement) -> list:
    resources = statement["Resource"]
    return resources if isinstance(resources, list) else [resources]


def _request_tag_conditions(statement) -> list[tuple]:
    return [
        (operator, key, value)
        for operator, entries in statement.get("Condition", {}).items()
        for key, value in entries.items()
        if key.startswith("aws:RequestTag/") or key == "aws:TagKeys"
    ]


def _satisfies(statement, tags: dict) -> bool:
    """요청 태그 조건만 평가한다. 평가기가 모르는 연산자는 테스트를 깨 갱신을 요구한다."""
    for operator, key, expected in _request_tag_conditions(statement):
        values = expected if isinstance(expected, list) else [expected]
        if key == "aws:TagKeys":
            if operator != "ForAllValues:StringEquals":
                raise AssertionError(f"평가기가 모르는 조건: {operator} {key}")
            ok = set(tags) <= set(values)
        elif operator == "StringEquals":
            ok = tags.get(key.split("/", 1)[1]) in values
        elif operator == "Null":
            ok = (key.split("/", 1)[1] not in tags) == (values[0] == "true")
        else:
            raise AssertionError(f"평가기가 모르는 조건: {operator} {key}")
        if not ok:
            return False
    return True


def policy_allows_request_tags(policy, action: str, resource_fragment: str, tags: dict) -> bool:
    statements = [
        s
        for s in policy["Statement"]
        if s["Effect"] == "Allow"
        and action in s["Action"]
        and any(resource_fragment in r for r in _resources(s))
        and _request_tag_conditions(s)
    ]
    assert statements, f"{action}({resource_fragment})에 요청 태그 울타리가 없다 — 평가 대상이 사라졌다"
    return any(_satisfies(s, tags) for s in statements)


def tag_dict(tags) -> dict:
    return {tag["Key"]: tag["Value"] for tag in tags}


PRECHECK_PARAMS = {"instance_id": SOURCE, "min_size": 1, "max_size": 3, "evidence_id": "ev-1"}


def test_precheck_dry_run_carries_the_execution_tag_shape(aws, smoke_policy):
    """DryRun도 실 생성과 같은 요청 태그 울타리를 통과해야 한다 — 실행 ID 자리는 DryRun 표지다."""
    outcome = ex.precheck("RUNBOOK_EC2_ENABLE_AUTOSCALING", TARGET_ARN, PRECHECK_PARAMS)

    assert outcome.passed, outcome.verification_summary
    (call,) = aws.kwargs("create_launch_template")
    assert call["DryRun"] is True
    (spec,) = call["TagSpecifications"]
    assert spec["ResourceType"] == "launch-template"
    tags = tag_dict(spec["Tags"])
    assert tags == {
        EXECUTION_TAG_KEY: PRECHECK_DRY_RUN_EXECUTION_TAG_VALUE,
        SOURCE_INSTANCE_TAG_KEY: SOURCE,
        "Environment": "staging",
        "vigilantis:smoke": "true",
    }
    assert policy_allows_request_tags(smoke_policy, "ec2:CreateLaunchTemplate", ":launch-template/", tags)


def test_precheck_does_not_invent_the_smoke_tag(aws, smoke_policy):
    """원본에 스모크 태그가 없으면 DryRun도 싣지 않는다 — 정책이 거절하는 것이 맞다."""
    aws.instances[SOURCE] = source_instance(Tags=[{"Key": "Name", "Value": "app"}])

    ex.precheck("RUNBOOK_EC2_ENABLE_AUTOSCALING", TARGET_ARN, PRECHECK_PARAMS)

    (call,) = aws.kwargs("create_launch_template")
    tags = tag_dict(call["TagSpecifications"][0]["Tags"])
    assert "vigilantis:smoke" not in tags
    assert not policy_allows_request_tags(smoke_policy, "ec2:CreateLaunchTemplate", ":launch-template/", tags)


def test_runtime_requests_pass_the_smoke_policy_tag_fences(aws, smoke_policy):
    """새 자원과 ASG가 띄울 인스턴스·볼륨의 요청 태그가 정책 울타리를 통과한다(RunInstances 의존 포함)."""
    plan = make_plan(aws)
    assert execute(plan).succeeded

    (image_call,) = aws.kwargs("create_image")
    image_tags = {s["ResourceType"]: tag_dict(s["Tags"]) for s in image_call["TagSpecifications"]}
    assert policy_allows_request_tags(smoke_policy, "ec2:CreateImage", "::image/", image_tags["image"])
    assert policy_allows_request_tags(smoke_policy, "ec2:CreateImage", "::snapshot/", image_tags["snapshot"])

    (template_call,) = aws.kwargs("create_launch_template")
    assert policy_allows_request_tags(
        smoke_policy, "ec2:CreateLaunchTemplate", ":launch-template/",
        tag_dict(template_call["TagSpecifications"][0]["Tags"]),
    )
    launched = {
        s["ResourceType"]: tag_dict(s["Tags"])
        for s in template_call["LaunchTemplateData"]["TagSpecifications"]
    }
    assert policy_allows_request_tags(smoke_policy, "ec2:RunInstances", ":instance/", launched["instance"])
    assert policy_allows_request_tags(smoke_policy, "ec2:RunInstances", ":volume/", launched["volume"])
    # 키 페어는 옮기지 않는다 — 정책에 key-pair RunInstances 범위가 필요 없다
    assert "KeyName" not in template_call["LaunchTemplateData"]

    (group_call,) = aws.kwargs("create_auto_scaling_group")
    group_tags = tag_dict(group_call["Tags"])
    assert policy_allows_request_tags(smoke_policy, "autoscaling:CreateAutoScalingGroup", "autoScalingGroupName/", group_tags)
    assert policy_allows_request_tags(smoke_policy, "autoscaling:CreateOrUpdateTags", "autoScalingGroupName/", group_tags)
    assert group_call["LaunchTemplate"]["Version"] == "1"  # 버전 명시(LaunchTemplateVersionSpecified)


# ------------------------------------------------------------------ Stubber — 요청 모양


@pytest.fixture
def stubbed(monkeypatch):
    clients = {}
    for service in ("ec2", "autoscaling"):
        clients[service] = boto3.client(
            service,
            region_name=REGION,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            config=Config(retries={"mode": "standard", "total_max_attempts": 1}),
        )
    stubs = {service: Stubber(client) for service, client in clients.items()}
    sleeps: list = []

    def factory(service, region=None, **_):
        return clients[service]

    monkeypatch.setattr(ex, "aws_client", factory)
    monkeypatch.setattr(asg, "aws_client", factory)
    monkeypatch.setattr(asg, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(asg, "_sleep", sleeps.append)
    for stub in stubs.values():
        stub.activate()
    yield SimpleNamespace(ec2=stubs["ec2"], autoscaling=stubs["autoscaling"], sleeps=sleeps)
    for stub in stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


IMAGE_ID = "ami-0abc123456789def0"
SNAPSHOT_ID = "snap-0abc123456789def0"
TEMPLATE_ID = "lt-0abc123456789def0"
LAUNCHED = "i-0bbb123456789def0"
IMAGE_FILTER = {"Owners": ["self"], "IncludeDisabled": True, "Filters": [{"Name": "name", "Values": [IMAGE_NAME]}]}
TEMPLATE_FILTER = {"Filters": [{"Name": "launch-template-name", "Values": [LT_NAME]}]}


def test_prepare_request_shapes(stubbed):
    stubbed.ec2.add_response(
        "describe_instances",
        {"Reservations": [{"Instances": [source_instance()]}]},
        {"InstanceIds": [SOURCE]},
    )
    stubbed.autoscaling.add_response(
        "describe_auto_scaling_instances", {"AutoScalingInstances": []}, {"InstanceIds": [SOURCE]}
    )
    stubbed.ec2.add_response("describe_images", {"Images": []}, IMAGE_FILTER)
    stubbed.ec2.add_response("describe_launch_templates", {"LaunchTemplates": []}, TEMPLATE_FILTER)
    stubbed.autoscaling.add_response(
        "describe_auto_scaling_groups", {"AutoScalingGroups": []}, {"AutoScalingGroupNames": [ASG_NAME]}
    )

    plan, code, detail = prepare()

    assert (plan, code, detail) == (PLAN, None, None)


def test_execute_request_shapes(stubbed):
    stubbed.ec2.add_response(
        "create_image",
        {"ImageId": IMAGE_ID},
        {
            "InstanceId": SOURCE,
            "Name": IMAGE_NAME,
            "Description": f"vigilantis EC2_ENABLE_AUTOSCALING {SOURCE} {EXECUTION}",
            "NoReboot": True,
            "TagSpecifications": [
                {"ResourceType": "image", "Tags": TAGS},
                {"ResourceType": "snapshot", "Tags": TAGS},
            ],
        },
    )
    stubbed.ec2.add_response("describe_images", {"Images": [{"ImageId": IMAGE_ID, "State": "pending"}]}, {"ImageIds": [IMAGE_ID]})
    stubbed.ec2.add_response("describe_images", {"Images": [{"ImageId": IMAGE_ID, "State": "available"}]}, {"ImageIds": [IMAGE_ID]})
    stubbed.ec2.add_response(
        "create_launch_template",
        {
            "LaunchTemplate": {
                "LaunchTemplateId": TEMPLATE_ID,
                "LaunchTemplateName": LT_NAME,
                "LatestVersionNumber": 1,
                "DefaultVersionNumber": 1,
            }
        },
        {
            "LaunchTemplateName": LT_NAME,
            "VersionDescription": f"vigilantis {EXECUTION}",
            "ClientToken": EXECUTION,  # 같은 실행의 SDK 재시도는 같은 토큰 — Stubber가 SDK 검증을 거친다
            "LaunchTemplateData": expected_template_data(IMAGE_ID),
            "TagSpecifications": [{"ResourceType": "launch-template", "Tags": TAGS}],
        },
    )
    stubbed.autoscaling.add_response(
        "create_auto_scaling_group",
        {},
        {
            "AutoScalingGroupName": ASG_NAME,
            "LaunchTemplate": {"LaunchTemplateId": TEMPLATE_ID, "Version": "1"},
            "MinSize": 1,
            "MaxSize": 3,
            "DesiredCapacity": 1,
            "VPCZoneIdentifier": SUBNET,
            "DefaultInstanceWarmup": 300,
            "Tags": [
                {
                    "ResourceId": ASG_NAME,
                    "ResourceType": "auto-scaling-group",
                    "Key": tag["Key"],
                    "Value": tag["Value"],
                    "PropagateAtLaunch": False,
                }
                for tag in TAGS
            ],
        },
    )
    stubbed.autoscaling.add_response(
        "put_scaling_policy",
        {"PolicyARN": "arn:aws:autoscaling:policy", "Alarms": []},
        {
            "AutoScalingGroupName": ASG_NAME,
            "PolicyName": SCALING_POLICY_NAME,
            "PolicyType": "TargetTrackingScaling",
            "TargetTrackingConfiguration": {
                "PredefinedMetricSpecification": {"PredefinedMetricType": "ASGAverageCPUUtilization"},
                "TargetValue": 50.0,
            },
        },
    )

    outcome = execute(PLAN)

    assert outcome.succeeded, outcome.error_summary
    assert stubbed.sleeps == [7]


def test_inspect_request_shapes(stubbed):
    stubbed.ec2.add_response(
        "describe_images",
        {
            "Images": [
                {
                    "ImageId": IMAGE_ID,
                    "Name": IMAGE_NAME,
                    "OwnerId": ACCOUNT,
                    "State": "available",
                    "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"SnapshotId": SNAPSHOT_ID}}],
                    "Tags": TAGS,
                }
            ]
        },
        IMAGE_FILTER,
    )
    stubbed.ec2.add_response(
        "describe_snapshots",
        {"Snapshots": [{"SnapshotId": SNAPSHOT_ID, "OwnerId": ACCOUNT, "State": "completed", "Tags": TAGS}]},
        {"OwnerIds": ["self"], "Filters": [{"Name": "snapshot-id", "Values": [SNAPSHOT_ID]}]},
    )
    stubbed.ec2.add_response(
        "describe_launch_templates",
        {"LaunchTemplates": [{"LaunchTemplateId": TEMPLATE_ID, "LaunchTemplateName": LT_NAME, "Tags": TAGS}]},
        TEMPLATE_FILTER,
    )
    observed = expected_template_data(IMAGE_ID)
    observed["MetadataOptions"] = {**observed["MetadataOptions"], "State": "applied"}
    stubbed.ec2.add_response(
        "describe_launch_template_versions",
        {
            "LaunchTemplateVersions": [
                {"LaunchTemplateId": TEMPLATE_ID, "VersionNumber": 1, "LaunchTemplateData": observed}
            ]
        },
        {"LaunchTemplateId": TEMPLATE_ID, "Versions": ["1"]},
    )
    group = group_record(template_id=TEMPLATE_ID, tags=TAGS, instances=[asg_instance(LAUNCHED, TEMPLATE_ID)])
    policy = {
        "AutoScalingGroupName": ASG_NAME,
        "PolicyName": SCALING_POLICY_NAME,
        "PolicyARN": "arn:aws:autoscaling:policy",
        "PolicyType": "TargetTrackingScaling",
        "TargetTrackingConfiguration": {
            "PredefinedMetricSpecification": {"PredefinedMetricType": "ASGAverageCPUUtilization"},
            "TargetValue": 50.0,
        },
        "Enabled": True,
    }
    # 설정 대조(ASG → 정책) 1회 + 부팅 확인(ASG) 1회 → ready 직전 정책 재조회 1회
    for _ in range(2):
        stubbed.autoscaling.add_response(
            "describe_auto_scaling_groups", {"AutoScalingGroups": [group]}, {"AutoScalingGroupNames": [ASG_NAME]}
        )
        stubbed.autoscaling.add_response(
            "describe_policies",
            {"ScalingPolicies": [policy]},
            {"AutoScalingGroupName": ASG_NAME, "PolicyNames": [SCALING_POLICY_NAME]},
        )
    stubbed.ec2.add_response(
        "describe_instance_status",
        {
            "InstanceStatuses": [
                {
                    "InstanceId": LAUNCHED,
                    "InstanceState": {"Name": "running", "Code": 16},
                    "SystemStatus": {"Status": "ok"},
                    "InstanceStatus": {"Status": "ok"},
                }
            ]
        },
        {"InstanceIds": [LAUNCHED], "IncludeAllInstances": True},
    )

    inspection = ex.inspect_ec2_autoscaling(PLAN)

    assert inspection.status == "ready", inspection.summary
    assert stubbed.sleeps == []
