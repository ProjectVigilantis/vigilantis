# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 고객 계정 온보딩 CloudFormation 템플릿을 만듭니다. (ADR-0010 5차 개정 · ADR-0012)
#
# 고객은 고객 연결 화면의 빠른 생성 링크(apps/core-api/tenant_admin.onboarding_url)로 이 템플릿을
# 자기 계정에 올려 접근 역할을 만든다. 플랫폼은 그 역할로 AssumeRole + ExternalId만 한다.
#
#   만들기 : uv run python scripts/onboarding_template.py          (packages/iac/onboarding/에 쓴다)
#   대조   : uv run python scripts/onboarding_template.py --check  (저장소 파일이 생성 결과와 같은지만 본다)
#
# 권한은 코드가 부르는 AWS 작업의 거울이다. 스모크 앱 정책(scripts/provision_smoke_aws.py)과 작업 목록은
# 같고 울타리만 다르다. 스모크의 울타리(스모크 태그 · 스모크 VPC · 스모크 서브넷)는 걷고, 실행이 새로
# 만드는 자원(AMI · 시작 템플릿 · ASG · 그 ASG가 띄우는 인스턴스)에는 실행 태그가 있어야만 한다는 울타리를
# 남긴다. tests/test_onboarding_template.py가 스모크 테스트의 작업 거울을 그대로 이 정책에 건다.
#
# 넣지 않은 것: iam:PassRole. 인스턴스 프로파일이 달린 인스턴스의 ASG 전환은 이 역할로는 할 수 없어,
# 코어가 테넌트 문맥에서 precheck·계획 단계에 미리 거절한다(executor._tenant_profile_needs_pass_role).
#
# ExternalId에 NoEcho를 걸지 않는다. 빠른 생성 링크는 NoEcho 파라미터의 param_ 값을 버리고, AWS는
# ExternalId를 비밀로 보지 않는다(IAM 사용 설명서 third-party 절). 막는 것은 신뢰 정책의 일치 조건이다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "packages") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "packages"))

from schemas.autoscaling import (  # noqa: E402
    EXECUTION_TAG_KEY,
    PRESERVED_SOURCE_TAG_KEYS,
    SOURCE_INSTANCE_TAG_KEY,
)

TEMPLATE_PATH = _REPO_ROOT / "packages" / "iac" / "onboarding" / "vigilantis-access.json"
TEMPLATE_KEY = "vigilantis-access.json"  # 플랫폼 up이 S3에 올릴 때의 객체 이름
# 빠른 생성 링크의 param_* 이름(tenant_admin.onboarding_url)과 같아야 한다
PARAMETERS = ("ExternalId", "PlatformPrincipalArn", "RoleName")
POLICY_NAME = "vigilantis-access"
# IAM 인라인 정책 합계 한도(공백 제외). 테스트가 잰다
INLINE_POLICY_MAX_CHARS = 10240

READ_ACTIONS = (
    "autoscaling:Describe*",
    "cloudwatch:GetMetricData",
    "ec2:Describe*",
    "elasticloadbalancing:Describe*",
)
COST_ACTIONS = ("ce:GetCostAndUsage",)
COST_REGION = "us-east-1"  # Cost Explorer 엔드포인트는 이 리전 하나뿐이다
ASG_PREFIX = "vigilantis-asg-"  # services/aws/executor._asg_name
ASG_MAX_SIZE = 4  # schemas.runbook_parameters AutoScalingSize 상한

_ACCOUNT = "${AWS::AccountId}"
_RUN_TAGS_PRESENT = {
    f"aws:RequestTag/{EXECUTION_TAG_KEY}": "false",
    f"aws:RequestTag/{SOURCE_INSTANCE_TAG_KEY}": "false",
}
_OURS = {f"aws:ResourceTag/{EXECUTION_TAG_KEY}": "false"}  # 실행이 만든 자원만


def _sub(value: str):
    return {"Fn::Sub": value} if "${" in value else value


def _ec2(kind: str, owned: bool = True) -> str:
    """리전은 열어 둔다 — 고객이 관제받는 리전은 테넌트 행이 정하고 템플릿은 모른다."""
    return f"arn:aws:ec2:*:{_ACCOUNT if owned else ''}:{kind}/*"


def _asg() -> str:
    return f"arn:aws:autoscaling:*:{_ACCOUNT}:autoScalingGroup:*:autoScalingGroupName/{ASG_PREFIX}*"


def _statement(actions, resources, condition=None) -> dict:
    resources = [resources] if isinstance(resources, str) else list(resources)
    resource = [_sub(r) for r in resources]
    out = {"Effect": "Allow", "Action": sorted(actions),
           "Resource": resource[0] if len(resource) == 1 else resource}
    if condition:
        out["Condition"] = condition
    return out


