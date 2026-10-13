# scripts/provision_smoke_aws.py 검증 — 실 AWS 스모크 환경(ADR-0009)이 코드로 지키는 약속.
#
#   ① 앱 IAM 정책은 코드가 부르는 AWS 작업의 거울이다. 실행 경로를 더한 PR이 정책을
#      빠뜨리면 여기서 깨진다 — 스모크 당일 AccessDenied로 처음 드러나지 않게. 반대로
#      코드가 부르지 않는 조치 권한이 정책에 끼어도 깨진다. 생성 작업은 새 자원이 아니라
#      생성 장소로 가둬야 한다 — 새 자원에는 기존 자원의 조건 키가 성립하지 않는다.
#   ② 실 AWS 전용 가드 — LocalStack 엔드포인트가 잡혀 있으면 AWS를 부르기 전에 멈춘다.
#   ③ 실 AWS에서만 드러나는 배치 조건 두 개(NACL 허용 번호·RIGHTSIZING 대상 타입).
#   ④ 끊긴 초기화 — 만든 직후의 설정(SG 규칙·NACL 허용·TG 대상 등록)이 도중에 실패하면
#      다음 up이 이어서 끝내고, 설정이 끝난 자원은 스모크가 바꾼 그대로 둔다.
#   ⑤ 앱 정책 저장 — 사용자 인라인 한도(합계 2,048자)로는 들어가지 않아 관리형으로 붙는다.
#      크기가 관리형 한도(6,144자)를 넘기는 PR은 up이 아니라 여기서 깨진다. 전환 전에 붙은
#      인라인이 남으면 앱 권한이 두 문서의 합집합이 되므로 up이 걷는다.
#   ⑥ 비용 알림 — Budget은 크레딧을 빼기 전 사용액으로 재고, 이미 선 Budget도 개정된 정의로 맞춘다.
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다. AWS를 부르지 않는다(④⑤는 대역).

from __future__ import annotations

import copy
import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from pydantic import TypeAdapter, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "provision_smoke_aws.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("provision_smoke_aws", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclass가 자기 모듈을 sys.modules에서 찾는다 — 등록하지 않으면 정의가 깨진다
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_script()  # 스크립트가 apps/core-api·packages를 sys.path에 넣는다

from schemas import autoscaling as autoscaling_schema
from schemas.rightsizing_policy import rightsizing_target_type
from schemas.runbook_parameters import RuleNumber
from services.aws import autoscaling as aws_autoscaling
from services.aws import executor, rollback

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
VPC_ID = "vpc-0123456789abcdef0"  # 실 VPC ID와 같은 길이(17자리 16진) — ⑤의 크기 계측이 이 길이에 기댄다
SUBNET_IDS = [
    "subnet-0123456789abcdef0",
    "subnet-0fedcba9876543210",
]  # 실 서브넷 ID와 같은 길이
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/vigilantis-smoke-instance"
POLICY = smoke.build_app_policy(ACCOUNT, REGION, VPC_ID, SUBNET_IDS)
POLICY_WITH_ROLE = smoke.build_app_policy(
    ACCOUNT, REGION, VPC_ID, SUBNET_IDS, [ROLE_ARN]
)

# boto3 클라이언트 이름 → IAM 서비스 접두
_IAM_SERVICE = {
    "ec2": "ec2",
    "elbv2": "elasticloadbalancing",
    "autoscaling": "autoscaling",
    "cloudwatch": "cloudwatch",
}


def _iam_action(operation: str) -> str:
    """ "ec2.modify_instance_attribute" → "ec2:ModifyInstanceAttribute"."""
    service, name = operation.split(".", 1)
    return f"{_IAM_SERVICE[service]}:{''.join(part.capitalize() for part in name.split('_'))}"


def _code_operations() -> set[str]:
    """코드가 부르는 AWS 작업 — precheck 명세(RUNBOOK_SPECS)와 실행·판정의 작업 상수."""
    operations = {
        op for spec in executor.RUNBOOK_SPECS.values() for op in spec.operations
    }
    for module in (executor, rollback, aws_autoscaling):
        operations |= {
            value
            for name, value in vars(module).items()
            if name.startswith("_OP_") and isinstance(value, str)
        }
    return operations


# 코드가 직접 부르지 않지만 AWS가 호출자에게 요구하는 권한 → 그것을 요구하는 직접 작업(IAM 형식).
# 직접 작업(_code_operations)과 섞지 않는다 — 여기 항목은 트리거 작업이 코드에 있을 때만 허용되고,
# 트리거가 사라지면 정책에서도 빠져야 한다. RunInstances는 앱이 부르지 않는다(ASG 서비스가 부른다).
_AUTHORIZATION_DEPENDENCIES = {
    "ec2:CreateTags": {
        "ec2:CreateImage",
        "ec2:CreateLaunchTemplate",
        "autoscaling:CreateAutoScalingGroup",
    },
    "ec2:RunInstances": {"autoscaling:CreateAutoScalingGroup"},
    "autoscaling:CreateOrUpdateTags": {"autoscaling:CreateAutoScalingGroup"},
}
# 운영자가 역할 허용 목록을 줄 때만 더해지는 의존 권한
_OPTIONAL_DEPENDENCIES = {"iam:PassRole": {"autoscaling:CreateAutoScalingGroup"}}
# 승인된 실행 경로의 직접 작업 — 상수가 빠지면 아래 거울이 조용히 비지 않게 못 박는다
_RUNTIME_DIRECT = {
    "ec2:CreateImage",
    "ec2:CreateLaunchTemplate",
    "autoscaling:CreateAutoScalingGroup",
    "autoscaling:PutScalingPolicy",
}
# #446 서비스 전환이 새로 부르는 유일한 앱 권한(다른 작업자의 보호 코드가 아직 없어도 정책은 이것만 더한다)
_SERVICE_446_ACTIONS = {"autoscaling:SetInstanceProtection"}
# 앱 키가 어떤 정책 변형에서도 가지면 안 되는 작업(삭제·폐기·부착·역할 변경·ELB/DNS)
_NEVER_GRANTED = (
    "autoscaling:DetachLoadBalancerTargetGroups",
    "autoscaling:DetachInstances",
    "autoscaling:EnterStandby",
    "autoscaling:ExitStandby",
    "elasticloadbalancing:CreateListener",
    "elasticloadbalancing:ModifyListener",
    "elasticloadbalancing:CreateRule",
    "elasticloadbalancing:ModifyRule",
    "elasticloadbalancing:SetRulePriorities",
    "ec2:DeregisterImage",
    "ec2:DeleteSnapshot",
    "ec2:DeleteLaunchTemplate",
    "ec2:DeleteLaunchTemplateVersions",
    "ec2:TerminateInstances",
    "ec2:AttachVolume",
    "ec2:CopyImage",
    "ec2:ModifyImageAttribute",
    "ec2:ModifySnapshotAttribute",
    "ec2:DeleteTags",
    "ec2:CreateLaunchTemplateVersion",
    "autoscaling:DeleteAutoScalingGroup",
    "autoscaling:UpdateAutoScalingGroup",
    "autoscaling:AttachInstances",
    "autoscaling:SetDesiredCapacity",
    "autoscaling:DeletePolicy",
    "autoscaling:AttachLoadBalancerTargetGroups",
    "autoscaling:TerminateInstanceInAutoScalingGroup",
    "elasticloadbalancing:CreateTargetGroup",
    "elasticloadbalancing:CreateLoadBalancer",
    "elasticloadbalancing:DeleteTargetGroup",
    "elasticloadbalancing:ModifyTargetGroup",
    "route53:ChangeResourceRecordSets",
    "iam:CreateRole",
    "iam:AttachRolePolicy",
    "iam:PutRolePolicy",
    "iam:CreateServiceLinkedRole",
    "iam:CreateInstanceProfile",
    "iam:AddRoleToInstanceProfile",
)