def build_customer_policy() -> dict:
    """고객 계정 접근 역할의 인라인 정책."""
    images = [_ec2("image", owned=False), _ec2("snapshot", owned=False)]
    return {
        "Version": "2012-10-17",
        "Statement": [
            _statement(READ_ACTIONS, "*"),
            _statement(COST_ACTIONS, "*", {"StringEquals": {"aws:RequestedRegion": COST_REGION}}),
            # 격리 · 다운사이징 · 원복
            _statement(["ec2:CreateImage", "ec2:ModifyInstanceAttribute", "ec2:StartInstances", "ec2:StopInstances"],
                       _ec2("instance")),
            # NACL 차단 · 해제
            _statement(["ec2:CreateNetworkAclEntry", "ec2:DeleteNetworkAclEntry"], _ec2("network-acl")),
            # 미부착 볼륨 삭제(최종 스냅숏을 먼저 남긴다)
            _statement(["ec2:CreateSnapshot", "ec2:DeleteVolume"], _ec2("volume")),
            _statement(["ec2:CreateSnapshot"], _ec2("snapshot", owned=False)),
            # 격리 SG · SG 삭제 · 재생성
            _statement(["ec2:AuthorizeSecurityGroupEgress", "ec2:AuthorizeSecurityGroupIngress",
                        "ec2:DeleteSecurityGroup", "ec2:ModifyNetworkInterfaceAttribute",
                        "ec2:RevokeSecurityGroupEgress"],
                       [_ec2("security-group"), _ec2("network-interface")]),
            _statement(["ec2:CreateSecurityGroup"], [_ec2("security-group"), _ec2("vpc")]),
            # 격리 대상의 대상 그룹 해제 · 복귀
            _statement(["elasticloadbalancing:DeregisterTargets", "elasticloadbalancing:RegisterTargets"],
                       f"arn:aws:elasticloadbalancing:*:{_ACCOUNT}:targetgroup/*"),
            # ASG 전환 — 새 자원은 실행 태그를 달아야만 만들어진다
            _statement(["ec2:CreateImage", "ec2:CreateLaunchTemplate"], [*images, _ec2("launch-template")],
                       {"Null": dict(_RUN_TAGS_PRESENT)}),
            _statement(["ec2:CreateTags"],
                       [_ec2("instance"), _ec2("volume"), _ec2("launch-template"), *images],
                       {"StringEquals": {"ec2:CreateAction": ["CreateImage", "CreateLaunchTemplate", "RunInstances"]},
                        "ForAllValues:StringEquals": {"aws:TagKeys": [
                            EXECUTION_TAG_KEY, SOURCE_INSTANCE_TAG_KEY, *PRESERVED_SOURCE_TAG_KEYS]},
                        "Null": {"aws:TagKeys": "false"}}),
            _statement(["autoscaling:CreateAutoScalingGroup"], _asg(),
                       {"Null": {**_RUN_TAGS_PRESENT, "autoscaling:VPCZoneIdentifiers": "false"},
                        "NumericLessThanEquals": {"autoscaling:MaxSize": str(ASG_MAX_SIZE),
                                                  "autoscaling:MinSize": str(ASG_MAX_SIZE)},
                        "Bool": {"autoscaling:LaunchTemplateVersionSpecified": "true"}}),
            _statement(["autoscaling:CreateOrUpdateTags"], _asg(), {"Null": dict(_RUN_TAGS_PRESENT)}),
            _statement(["autoscaling:PutScalingPolicy", "autoscaling:SetInstanceProtection"], _asg(),
                       {"Null": dict(_OURS)}),
            # CreateAutoScalingGroup이 호출자에게 요구하는 RunInstances(앱은 직접 부르지 않는다)
            _statement(["ec2:RunInstances"], [_ec2("instance"), _ec2("volume")], {"Null": dict(_RUN_TAGS_PRESENT)}),
            _statement(["ec2:RunInstances"], _ec2("launch-template"), {"Null": dict(_OURS)}),
            _statement(["ec2:RunInstances"], images,
                       {"Null": dict(_OURS), "StringEquals": {"ec2:Owner": _sub(_ACCOUNT)}}),
            _statement(["ec2:RunInstances"], [_ec2("subnet"), _ec2("network-interface"), _ec2("security-group")]),
        ],
    }


def build_template() -> dict:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": {"Ref": "PlatformPrincipalArn"}},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}},
        }],
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Vigilantis customer access role. The platform assumes this role with your ExternalId "
                       "to monitor and act on this account. Delete the stack to revoke access.",
        "Parameters": {
            "ExternalId": {"Type": "String", "MinLength": 16,
                           "Description": "Issued by Vigilantis for this customer. Required on every AssumeRole."},
            "PlatformPrincipalArn": {"Type": "String", "AllowedPattern": r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]+$",
                                     "Description": "The Vigilantis platform role allowed to assume this role."},
            "RoleName": {"Type": "String", "Default": "vigilantis-access", "AllowedPattern": r"^vigilantis-access[\w+=,.@-]*$",
                         "Description": "Name of the access role. Must start with vigilantis-access."},
        },
        "Resources": {
            "AccessRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": {"Ref": "RoleName"},
                    "Description": "Vigilantis monitored-account access",
                    "MaxSessionDuration": 3600,
                    "AssumeRolePolicyDocument": trust,
                    "Policies": [{"PolicyName": POLICY_NAME, "PolicyDocument": build_customer_policy()}],
                    "Tags": [{"Key": "vigilantis:access-role", "Value": "true"}],
                },
            },
        },
        "Outputs": {"RoleArn": {"Value": {"Fn::GetAtt": ["AccessRole", "Arn"]}}},
    }


def render() -> str:
    return json.dumps(build_template(), indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="고객 온보딩 CloudFormation 템플릿")
    parser.add_argument("--check", action="store_true", help="저장소 파일이 생성 결과와 같은지만 본다")
    args = parser.parse_args()
    text = render()
    if args.check:
        current = TEMPLATE_PATH.read_text(encoding="utf-8") if TEMPLATE_PATH.exists() else ""
        if current != text:
            sys.exit(f"{TEMPLATE_PATH.relative_to(_REPO_ROOT)}가 생성 결과와 다르다. 이 스크립트를 다시 돌릴 것")
        return
    TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATE_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"{TEMPLATE_PATH.relative_to(_REPO_ROOT)} 작성")


if __name__ == "__main__":
    main()