def _granted(policy: dict = POLICY) -> list[str]:
    return [
        action for statement in policy["Statement"] for action in statement["Action"]
    ]


def _is_granted(action: str, policy: dict = POLICY) -> bool:
    return any(fnmatch.fnmatchcase(action, pattern) for pattern in _granted(policy))


def _statements(action: str, policy: dict = POLICY) -> list[dict]:
    return [s for s in policy["Statement"] if action in s["Action"]]


def _resources(statement: dict) -> list[str]:
    resources = statement["Resource"]
    return resources if isinstance(resources, list) else [resources]


def test_policy_grants_every_aws_operation_the_code_calls():
    missing = sorted(
        a for a in {_iam_action(op) for op in _code_operations()} if not _is_granted(a)
    )
    assert not missing, f"앱 정책에 없는 작업 — build_app_policy에 더할 것: {missing}"


def _direct_actions() -> set[str]:
    return {_iam_action(op) for op in _code_operations()}


def _active_dependencies(optional: bool = False) -> set[str]:
    direct = _direct_actions()
    table = {
        **_AUTHORIZATION_DEPENDENCIES,
        **(_OPTIONAL_DEPENDENCIES if optional else {}),
    }
    return {action for action, triggers in table.items() if triggers & direct}


def test_policy_grants_only_required_mutations_and_dependencies():
    """허용은 직접 작업 + 트리거가 코드에 있는 명시적 의존 권한뿐이다(PassRole 제외 — 아래)."""
    allowed = _direct_actions() | _active_dependencies() | _SERVICE_446_ACTIONS
    extra = sorted(
        a for a in _granted() if a not in smoke.READ_ACTIONS and a not in allowed
    )
    assert not extra, f"코드가 부르지 않는 조치 권한: {extra}"


def test_runtime_operations_are_direct_and_dependencies_are_not():
    direct = _direct_actions()
    assert _RUNTIME_DIRECT <= direct, sorted(_RUNTIME_DIRECT - direct)
    assert not set(_AUTHORIZATION_DEPENDENCIES) & direct, (
        "의존 권한을 코드가 직접 부르면 직접 작업 쪽으로 옮길 것"
    )
    assert not set(_OPTIONAL_DEPENDENCIES) & direct
    for dependency, triggers in {
        **_AUTHORIZATION_DEPENDENCIES,
        **_OPTIONAL_DEPENDENCIES,
    }.items():
        assert triggers & direct, (
            f"{dependency}: 트리거 작업이 코드에 없다 — 의존 항목을 걷을 것"
        )


def test_policy_grants_every_authorization_dependency():
    missing = sorted(a for a in _active_dependencies() if not _is_granted(a))
    assert not missing, f"AWS가 요구하는 의존 권한이 없다: {missing}"


def test_policy_never_grants_destructive_or_out_of_scope_actions():
    for policy in (POLICY, POLICY_WITH_ROLE):
        wildcards = [
            a for a in _granted(policy) if "*" in a and a not in smoke.READ_ACTIONS
        ]
        assert not wildcards, wildcards
        granted = sorted(a for a in _NEVER_GRANTED if _is_granted(a, policy))
        assert not granted, granted


# 조회 쪽 거울. READ_ACTIONS는 손으로 적은 목록이고 바로 위 ②방향 테스트가 그것을 통째로 제외하므로,
# 수집기가 Describe* 밖의 조회를 더하면 정책이 그대로여도 초록불이다. 조치 쪽보다 더 조용히 새는데,
# 수집기는 autoscaling·elbv2·launch template 조회를 _safe_describe로 감싸 AccessDenied까지 빈 목록
# + collector_failures로 강등하고 회차를 PARTIAL로 마감하기 때문이다(ADR-0006 §4, C4) — LocalStack
# 에서 늘 보던 PARTIAL과 겉모습이 같다(#348 리뷰). 실행·원복·백업의 조회는 RUNBOOK_SPECS와 _OP_*
# 상수가 이미 위 두 방향으로 비춘다.
_COLLECTOR = REPO_ROOT / "apps" / "core-api" / "services" / "collector.py"


def _collector_clients(source: str) -> dict[str, str]:
    """수집기가 만드는 클라이언트 변수 → boto3 서비스. 변수 이름을 여기 박지 않고 소스에서 읽는다
    — 박아 두면 수집기가 이름을 바꿨을 때 찾는 호출이 조용히 줄어든다."""
    clients = dict(re.findall(r'(\w+) = aws_client\("([a-z0-9]+)"', source))
    unknown = sorted(
        {service for service in clients.values() if service not in _IAM_SERVICE}
    )
    assert not unknown, f"_IAM_SERVICE에 IAM 접두가 없는 수집기 클라이언트: {unknown}"
    return clients


def test_policy_grants_every_read_the_collector_makes():
    source = _COLLECTOR.read_text(encoding="utf-8")
    clients = _collector_clients(source)
    assert clients, (
        "수집기의 aws_client 대입을 하나도 못 찾았다 — 패턴이 코드와 어긋났다"
    )
    names = "|".join(sorted(clients))
    patterns = (
        rf"\b({names})\.(?!get_paginator\b)([a-z0-9_]+)\(",  # 직접 호출
        rf'\b({names})\.get_paginator\("([a-z0-9_]+)"\)',  # 페이지네이터
        rf'_paginate\(({names}), "([a-z0-9_]+)"',  # 공통 헬퍼
    )
    calls = {
        _iam_action(f"{clients[variable]}.{operation}")
        for pattern in patterns
        for variable, operation in re.findall(pattern, source)
    }
    assert calls, "수집기 호출을 하나도 못 찾았다 — 패턴이 코드와 어긋났다"
    missing = sorted(action for action in calls if not _is_granted(action))
    assert not missing, f"앱 정책의 조회 권한에 없는 수집기 호출: {missing}"


def test_target_registration_baseline_is_kept_and_smoke_tag_fenced():
    """EC2_ISOLATE·UNISOLATE의 등록 해제·재등록(최신 dev 기준선) — #446이 ALB 생성 권한을 더하지 않는다."""
    for action in (
        "elasticloadbalancing:RegisterTargets",
        "elasticloadbalancing:DeregisterTargets",
    ):
        (statement,) = _statements(action)
        assert sorted(statement["Action"]) == [
            "elasticloadbalancing:DeregisterTargets",
            "elasticloadbalancing:RegisterTargets",
        ]
        assert (
            statement["Resource"]
            == f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/*"
        )
        assert _conditions(statement) == {
            "StringEquals": {"aws:ResourceTag/vigilantis:smoke": "true"}
        }
    assert not [
        a
        for a in _granted()
        if a.startswith("elasticloadbalancing:")
        and "Targets" not in a
        and a not in smoke.READ_ACTIONS
    ]


def test_every_change_statement_is_fenced():
    """조회 문장만 Resource "*"를 쓰고, 그것도 리전 조건으로 묶인다. 조치 문장은 리전이
    박힌 ARN이며, 특정 VPC 하나를 가리키지 않는 한 태그·VPC 조건이 붙는다."""
    subnet_arns = {f"arn:aws:ec2:{REGION}:{ACCOUNT}:subnet/{s}" for s in SUBNET_IDS}
    for statement in POLICY_WITH_ROLE["Statement"]:
        resources = _resources(statement)
        if set(statement["Action"]) <= set(smoke.READ_ACTIONS):
            assert statement["Condition"] == {
                "StringEquals": {"aws:RequestedRegion": REGION}
            }
            continue
        assert "*" not in resources, statement["Action"]
        if statement["Action"] == ["iam:PassRole"]:
            # IAM 역할 ARN에는 리전이 없다 — 리전 울타리 예외. 대신 계정·역할 이름이 박히고 서비스가 묶인다.
            assert resources == [ROLE_ARN]
            assert statement["Condition"] == {
                "StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}
            }
            continue
        assert all(f":{REGION}:" in r for r in resources), statement["Action"]
        pinned = (
            all(r.endswith(VPC_ID) for r in resources) or set(resources) == subnet_arns
        )
        # 새 스냅숏·새 SG: 생성 장소 쪽 문장이 가둔다(아래 _CREATE_ACTIONS 테스트)
        new_resource_only = statement["Action"] in (
            ["ec2:CreateSnapshot"],
            ["ec2:CreateSecurityGroup"],
        ) and (any("::snapshot/" in r or ":security-group/" in r for r in resources))
        assert "Condition" in statement or pinned or new_resource_only, statement[
            "Action"
        ]


# 생성 작업 → (새로 생기는 자원, 생성 장소 자원)의 ARN 조각. AWS 서비스 권한 참조상 새 자원이
# 받는 조건 키는 요청 태그 계열뿐이라 ec2:Vpc·aws:ResourceTag로는 가둘 수 없다 — 붙이면 그 Allow가
# 영영 성립하지 않아 권한 거부로 끝난다(#348 리뷰). 울타리는 생성 장소 쪽 문장이 친다.
_CREATE_ACTIONS = {
    "ec2:CreateSecurityGroup": (":security-group/", ":vpc/"),
    "ec2:CreateSnapshot": ("::snapshot/", ":volume/"),
    "ec2:CreateImage": (
        "::image/",
        ":instance/",
    ),  # 스냅숏도 새 자원 — 같은 문장이 요청 태그로 가둔다
}
_NEW_RESOURCE_KEYS = ("aws:RequestTag/", "aws:TagKeys")


def test_create_actions_are_fenced_at_the_place_not_the_new_resource():
    for action, (new_part, place_part) in _CREATE_ACTIONS.items():
        statements = [s for s in POLICY["Statement"] if action in s["Action"]]
        new = [s for s in statements if any(new_part in r for r in _resources(s))]
        place = [s for s in statements if any(place_part in r for r in _resources(s))]
        assert new, f"{action}: 새 자원 쪽 허용 문장이 없다"
        assert place, f"{action}: 생성 장소 쪽 허용 문장이 없다"
        for statement in new:
            keys = [
                k
                for operator in statement.get("Condition", {}).values()
                for k in operator
            ]
            unsupported = [k for k in keys if not k.startswith(_NEW_RESOURCE_KEYS)]
            assert not unsupported, (
                f"{statement['Action']}: 새 자원에 없는 조건 키 {unsupported}"
            )
        for statement in place:
            pinned = all(r.endswith(VPC_ID) for r in _resources(statement))
            assert "Condition" in statement or pinned, (
                f"{statement['Action']}: 생성 장소가 열려 있다"
            )


def _conditions(statement: dict) -> dict:
    return statement.get("Condition", {})


_RUN_TAGS_REQUIRED = {
    "StringEquals": {"aws:RequestTag/vigilantis:smoke": "true"},
    "Null": {
        "aws:RequestTag/vigilantis:execution-id": "false",
        "aws:RequestTag/vigilantis:source-instance": "false",
    },
}


def test_new_runtime_resources_require_execution_and_source_tags():
    ec2 = f"arn:aws:ec2:{REGION}"
    (new,) = [
        s
        for s in _statements("ec2:CreateImage")
        if "ec2:CreateLaunchTemplate" in s["Action"]
    ]
    assert _statements("ec2:CreateLaunchTemplate") == [new]
    assert new["Action"] == ["ec2:CreateImage", "ec2:CreateLaunchTemplate"]
    # 서비스 권한 참조의 자원 유형(새 자원): CreateImage → image·snapshot, CreateLaunchTemplate → launch-template.
    # 문장의 자원 유형이 이 합집합을 넘으면 한쪽 작업에 적용되지 않는 자원이 끼어든 것이다.
    assert sorted(r.split(":")[-1] for r in _resources(new)) == [
        "image/*",
        "launch-template/*",
        "snapshot/*",
    ]
    assert f"{ec2}::image/*" in _resources(new) and f"{ec2}::snapshot/*" in _resources(
        new
    )  # 계정 없는 ARN
    assert _resources(new)[2] == f"{ec2}:{ACCOUNT}:launch-template/*"
    assert _conditions(new) == _RUN_TAGS_REQUIRED
    (source,) = [s for s in _statements("ec2:CreateImage") if s is not new]
    assert _resources(source) == [
        f"{ec2}:{ACCOUNT}:instance/*"
    ]  # 원본 인스턴스(instance*: 리소스 태그)
    assert _conditions(source) == {
        "StringEquals": {"aws:ResourceTag/vigilantis:smoke": "true"}
    }


def test_create_tags_is_limited_to_creation_time_tagging_of_runtime_resources():
    (statement,) = _statements("ec2:CreateTags")
    assert _conditions(statement) == {
        "StringEquals": {
            "ec2:CreateAction": ["CreateImage", "CreateLaunchTemplate", "RunInstances"]
        },
        "ForAllValues:StringEquals": {"aws:TagKeys": list(smoke.CREATE_TAG_KEYS)},
        "Null": {"aws:TagKeys": "false"},  # ForAllValues는 키가 없으면 참이다
    }
    assert set(smoke.CREATE_TAG_KEYS) == {
        "vigilantis:smoke",
        "vigilantis:execution-id",
        "vigilantis:source-instance",
        "Environment",
        "environment",
    }
    assert set(smoke.CREATE_TAG_KEYS) == {
        autoscaling_schema.EXECUTION_TAG_KEY,
        autoscaling_schema.SOURCE_INSTANCE_TAG_KEY,
        *autoscaling_schema.PRESERVED_SOURCE_TAG_KEYS,
    }
    kinds = sorted(r.split(":")[-1].split("/")[0] for r in _resources(statement))
    assert kinds == ["image", "instance", "launch-template", "snapshot", "volume"]
    assert all(r.endswith("/*") and f":{REGION}:" in r for r in _resources(statement))


def test_autoscaling_group_actions_are_fenced_by_name_region_account_subnets_and_size():
    asg_arn = f"arn:aws:autoscaling:{REGION}:{ACCOUNT}:autoScalingGroup:*:autoScalingGroupName/vigilantis-asg-*"
    for action in (
        "autoscaling:CreateAutoScalingGroup",
        "autoscaling:CreateOrUpdateTags",
        "autoscaling:PutScalingPolicy",
    ):
        (statement,) = _statements(action)
        assert _resources(statement) == [asg_arn], action
    (create,) = _statements("autoscaling:CreateAutoScalingGroup")
    assert _conditions(create) == {
        "StringEquals": {"aws:RequestTag/vigilantis:smoke": "true"},
        "NumericLessThanEquals": {
            "autoscaling:MaxSize": "4",
            "autoscaling:MinSize": "4",
        },
        "Bool": {"autoscaling:LaunchTemplateVersionSpecified": "true"},
        "ForAllValues:StringEquals": {
            "autoscaling:VPCZoneIdentifiers": sorted(SUBNET_IDS)
        },
        "Null": {  # ForAllValues는 키가 없으면 참이다 + 실행 ID·원본 인스턴스 태그 필수
            "autoscaling:VPCZoneIdentifiers": "false",
            "aws:RequestTag/vigilantis:execution-id": "false",
            "aws:RequestTag/vigilantis:source-instance": "false",
        },
    }
    (tags,) = _statements("autoscaling:CreateOrUpdateTags")
    assert _conditions(tags) == _RUN_TAGS_REQUIRED
    # CPU 목표 추적 경보는 ASG 서비스 연결 역할이 관리한다 — 앱에 경보 권한은 없다
    assert not [
        a
        for a in _granted(POLICY_WITH_ROLE)
        if a.startswith("cloudwatch:") and a not in smoke.READ_ACTIONS
    ]
    (scaling,) = _statements("autoscaling:PutScalingPolicy")
    assert _conditions(scaling) == {
        "StringEquals": {"aws:ResourceTag/vigilantis:smoke": "true"}
    }


def test_set_instance_protection_is_limited_to_managed_asgs_with_the_smoke_tag():
    asg_arn = f"arn:aws:autoscaling:{REGION}:{ACCOUNT}:autoScalingGroup:*:autoScalingGroupName/vigilantis-asg-*"
    for policy in (POLICY, POLICY_WITH_ROLE):
        (statement,) = _statements("autoscaling:SetInstanceProtection", policy)
        assert _resources(statement) == [asg_arn]
        assert _conditions(statement) == {
            "StringEquals": {"aws:ResourceTag/vigilantis:smoke": "true"}
        }
        assert sorted(statement["Action"]) == [
            "autoscaling:PutScalingPolicy",
            "autoscaling:SetInstanceProtection",
        ]
    assert not [
        a
        for a in _granted()
        if a.startswith("autoscaling:")
        and a not in smoke.READ_ACTIONS
        and a
        not in {
            "autoscaling:CreateAutoScalingGroup",
            "autoscaling:CreateOrUpdateTags",
            "autoscaling:PutScalingPolicy",
            "autoscaling:SetInstanceProtection",
        }
    ]


def test_run_instances_dependency_is_fenced_per_supported_resource_type():
    """앱은 RunInstances를 부르지 않는다 — CreateAutoScalingGroup이 요구하는 의존 권한이다."""
    assert "ec2:RunInstances" not in _direct_actions()
    ec2 = f"arn:aws:ec2:{REGION}:{ACCOUNT}"
    subnet_arns = sorted(f"{ec2}:subnet/{s}" for s in SUBNET_IDS)
    by_resource: dict[str, dict] = {}
    for statement in _statements("ec2:RunInstances"):
        for resource in _resources(statement):
            assert f":{REGION}:" in resource and "*" not in resource.split("/", 1)[0]
            kind = resource.split(":")[-1].split("/")[0]
            assert by_resource.setdefault(kind, statement) is statement, (
                f"{kind}: 자원 유형이 두 문장에 걸쳤다"
            )
    # RunInstances는 어떤 자원 유형 문장에서도 다른 작업과 섞이지 않는다(자원마다 지원 조건 키가 다르다)
    assert all(
        s["Action"] == ["ec2:RunInstances"] for s in _statements("ec2:RunInstances")
    )
    assert sorted(by_resource) == [
        "image",
        "instance",
        "launch-template",
        "network-interface",
        "security-group",
        "snapshot",
        "subnet",
        "volume",
    ]
    for kind in ("instance", "volume"):
        assert _conditions(by_resource[kind]) == _RUN_TAGS_REQUIRED
    smoke_tag = {"aws:ResourceTag/vigilantis:smoke": "true"}
    # ec2:Owner는 계정 없는 image·snapshot ARN에만 — launch-template(계정이 ARN에 있다)은 별도 문장
    for kind in ("image", "snapshot"):
        assert _conditions(by_resource[kind]) == {
            "StringEquals": {**smoke_tag, "ec2:Owner": ACCOUNT}
        }
    assert by_resource["image"] is by_resource["snapshot"]
    assert _conditions(by_resource["launch-template"]) == {"StringEquals": smoke_tag}
    assert by_resource["launch-template"] is not by_resource["image"]
    assert sorted(_resources(by_resource["subnet"])) == subnet_arns
    assert not _conditions(by_resource["subnet"])  # 서브넷은 정확한 ARN
    assert not any(
        ":key-pair/" in r
        for s in _statements("ec2:RunInstances")
        for r in _resources(s)
    )  # LT에 KeyName 없음
    vpc_arn = f"{ec2}:vpc/{VPC_ID}"
    assert _conditions(by_resource["network-interface"]) == {
        "ArnEquals": {"ec2:Vpc": vpc_arn, "ec2:Subnet": subnet_arns}
    }
    assert _conditions(by_resource["security-group"]) == {
        "ArnEquals": {"ec2:Vpc": vpc_arn}
    }
    # 스냅숏·이미지 ARN에는 계정이 없다(서비스 권한 참조의 ARN 형식)
    for kind in ("image", "snapshot"):
        assert any(
            r.startswith(f"arn:aws:ec2:{REGION}::{kind}/")
            for r in _resources(by_resource[kind])
        )


def test_pass_role_is_absent_without_an_operator_allowlist():
    assert not _is_granted("iam:PassRole")
    assert "iam:PassRole" not in _active_dependencies()  # 선택 의존은 목록이 있을 때만


def test_pass_role_is_limited_to_the_allowlist_and_ec2():
    assert _is_granted("iam:PassRole", POLICY_WITH_ROLE)
    assert "iam:PassRole" in _active_dependencies(optional=True)
    (statement,) = _statements("iam:PassRole", POLICY_WITH_ROLE)
    assert statement["Resource"] == [
        ROLE_ARN
    ]  # 리전 없는 IAM ARN — 리전 울타리의 유일한 예외
    assert f":{REGION}:" not in ROLE_ARN
    assert statement["Condition"] == {
        "StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}
    }
    assert not any(
        a.startswith("iam:") and a != "iam:PassRole" for a in _granted(POLICY_WITH_ROLE)
    )


@pytest.mark.parametrize(
    "bad",
    [
        "*",
        f"arn:aws:iam::{ACCOUNT}:role/*",
        f"arn:aws:iam::{ACCOUNT}:role/app-*",
        "arn:aws:iam::999999999999:role/other-account",
        f"arn:aws:iam::{ACCOUNT}:user/not-a-role",
        f"arn:aws:iam:{REGION}:{ACCOUNT}:role/with-region",
    ],
)
def test_pass_role_rejects_wildcard_foreign_or_non_role_arns(bad):
    with pytest.raises(ValueError):
        smoke.build_app_policy(ACCOUNT, REGION, VPC_ID, SUBNET_IDS, [bad])


def test_policy_requires_exact_subnets():
    with pytest.raises(ValueError):
        smoke.build_app_policy(ACCOUNT, REGION, VPC_ID, [])


def test_policy_with_a_role_fits_the_managed_policy_limit():
    assert _policy_chars(POLICY_WITH_ROLE) <= smoke.MANAGED_POLICY_MAX_CHARS
    assert _policy_chars(POLICY) < _policy_chars(POLICY_WITH_ROLE)


def test_too_many_roles_are_rejected_rather_than_loosening_fences():
    roles = [
        f"arn:aws:iam::{ACCOUNT}:role/service-path/vigilantis-smoke-instance-{n}"
        for n in range(8)
    ]
    with pytest.raises(ValueError, match="한도"):
        smoke.build_app_policy(ACCOUNT, REGION, VPC_ID, SUBNET_IDS, roles)


def test_oversized_policy_is_rejected_not_loosened(monkeypatch):
    monkeypatch.setattr(smoke, "MANAGED_POLICY_MAX_CHARS", 100)
    with pytest.raises(ValueError, match="한도"):
        smoke.build_app_policy(ACCOUNT, REGION, VPC_ID, SUBNET_IDS)


def test_nacl_allow_rule_sits_at_the_top_of_what_ai_can_pick():
    """허용 규칙이 AI가 고를 수 있는 번호의 맨 끝이어야 어떤 차단 규칙도 먼저 평가된다."""
    adapter = TypeAdapter(RuleNumber)
    assert (
        adapter.validate_python(smoke.NACL_ALLOW_ALL_RULE) == smoke.NACL_ALLOW_ALL_RULE
    )
    with pytest.raises(ValidationError):
        adapter.validate_python(smoke.NACL_ALLOW_ALL_RULE + 1)


def test_non_production_instance_is_a_rightsizing_candidate():
    """production은 SKIP_PROD_PROTECTED로 빠지므로 FinOps 경로는 non-prod 대상에 달려 있다."""
    candidates = [s for s in smoke.INSTANCES if s.environment != "production"]
    assert candidates
    assert all(rightsizing_target_type(s.instance_type) for s in candidates)


def test_refuses_localstack_endpoint_before_calling_aws():
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "AWS_ENDPOINT_URL": "http://localhost:4566",
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "status", "--account", "000000000000"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode != 0
    assert "실 AWS 전용" in result.stderr


# ------------------------------------------------------------------ ④ 끊긴 초기화
VPC = "vpc-smoke"
SUBNET = "subnet-smoke-a"
_ALL = "0.0.0.0/0"
INSTANCE_IDS = {spec.name: f"i-{n:04d}" for n, spec in enumerate(smoke.INSTANCES)}


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class _Fake:
    """faults[작업 이름] = 호출마다의 실패 여부(앞에서부터 소비). writes는 쓰기 호출 기록."""

    def __init__(self):
        self.faults: dict[str, list[bool]] = {}
        self.writes: list[str] = []
        self._seq = 0

    def _write(self, operation: str) -> None:
        self.writes.append(operation)
        queue = self.faults.get(operation)
        if queue and queue.pop(0):
            raise _client_error("InternalError", operation)

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"


class FakeEc2(_Fake):
    """스모크 VPC 하나의 SG·NACL. 서브넷은 처음에 기본 NACL에 붙어 있다."""

    def __init__(self):
        super().__init__()
        self.groups: dict[str, dict] = {}
        self.acls: dict[str, dict] = {
            "acl-default": {
                "NetworkAclId": "acl-default",
                "IsDefault": True,
                "Tags": [],
                "Entries": [],
                "Associations": [
                    {"NetworkAclAssociationId": "aclassoc-0", "SubnetId": SUBNET}
                ],
            }
        }

    def create_tags(self, Resources, Tags):
        self._write("create_tags")
        keys = {t["Key"] for t in Tags}
        for resource_id in Resources:
            resource = self.groups.get(resource_id) or self.acls[resource_id]
            resource["Tags"] = [
                t for t in resource["Tags"] if t["Key"] not in keys
            ] + copy.deepcopy(Tags)

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": copy.deepcopy(list(self.groups.values()))}

    def create_security_group(self, GroupName, Description, VpcId, TagSpecifications):
        self._write("create_security_group")
        group_id = self._new_id("sg")
        self.groups[group_id] = {
            "GroupId": group_id,
            "GroupName": GroupName,
            "Tags": copy.deepcopy(TagSpecifications[0]["Tags"]),
            "IpPermissions": [],
            # AWS가 새 VPC SG에 넣는 기본 egress
            "IpPermissionsEgress": [
                {"IpProtocol": "-1", "IpRanges": [{"CidrIp": _ALL}]}
            ],
        }
        return {"GroupId": group_id}

    def authorize_security_group_ingress(self, GroupId, IpPermissions):
        self._write("authorize_security_group_ingress")
        rules = self.groups[GroupId]["IpPermissions"]
        if any(p in rules for p in IpPermissions):
            raise _client_error(
                "InvalidPermission.Duplicate", "AuthorizeSecurityGroupIngress"
            )
        rules.extend(copy.deepcopy(IpPermissions))

    def revoke_security_group_ingress(self, GroupId, IpPermissions):
        self._write("revoke_security_group_ingress")
        for p in IpPermissions:
            self.groups[GroupId]["IpPermissions"].remove(p)  # 없는 규칙이면 ValueError

    def revoke_security_group_egress(self, GroupId, IpPermissions):
        self._write("revoke_security_group_egress")
        for p in IpPermissions:
            self.groups[GroupId]["IpPermissionsEgress"].remove(p)

    def describe_network_acls(self, Filters):
        names = next((f["Values"] for f in Filters if f["Name"] == "tag:Name"), None)
        acls = [
            a
            for a in self.acls.values()
            if names is None
            or next((t["Value"] for t in a["Tags"] if t["Key"] == "Name"), None)
            in names
        ]
        return {"NetworkAcls": copy.deepcopy(acls)}

    def create_network_acl(self, VpcId, TagSpecifications):
        self._write("create_network_acl")
        acl_id = self._new_id("acl")
        self.acls[acl_id] = {
            "NetworkAclId": acl_id,
            "IsDefault": False,
            "Tags": copy.deepcopy(TagSpecifications[0]["Tags"]),
            # 실 AWS의 커스텀 NACL은 deny-all(32767)만 갖고 태어난다
            "Entries": [
                {
                    "RuleNumber": 32767,
                    "Egress": egress,
                    "RuleAction": "deny",
                    "Protocol": "-1",
                    "CidrBlock": _ALL,
                }
                for egress in (False, True)
            ],
            "Associations": [],
        }
        return {"NetworkAcl": copy.deepcopy(self.acls[acl_id])}

    def create_network_acl_entry(
        self, NetworkAclId, RuleNumber, Protocol, RuleAction, Egress, CidrBlock
    ):
        self._write("create_network_acl_entry")
        entries = self.acls[NetworkAclId]["Entries"]
        if any(
            e["RuleNumber"] == RuleNumber and e["Egress"] == Egress for e in entries
        ):
            raise _client_error("NetworkAclEntryAlreadyExists", "CreateNetworkAclEntry")
        entries.append(
            {
                "RuleNumber": RuleNumber,
                "Egress": Egress,
                "RuleAction": RuleAction,
                "Protocol": Protocol,
                "CidrBlock": CidrBlock,
            }
        )

    def replace_network_acl_association(self, AssociationId, NetworkAclId):
        self._write("replace_network_acl_association")
        for acl in self.acls.values():
            for assoc in [
                a
                for a in acl["Associations"]
                if a["NetworkAclAssociationId"] == AssociationId
            ]:
                acl["Associations"].remove(assoc)
                self.acls[NetworkAclId]["Associations"].append(
                    {**assoc, "NetworkAclAssociationId": self._new_id("aclassoc")}
                )

    def smoke_acl(self) -> dict:
        return next(a for a in self.acls.values() if not a["IsDefault"])


class FakeElbv2(_Fake):
    """Target Group 하나. ALB와 리스너는 이미 있다고 둔다 — TG 초기화만 본다."""

    def __init__(self):
        super().__init__()
        self.tg: dict | None = None
        self.tags: list[dict] = []
        self.targets: set[str] = set()

    def describe_target_groups(self, Names):
        if self.tg is None:
            raise _client_error("TargetGroupNotFound", "DescribeTargetGroups")
        return {"TargetGroups": [dict(self.tg)]}

    def create_target_group(self, Name, Tags, **_):
        self._write("create_target_group")
        self.tg = {
            "TargetGroupArn": f"arn:aws:elasticloadbalancing:{REGION}:123456789012:targetgroup/{Name}/1"
        }
        self.tags = copy.deepcopy(Tags)
        return {"TargetGroups": [dict(self.tg)]}

    def describe_tags(self, ResourceArns):
        return {
            "TagDescriptions": [
                {"ResourceArn": ResourceArns[0], "Tags": copy.deepcopy(self.tags)}
            ]
        }

    def add_tags(self, ResourceArns, Tags):
        self._write("add_tags")
        keys = {t["Key"] for t in Tags}
        self.tags = [t for t in self.tags if t["Key"] not in keys] + copy.deepcopy(Tags)

    def describe_target_health(self, TargetGroupArn):
        return {
            "TargetHealthDescriptions": [
                {"Target": {"Id": target}, "TargetHealth": {"State": "healthy"}}
                for target in sorted(self.targets)
            ]
        }

    def register_targets(self, TargetGroupArn, Targets):
        self._write("register_targets")
        self.targets |= {t["Id"] for t in Targets}

    def describe_load_balancers(self, Names):
        return {"LoadBalancers": [{"LoadBalancerArn": "arn:lb"}]}

    def describe_listeners(self, LoadBalancerArn):
        return {"Listeners": [{"ListenerArn": "arn:listener"}]}


def _done(resource: dict) -> bool:
    return smoke._tag_of(resource, smoke.INIT_TAG_KEY) == smoke.INIT_DONE


def _ensure_tg(elbv2: FakeElbv2) -> str:
    subnets = {"a": SUBNET, "c": "subnet-smoke-c"}
    return smoke._ensure_load_balancer(
        elbv2, VPC, subnets, {smoke.SG_ALB: "sg-alb"}, INSTANCE_IDS
    )


def test_isolation_sg_whose_egress_revoke_failed_is_finished_on_rerun():
    """기본 egress 제거가 실패한 격리 SG를 다음 up이 건너뛰면, 전체 허용 egress가 남은 SG가
    격리용(isolation_group_id)으로 나간다(#348 리뷰 재현)."""
    ec2 = FakeEc2()
    ec2.faults["revoke_security_group_egress"] = [True]
    with pytest.raises(ClientError):
        smoke._ensure_security_groups(ec2, VPC)

    ids = smoke._ensure_security_groups(ec2, VPC)
    isolation = ec2.groups[ids[smoke.SG_ISOLATION]]
    assert isolation["IpPermissions"] == []
    assert isolation["IpPermissionsEgress"] == []
    assert all(_done(g) for g in ec2.groups.values())


def test_isolation_sg_declares_its_role_for_the_rule_engine():
    """격리용 SG는 격리 전까지 어디에도 붙지 않아 evaluate_sg가 곧장 UNUSED(삭제 후보)로 본다 —
    EC2와 달리 관측치 게이트가 없어 첫 회차부터 unused SG와 나란히 올라온다. 이름이나 규칙 수로
    가리는 것은 추정이라, 판정 규칙이 기댈 표지를 태그로 남긴다(#348 리뷰 · 규칙 쪽은 DATA 카드)."""
    ec2 = FakeEc2()
    ids = smoke._ensure_security_groups(ec2, VPC)
    roles = {
        name: smoke._tag_of(ec2.groups[group_id], smoke.ROLE_TAG_KEY)
        for name, group_id in ids.items()
    }
    assert roles.pop(smoke.SG_ISOLATION) == smoke.ROLE_ISOLATION
    assert set(roles.values()) == {None}, f"격리 SG 말고도 role이 붙었다: {roles}"


def test_sg_left_pending_after_its_rule_landed_gets_no_duplicate_rule():
    """규칙은 들어갔는데 표지를 바꾸기 전에 끊긴 경우 — 다시 넣다 Duplicate로 멈추지 않는다."""
    ec2 = FakeEc2()
    ec2.faults["create_tags"] = [
        True
    ]  # 첫 그룹(alb)의 규칙이 들어간 뒤 표지에서 끊긴다
    with pytest.raises(ClientError):
        smoke._ensure_security_groups(ec2, VPC)

    ids = smoke._ensure_security_groups(ec2, VPC)
    assert len(ec2.groups[ids[smoke.SG_ALB]]["IpPermissions"]) == 1
    assert all(_done(g) for g in ec2.groups.values())


def test_nacl_missing_an_allow_rule_is_completed_before_it_is_attached():
    """아웃바운드 허용이 빠진 NACL을 서브넷에 붙이면 서브넷 경계 통신이 끊긴다(#348 리뷰 재현)."""
    ec2 = FakeEc2()
    ec2.faults["create_network_acl_entry"] = [
        False,
        True,
    ]  # 인바운드는 들어가고 아웃바운드에서 끊긴다
    with pytest.raises(ClientError):
        smoke._ensure_nacl(ec2, VPC, SUBNET)
    assert ec2.smoke_acl()["Associations"] == []  # 미완성 NACL은 서브넷에 붙지 않았다

    smoke._ensure_nacl(ec2, VPC, SUBNET)
    acl = ec2.smoke_acl()
    allow = [e for e in acl["Entries"] if e["RuleNumber"] == smoke.NACL_ALLOW_ALL_RULE]
    assert sorted(e["Egress"] for e in allow) == [False, True]
    assert [a["SubnetId"] for a in acl["Associations"]] == [SUBNET]
    assert _done(acl)


def test_target_group_registration_is_finished_on_rerun():
    elbv2 = FakeElbv2()
    elbv2.faults["register_targets"] = [True]
    with pytest.raises(ClientError):
        _ensure_tg(elbv2)

    _ensure_tg(elbv2)
    assert elbv2.targets == {
        INSTANCE_IDS[s.name] for s in smoke.INSTANCES if s.in_target_group
    }
    assert _done({"Tags": elbv2.tags})


def test_rerun_leaves_initialized_resources_as_the_smoke_left_them():
    """초기화가 끝난 자원은 스모크 도중 바뀐 그대로 둔다 — 격리·차단을 조용히 되돌리지 않는다."""
    ec2, elbv2 = FakeEc2(), FakeElbv2()
    ids = smoke._ensure_security_groups(ec2, VPC)
    acl_id = smoke._ensure_nacl(ec2, VPC, SUBNET)
    _ensure_tg(elbv2)
    # 스모크 도중의 변경 — 차단 규칙 추가(NACL_ADD_DENY) · web-1 등록 해제(EC2_ISOLATE) · 미끼 규칙 제거
    ec2.acls[acl_id]["Entries"].append(
        {
            "RuleNumber": 100,
            "Egress": False,
            "RuleAction": "deny",
            "Protocol": "-1",
            "CidrBlock": "198.51.100.0/24",
        }
    )
    elbv2.targets.discard(INSTANCE_IDS[f"{smoke.PREFIX}-web-1"])
    ec2.groups[ids[smoke.SG_OPEN_SSH]]["IpPermissions"] = []
    ec2.writes.clear()
    elbv2.writes.clear()

    smoke._ensure_security_groups(ec2, VPC)
    smoke._ensure_nacl(ec2, VPC, SUBNET)
    _ensure_tg(elbv2)
    assert ec2.writes == []
    assert elbv2.writes == []


# ------------------------------------------------------------------ ⑤ 앱 정책 저장
def _policy_chars(policy: dict) -> int:
    """IAM이 정책 한도에 세는 길이 — 공백은 세지 않는다."""
    return len(re.sub(r"\s", "", json.dumps(policy)))


def test_app_policy_fits_the_managed_policy_limit():
    """P2 실행 경로가 붙으면 ①의 대조 테스트가 정책을 키우게 한다 — 한도는 여기서 막는다."""
    assert _policy_chars(POLICY) <= smoke.MANAGED_POLICY_MAX_CHARS


class FakeIam(_Fake):
    """앱 사용자와 관리형 정책. put_user_policy는 두지 않는다 — 인라인으로 붙이려 하면 AttributeError."""

    def __init__(self):
        super().__init__()
        self.users: dict[str, set[str]] = {}  # 사용자 → 연결된 정책 ARN
        self.inline: dict[str, set[str]] = {}  # 사용자 → 인라인 정책 이름(전환 전 잔재)
        self.versions: dict[str, list[dict]] = {}  # 정책 ARN → 버전(오래된 순)

    def get_user(self, UserName):
        if UserName not in self.users:
            raise _client_error("NoSuchEntity", "GetUser")
        return {"User": {"UserName": UserName}}

    def create_user(self, UserName, Tags):
        self._write("create_user")
        self.users[UserName] = set()
        self.inline[UserName] = set()

    def _add_version(self, arn: str, document: str, default: bool) -> None:
        self._seq += 1
        if default:
            for version in self.versions[arn]:
                version["IsDefaultVersion"] = False
        self.versions[arn].append(
            {
                "VersionId": f"v{self._seq}",
                "IsDefaultVersion": default,
                "CreateDate": self._seq,
                "Document": json.loads(
                    document
                ),  # botocore가 URL 인코딩된 문서를 dict로 풀어 준다
            }
        )

    def create_policy(self, PolicyName, PolicyDocument, Tags):
        self._write("create_policy")
        arn = f"arn:aws:iam::{ACCOUNT}:policy/{PolicyName}"
        self.versions[arn] = []
        self._add_version(arn, PolicyDocument, default=True)
        return {"Policy": {"Arn": arn}}

    def get_policy(self, PolicyArn):
        if PolicyArn not in self.versions:
            raise _client_error("NoSuchEntity", "GetPolicy")
        default = next(v for v in self.versions[PolicyArn] if v["IsDefaultVersion"])
        return {"Policy": {"Arn": PolicyArn, "DefaultVersionId": default["VersionId"]}}

    def get_policy_version(self, PolicyArn, VersionId):
        version = next(
            v for v in self.versions[PolicyArn] if v["VersionId"] == VersionId
        )
        return {"PolicyVersion": copy.deepcopy(version)}

    def list_policy_versions(self, PolicyArn):
        return {
            "Versions": [
                {k: v[k] for k in ("VersionId", "IsDefaultVersion", "CreateDate")}
                for v in self.versions[PolicyArn]
            ]
        }

    def create_policy_version(self, PolicyArn, PolicyDocument, SetAsDefault):
        self._write("create_policy_version")
        if len(self.versions[PolicyArn]) >= smoke._POLICY_VERSION_LIMIT:
            raise _client_error("LimitExceeded", "CreatePolicyVersion")
        self._add_version(PolicyArn, PolicyDocument, default=SetAsDefault)

    def delete_policy_version(self, PolicyArn, VersionId):
        self._write("delete_policy_version")
        version = next(
            v for v in self.versions[PolicyArn] if v["VersionId"] == VersionId
        )
        if version["IsDefaultVersion"]:
            raise _client_error("DeleteConflict", "DeletePolicyVersion")
        self.versions[PolicyArn].remove(version)

    def delete_policy(self, PolicyArn):
        self._write("delete_policy")
        if len(self.versions[PolicyArn]) > 1 or any(
            PolicyArn in arns for arns in self.users.values()
        ):
            raise _client_error("DeleteConflict", "DeletePolicy")
        del self.versions[PolicyArn]

    def list_attached_user_policies(self, UserName):
        return {
            "AttachedPolicies": [
                {"PolicyArn": arn} for arn in sorted(self.users[UserName])
            ]
        }

    def attach_user_policy(self, UserName, PolicyArn):
        self._write("attach_user_policy")
        self.users[UserName].add(PolicyArn)

    def detach_user_policy(self, UserName, PolicyArn):
        self._write("detach_user_policy")
        self.users[UserName].discard(PolicyArn)

    def list_access_keys(self, UserName):
        return {"AccessKeyMetadata": []}

    def list_user_policies(self, UserName):
        return {"PolicyNames": sorted(self.inline.get(UserName, ()))}

    def delete_user_policy(self, UserName, PolicyName):
        self._write("delete_user_policy")
        self.inline[UserName].remove(PolicyName)

    def delete_user(self, UserName):
        self._write("delete_user")
        if self.users[UserName] or self.inline[UserName]:
            raise _client_error("DeleteConflict", "DeleteUser")
        del self.users[UserName]
        del self.inline[UserName]

    def default_document(self, arn: str) -> dict:
        return next(v["Document"] for v in self.versions[arn] if v["IsDefaultVersion"])


def test_app_policy_is_attached_as_a_managed_policy():
    """사용자 인라인 정책(합계 2,048자)으로는 저장되지 않는다(#348 리뷰 — 2,084자)."""
    iam = FakeIam()
    smoke._ensure_app_user(iam, ACCOUNT, REGION, VPC_ID, SUBNET_IDS)
    arn = smoke._app_policy_arn(ACCOUNT)
    assert iam.users[smoke.APP_USER] == {arn}
    assert iam.default_document(arn) == POLICY

    iam.writes.clear()
    smoke._ensure_app_user(
        iam, ACCOUNT, REGION, VPC_ID, SUBNET_IDS
    )  # 같은 VPC로 다시 — 버전을 쌓지 않는다
    assert iam.writes == []


def test_operator_role_allowlist_reaches_the_attached_policy():
    iam = FakeIam()
    smoke._ensure_app_user(iam, ACCOUNT, REGION, VPC_ID, SUBNET_IDS, [ROLE_ARN])
    assert iam.default_document(smoke._app_policy_arn(ACCOUNT)) == POLICY_WITH_ROLE


def test_recreated_vpc_moves_the_policy_to_a_new_version_within_the_limit():
    """VPC를 다시 세우면 정책의 VPC ARN이 따라간다 — 버전 한도(5)를 넘겨도 up이 멈추지 않는다."""
    iam = FakeIam()
    vpcs = [f"vpc-{n:017x}" for n in range(smoke._POLICY_VERSION_LIMIT + 3)]
    for vpc_id in vpcs:
        smoke._ensure_app_user(iam, ACCOUNT, REGION, vpc_id, SUBNET_IDS)
    arn = smoke._app_policy_arn(ACCOUNT)
    assert len(iam.versions[arn]) == smoke._POLICY_VERSION_LIMIT
    assert iam.default_document(arn) == smoke.build_app_policy(
        ACCOUNT, REGION, vpcs[-1], SUBNET_IDS
    )


def test_down_removes_the_app_user_and_its_managed_policy():
    """사용자를 지워도 관리형 정책은 남는다 — status 잔여 0건이 되려면 따로 걷혀야 한다."""
    iam = FakeIam()
    smoke._ensure_app_user(iam, ACCOUNT, REGION, VPC_ID, SUBNET_IDS)
    smoke._ensure_app_user(
        iam, ACCOUNT, REGION, "vpc-0fedcba9876543210", SUBNET_IDS
    )  # 비기본 버전을 하나 남긴다
    smoke._delete_app_user(iam)
    smoke._delete_app_policy(iam, ACCOUNT)
    assert iam.users == {}
    assert iam.versions == {}


class FakeIamWithRoles(FakeIam):
    """플랫폼 접근 역할(ADR-0010 §3)이 앱 정책을 빌려 붙인 상태."""

    def __init__(self, roles):
        super().__init__()
        self.policy_roles = list(roles)

    def get_paginator(self, name):
        assert name == "list_entities_for_policy"
        iam = self

        class _Pages:
            def paginate(self, PolicyArn, EntityFilter):
                assert EntityFilter == "Role"
                yield {"PolicyRoles": [{"RoleName": r} for r in iam.policy_roles]}

        return _Pages()


def test_down_stops_first_when_a_role_still_holds_the_app_policy():
    """정책 삭제는 down의 맨 끝이다 — 역할이 붙잡고 있으면 VPC까지 지운 뒤 DeleteConflict로 멈춘다.
    무엇이든 지우기 전에 "플랫폼 down 먼저"로 멈춰야 한다."""
    iam = FakeIamWithRoles(["vigilantis-access"])
    smoke._ensure_app_user(iam, ACCOUNT, REGION, VPC_ID, SUBNET_IDS)
    iam.writes.clear()
    # ec2·elbv2·asg 자리는 비워 둔다 — 건드리면 KeyError로 드러난다
    with pytest.raises(SystemExit, match="플랫폼 down → 스모크 down"):
        smoke.down({"iam": iam}, ACCOUNT)
    assert iam.writes == []


def test_down_guard_passes_when_no_role_holds_the_app_policy():
    iam = FakeIamWithRoles([])
    smoke._ensure_app_user(iam, ACCOUNT, REGION, VPC_ID, SUBNET_IDS)
    assert smoke._roles_holding_app_policy(iam, ACCOUNT) == []
    assert (
        smoke._roles_holding_app_policy(FakeIamWithRoles(["x"]), ACCOUNT) == []
    )  # 정책이 없으면 볼 것도 없다


def test_up_clears_an_inline_policy_left_from_before_the_managed_switch():
    """앱 권한은 관리형 정책 하나여야 한다 — 전환 전에 붙은 인라인이 남으면 권한이 합집합이 된다."""
    iam = FakeIam()
    smoke._ensure_app_user(iam, ACCOUNT, REGION, VPC_ID, SUBNET_IDS)
    iam.inline[smoke.APP_USER].add(smoke.APP_USER)  # 전환 전 up이 남긴 인라인 정책

    smoke._ensure_app_user(iam, ACCOUNT, REGION, VPC_ID, SUBNET_IDS)
    assert iam.inline[smoke.APP_USER] == set()
    assert iam.users[smoke.APP_USER] == {smoke._app_policy_arn(ACCOUNT)}


# ⑥ 비용 알림 (ADR-0009 §3 2026-10-12 개정) ----------------------------------------------------


class FakeBudgets:
    def __init__(self, existing: dict | None = None):
        self.budget = copy.deepcopy(existing)
        self.created = 0
        self.updated = 0

    def describe_budget(self, AccountId, BudgetName):
        if self.budget is None:
            raise _client_error("NotFoundException", "DescribeBudget")
        return {"Budget": copy.deepcopy(self.budget)}

    def create_budget(self, AccountId, Budget, NotificationsWithSubscribers):
        self.budget = copy.deepcopy(Budget)
        self.created += 1

    def update_budget(self, AccountId, NewBudget):
        self.budget = copy.deepcopy(NewBudget)
        self.updated += 1


def test_budget_measures_usage_before_credits():
    """크레딧이 비용을 상쇄하면 사용액이 0에 가깝게 보여 알림이 울리지 않는다 — 크레딧 제외로 만든다."""
    budgets = FakeBudgets()
    smoke._ensure_budget(budgets, ACCOUNT, "pm@example.com")
    assert budgets.created == 1
    assert budgets.budget["CostTypes"]["IncludeCredit"] is False
    assert float(budgets.budget["BudgetLimit"]["Amount"]) == float(smoke.BUDGET_USD)


def test_existing_budget_from_before_the_amendment_is_brought_up_to_date():
    """정의가 바뀌기 전(월 $50·크레딧 포함)에 선 Budget은 그대로 두면 바뀐 정의가 계정에 닿지 않는다 — 다시 돌린 up이 맞춘다."""
    budgets = FakeBudgets(
        {
            "BudgetName": smoke.BUDGET_NAME,
            "BudgetLimit": {"Amount": "50.0", "Unit": "USD"},
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
            "CostTypes": {"IncludeCredit": True},
        }
    )
    smoke._ensure_budget(budgets, ACCOUNT, "pm@example.com")
    assert budgets.updated == 1
    assert budgets.budget["CostTypes"]["IncludeCredit"] is False
    assert float(budgets.budget["BudgetLimit"]["Amount"]) == float(smoke.BUDGET_USD)

    smoke._ensure_budget(budgets, ACCOUNT, "pm@example.com")
    assert budgets.updated == 1, "정의가 같으면 다시 갱신하지 않는다"


def test_existing_budget_with_other_time_unit_is_brought_up_to_date():
    """update_budget은 정의 전체를 덮어쓰므로 금액·크레딧이 같아도 기간 단위가 다르면 갱신 대상이다."""
    budgets = FakeBudgets({**smoke._budget_definition(), "TimeUnit": "QUARTERLY"})
    smoke._ensure_budget(budgets, ACCOUNT, "pm@example.com")
    assert budgets.updated == 1
    assert budgets.created == 0
    assert budgets.budget["TimeUnit"] == "MONTHLY"


def test_budget_lookup_error_other_than_not_found_is_not_swallowed():
    """권한 거절 같은 조회 실패를 '없음'으로 읽으면 이미 선 Budget 위에 생성을 시도한다 — 그대로 올린다."""

    class DeniedBudgets(FakeBudgets):
        def describe_budget(self, AccountId, BudgetName):
            raise _client_error("AccessDeniedException", "DescribeBudget")

    budgets = DeniedBudgets()
    with pytest.raises(ClientError):
        smoke._ensure_budget(budgets, ACCOUNT, "pm@example.com")
    assert budgets.created == 0
