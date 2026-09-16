# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 실 AWS 스모크 환경 구성·정리 스크립트입니다. (ADR-0009)
#
# 9주차(10/02–10/08) 실 AWS 스모크와 10주차(10/12–10/15) P2 3종 첫 검증이 겨눌 자산을
# 한 번에 세우고(up), 끝나면 한 번에 걷는다(down). 그 사이에는 status로 무엇이 떠 있는지 본다.
#
# Terraform이 아니라 Boto3인 이유: IaC는 Post-MVP다(ADR-0006 §2 — MVP는 Boto3 직접 실행).
# 방식은 LocalStack 시드(scripts/seed_localstack.py)와 같다 — 태그 식별·멱등. 방향만 반대다.
#
# 실행 (repo 루트, **관리자 프로필**로 — .env의 앱 키가 아니다. ADR-0009 §2):
#   읽기만    : uv run python scripts/provision_smoke_aws.py status --account <계정 ID>
#   세울 목록 : uv run python scripts/provision_smoke_aws.py up --account <계정 ID> --budget-email example@email.com
#   실제 생성 : ... up --account <계정 ID> --budget-email example@email.com --yes
#   전부 정리 : ... down --account <계정 ID> --yes
#   앱 정책   : ... policy --account <계정 ID>   (앱 사용자에 붙일 IAM 정책 JSON — 읽기만)
#
# 안전 가드 — 시드 가드의 거울상이다.
#   ① AWS_ENDPOINT_URL이 잡혀 있으면 AWS를 부르기 전에 멈춘다. LocalStack에는 elbv2가
#      없고(ADR-0007), 여기서 만드는 것은 전부 실 AWS 과금 자원이다.
#   ② --account가 호출 주체의 계정(sts)과 다르면 멈춘다 — 프로필을 잘못 잡은 채 다른
#      계정에 VPC를 세우는 사고를 막는다. 계정 ID는 저장소에 적지 않는다.
#   ③ up·down은 --yes 없이는 아무것도 바꾸지 않는다(목록만 보여 준다).
#   ④ down이 지우는 것은 스모크 태그 자원과 **스모크 VPC 안의 것**뿐이다. VPC 안을 통째로
#      쓰는 이유는 앱이 런북으로 만든 자원(재생성 SG·ASG가 띄운 인스턴스)에 우리 태그가
#      없어서다.
#
# up은 **없는 것을 만들 뿐 바뀐 것을 되돌리지 않는다.** 스모크 도중 다시 돌려도 격리
# (EC2_ISOLATE가 뺀 Target Group 등록)나 차단 규칙을 조용히 되돌리지 않게 하기 위해서다.
# 런북이 지운 자원(미사용 SG·미연결 EBS)은 없는 것이므로 다시 만든다 — 재실행 준비가 그것이다.
# 예외는 **만든 직후의 설정이 끝나지 않은 자원**이다. SG 규칙·NACL 허용 규칙·TG 대상 등록은
# 생성과 별개 호출이라 그 사이에 끊길 수 있다. 그래서 생성 때 초기화 표지(INIT_TAG_KEY=pending)를
# 함께 달고 설정이 끝나야 done으로 바꾼다. 다음 up은 pending인 자원만 설정을 이어서 끝낸다 —
# done이거나 표지가 없는 자원(앱이 런북으로 만든 것)은 여전히 손대지 않는다.
#
# 격리 설계(ADR-0009 §1): 인터넷 게이트웨이를 두지 않는다. ALB는 internal, 인스턴스는 공인
# IP가 없다 — 22/tcp 0.0.0.0/0 위협 미끼 SG가 실제로 열리는 일이 없고 공인 IPv4 과금도 없다.
# 웹 서버는 AL2023 기본 python3라 패키지 설치(인터넷)가 필요 없다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT / "apps" / "core-api"), str(_REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from botocore.exceptions import ClientError  # noqa: E402

from schemas.rightsizing_policy import rightsizing_target_type  # noqa: E402

# 리전·엔드포인트·자격증명 해석과 클라이언트 생성의 단일 원천(ADR-0006 §3, Issue #128).
from services.aws.client import aws_client, default_region, endpoint_url  # noqa: E402
from services.rule_engine import MIN_DATAPOINTS  # noqa: E402

SMOKE_TAG_KEY = "vigilantis:smoke"
SMOKE_TAG_VALUE = "true"
PREFIX = "vigilantis-smoke"
# 초기화 표지 — 맨 위 "up은 되돌리지 않는다"의 예외를 가르는 기준. pending은 이 스크립트의
# 생성 호출만 단다(생성과 한 호출이라 표지 없는 미완성 자원은 생기지 않는다).
INIT_TAG_KEY = "vigilantis:smoke-init"
INIT_PENDING = "pending"
INIT_DONE = "done"
# 자원이 맡은 자리를 판정 규칙에 알리는 표지. 격리용 SG는 격리 전까지 어디에도 붙지 않아
# evaluate_sg가 UNUSED(삭제 후보)로 보는데, 이름·규칙 수로 가리는 것은 추정이라 태그로 가둔다.
# 값의 뜻(어느 role을 판정에서 빼는가)은 규칙 쪽이 정한다 — 여기는 자리만 선언한다(#348 리뷰).
ROLE_TAG_KEY = "vigilantis:role"
ROLE_ISOLATION = "isolation"

VPC_NAME = f"{PREFIX}-vpc"
VPC_CIDR = "10.42.0.0/16"  # 기본 VPC(172.31.0.0/16)와 겹치지 않게
# AZ 접미 → CIDR. ALB는 서로 다른 AZ의 서브넷 2개를 요구한다.
SUBNETS = {"a": "10.42.1.0/24", "c": "10.42.2.0/24"}
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"

WEB_TYPE = "t3.micro"
# RIGHTSIZING 후보 자리. t3.small 이하는 규칙이 목표 타입을 내지 않는다(rightsizing_policy
# ③ 메모리 2 GiB 하한) — ADR-0006 §4의 "t3.micro급"을 이 1대만 넘는 이유다. 규칙이 바뀌어
# 이 타입이 후보가 못 되면 스모크의 FinOps 경로가 통째로 사라지므로 up이 멈춘다(main).
# import 시점에 검사하지 않는다 — 테스트가 수집 중에 이 모듈을 읽으므로 거기서 sys.exit하면
# pytest 세션 전체가 INTERNALERROR로 끝나 CI 테스트가 0건 돈다.
IDLE_DEV_TYPE = "t3.medium"

SG_ALB = f"{PREFIX}-alb"              # internal ALB 수신 80/tcp — VPC 안에서만
SG_WEB = f"{PREFIX}-web"              # ALB에서 오는 80/tcp만
SG_OPEN_SSH = f"{PREFIX}-open-ssh"    # OpenIP 위협 미끼 22/tcp 0.0.0.0/0 — 공인 IP 없는 인스턴스에만
SG_UNUSED = f"{PREFIX}-unused"        # 어디에도 붙지 않는다 — SG_DELETE_ISOLATED 대상
SG_ISOLATION = f"{PREFIX}-isolation"  # 규칙 0개 — EC2_ISOLATE가 ENI를 이 SG로 바꾼다
SECURITY_GROUPS = (SG_ALB, SG_WEB, SG_OPEN_SSH, SG_UNUSED, SG_ISOLATION)


@dataclass(frozen=True)
class InstanceSpec:
    name: str
    instance_type: str
    az: str                    # SUBNETS 키
    groups: tuple[str, ...]
    environment: str           # _is_prod가 보는 태그(services/rule_engine.py)
    in_target_group: bool


INSTANCES = (
    # EC2_ISOLATE 대상 · NACL_ADD_DENY가 겨누는 서브넷 · OpenIP 위협 SG.
    # production이라 비용 판정은 관측치가 찬 뒤 SKIP_PROD_PROTECTED — evaluate_ec2는 관측치
    # 검사가 prod 검사보다 앞이라 기동 후 MIN_DATAPOINTS시간은 SKIP_INSUFFICIENT_DATA다.
    InstanceSpec(f"{PREFIX}-web-1", WEB_TYPE, "a", (SG_WEB, SG_OPEN_SSH), "production", True),
    # 격리 뒤에도 Target Group을 잇는 두 번째 대상 — "다중 EC2"의 자리
    InstanceSpec(f"{PREFIX}-web-2", WEB_TYPE, "c", (SG_WEB,), "production", True),
    # RIGHTSIZING → REVERT_SIZE · Status Check 주입 대상. MIN_DATAPOINTS시간 관측 뒤 COST_CANDIDATE
    InstanceSpec(f"{PREFIX}-idle-dev", IDLE_DEV_TYPE, "a", (SG_WEB,), "development", False),
)

NACL_NAME = f"{PREFIX}-nacl"
NACL_AZ = "a"  # web-1의 서브넷 — 위협 대상과 조치가 닿는 자원을 같은 자리로(시드와 같은 이유)
# 실 AWS의 커스텀 NACL은 deny-all(32767)만 갖고 태어난다 — 허용 없이 붙이면 서브넷 통신이
# 끊긴다(LocalStack은 트래픽을 흉내 내지 않아 드러나지 않는다). AI는 rule_number를 1–32766
# 에서 고르므로(ai/agent.py) 허용을 그 맨 끝에 둔다 — 어떤 차단 규칙도 허용보다 먼저
# 평가되고, 같은 번호를 고르면 precheck ②가 점유로 거절한다.
NACL_ALLOW_ALL_RULE = 32766
VOLUME_NAME = f"{PREFIX}-unattached"
ALB_NAME = f"{PREFIX}-alb"
TG_NAME = f"{PREFIX}-tg"

APP_USER = f"{PREFIX}-app"
# 앱 정책은 사용자 인라인이 아니라 고객 관리형으로 붙인다. 사용자 인라인 정책은 합계 2,048자(공백
# 제외)가 한도라 코드가 부르는 조치 권한만 담은 지금 정책(2,084자)도 들어가지 않는다(#348 리뷰).
# 관리형 한도는 6,144자이고 P2 실행 경로가 붙으며 늘어날 문장도 담는다 — 한도는 테스트가 지킨다.
APP_POLICY = APP_USER
MANAGED_POLICY_MAX_CHARS = 6144
_POLICY_VERSION_LIMIT = 5  # 관리형 정책 하나가 가질 수 있는 버전 수
BUDGET_NAME = f"{PREFIX}-monthly"
BUDGET_USD = "50"  # 알림선이지 상한이 아니다(ADR-0009 §3)
BUDGET_ALERTS = (("ACTUAL", 50), ("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100))
# 앱이 ENABLE_AUTOSCALING으로 만드는 Launch Template 이름(executor._launch_template_name).
# 태그가 없고 VPC 자원도 아니라 이름으로만 가린다.
APP_LT_PREFIX = "vigilantis-lt-"
ASG_SERVICE = "autoscaling.amazonaws.com"
ASG_SERVICE_ROLE = "AWSServiceRoleForAutoScaling"

# 앱 정책의 조회 권한. 수집(collector)·precheck·원복 판정이 부르는 describe 전부와 메트릭 조회.
READ_ACTIONS = (
    "autoscaling:Describe*",
    "cloudwatch:GetMetricData",
    "ec2:Describe*",
    "elasticloadbalancing:Describe*",
)

_LIVE_STATES = ["pending", "running", "stopping", "stopped"]
_SMOKE_FILTER = {"Name": f"tag:{SMOKE_TAG_KEY}", "Values": [SMOKE_TAG_VALUE]}

_USER_DATA = """#!/bin/bash
# ALB 헬스체크와 격리 확인용 최소 웹 서버. AL2023 기본 python3 — 인터넷 없이 뜬다.
# systemd로 두는 이유: RIGHTSIZING은 stop/start라 user data가 다시 돌지 않는다.
mkdir -p /srv/www
echo "{name}" > /srv/www/index.html
cat > /etc/systemd/system/smoke-http.service <<'EOF'
[Unit]
Description=vigilantis smoke http
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 -m http.server 80 --directory /srv/www
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now smoke-http
"""


# ------------------------------------------------------------------ 앱 IAM 정책
def build_app_policy(account: str, region: str, vpc_id: str) -> dict:
    """앱 키에 붙일 최소 권한 정책. **코드가 부르는 AWS 작업의 거울이다**(ADR-0009 §2).

    tests/test_provision_smoke_aws.py가 두 방향을 지킨다 — 실행 경로를 더한 PR이 여기를
    빠뜨리면 깨지고, 코드가 부르지 않는 조치 권한이 끼어도 깨진다. DryRun도 같은 권한을
    요구하므로(권한이 없으면 UnauthorizedOperation) precheck만 하는 런북의 작업도 들어간다.

    울타리는 셋이다. ① 리전(ARN에 박히거나 aws:RequestedRegion) ② 인스턴스·NACL·볼륨은
    스모크 태그 자원만 ③ SG·ENI는 스모크 VPC 안만 — 앱이 재생성한 SG에는 우리 태그가 없어
    태그 대신 VPC로 가둔다. 새 SG 생성은 새 SG가 아니라 생성 장소(VPC ARN)로 가둔다.
    조건 키가 실제로 채워지는지는 적용 직후 DryRun으로 잰다.

    크기는 관리형 정책 한도(MANAGED_POLICY_MAX_CHARS, 공백 제외) 안이어야 한다 — 테스트가 잰다.
    """
    arn = f"arn:aws:ec2:{region}:{account}"
    vpc_arn = f"{arn}:vpc/{vpc_id}"
    smoke_tagged = {"StringEquals": {f"aws:ResourceTag/{SMOKE_TAG_KEY}": SMOKE_TAG_VALUE}}
    in_region = {"StringEquals": {"aws:RequestedRegion": region}}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadInventory",
                "Effect": "Allow",
                "Action": list(READ_ACTIONS),
                "Resource": "*",
                "Condition": in_region,
            },
            {
                "Sid": "SmokeInstances",  # RIGHTSIZING·REVERT_SIZE(stop → modify → start)
                "Effect": "Allow",
                "Action": ["ec2:ModifyInstanceAttribute", "ec2:StartInstances", "ec2:StopInstances"],
                "Resource": f"{arn}:instance/*",
                "Condition": smoke_tagged,
            },
            {
                "Sid": "SmokeNaclEntries",  # NACL_ADD_DENY·NACL_RESTORE
                "Effect": "Allow",
                "Action": ["ec2:CreateNetworkAclEntry", "ec2:DeleteNetworkAclEntry"],
                "Resource": f"{arn}:network-acl/*",
                "Condition": smoke_tagged,
            },
            {
                "Sid": "SmokeVolumes",  # EBS_DELETE_UNATTACHED(스냅숏 → 삭제)
                "Effect": "Allow",
                "Action": ["ec2:CreateSnapshot", "ec2:DeleteVolume"],
                "Resource": f"{arn}:volume/*",
                "Condition": smoke_tagged,
            },
            {
                "Sid": "SnapshotsOfSmokeVolumes",  # 새 스냅숏 자원 — 볼륨 쪽 조건이 이미 가둔다
                "Effect": "Allow",
                "Action": ["ec2:CreateSnapshot"],
                "Resource": f"arn:aws:ec2:{region}::snapshot/*",
            },
            {
                # SG_DELETE_ISOLATED·SG_RECREATE(규칙 복원)·EC2_ISOLATE·EC2_UNISOLATE(ENI의 SG 교체)
                "Sid": "SecurityGroupsAndEnisInSmokeVpc",
                "Effect": "Allow",
                "Action": [
                    "ec2:AuthorizeSecurityGroupEgress",
                    "ec2:AuthorizeSecurityGroupIngress",
                    "ec2:DeleteSecurityGroup",
                    "ec2:ModifyNetworkInterfaceAttribute",
                ],
                "Resource": [f"{arn}:security-group/*", f"{arn}:network-interface/*"],
                "Condition": {"ArnEquals": {"ec2:Vpc": vpc_arn}},
            },
            {
                # SG_RECREATE의 생성. CreateSecurityGroup은 새 SG와 생성 장소 VPC를 **각각** 검사하는데,
                # 새 SG 쪽 조건 키는 요청 태그·ec2:SecurityGroupID뿐이다(ec2:Vpc 없음 — 붙이면 이
                # 문장은 영영 성립하지 않는다). 그래서 새 SG 쪽은 열어 두고 울타리는 아래 VPC 문장이 친다.
                "Sid": "NewSecurityGroups",
                "Effect": "Allow",
                "Action": ["ec2:CreateSecurityGroup"],
                "Resource": f"{arn}:security-group/*",
            },
            {
                "Sid": "CreateSecurityGroupInSmokeVpc",  # 생성 장소 — 스모크 VPC 밖에는 만들지 못한다
                "Effect": "Allow",
                "Action": ["ec2:CreateSecurityGroup"],
                "Resource": vpc_arn,
            },
            {
                "Sid": "LaunchTemplatesForAutoscaling",  # ENABLE_AUTOSCALING precheck(DryRun)
                "Effect": "Allow",
                "Action": ["ec2:CreateLaunchTemplate"],
                "Resource": f"{arn}:launch-template/*",
            },
        ],
    }


# ------------------------------------------------------------------ 가드·공통
def _require_real_aws(expected_account: str) -> None:
    """실 AWS 전용(가드 ①) + 대상 계정 대조(가드 ②)."""
    endpoint = endpoint_url()
    if endpoint:
        sys.exit(
            f"AWS_ENDPOINT_URL={endpoint} 설정됨 — 이 스크립트는 실 AWS 전용이다"
            "(LocalStack에는 elbv2가 없다). 셸에서 변수를 지우고 다시 실행할 것."
        )
    identity = aws_client("sts").get_caller_identity()
    if identity["Account"] != expected_account:
        sys.exit(
            f"--account {expected_account} ≠ 호출 주체의 계정 {identity['Account']} — "
            "AWS 프로필을 확인할 것."
        )
    print(f"[smoke] 계정 {identity['Account']} · 주체 {identity['Arn']} · 리전 {default_region()}")


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _tags(
    name: str,
    environment: Optional[str] = None,
    *,
    pending: bool = False,
    role: Optional[str] = None,
) -> list[dict]:
    tags = [{"Key": "Name", "Value": name}, {"Key": SMOKE_TAG_KEY, "Value": SMOKE_TAG_VALUE}]
    if environment:
        tags.append({"Key": "Environment", "Value": environment})
    if pending:
        tags.append({"Key": INIT_TAG_KEY, "Value": INIT_PENDING})
    if role:
        tags.append({"Key": ROLE_TAG_KEY, "Value": role})
    return tags


def _tag_spec(
    resource_type: str,
    name: str,
    environment: Optional[str] = None,
    *,
    pending: bool = False,
    role: Optional[str] = None,
) -> list[dict]:
    return [{
        "ResourceType": resource_type,
        "Tags": _tags(name, environment, pending=pending, role=role),
    }]


def _vpc_filter(vpc_id: str) -> dict:
    return {"Name": "vpc-id", "Values": [vpc_id]}


def _tag_of(resource: dict, key: str) -> Optional[str]:
    return next((t["Value"] for t in resource.get("Tags") or [] if t["Key"] == key), None)


def _name_of(resource: dict) -> Optional[str]:
    return _tag_of(resource, "Name")


def _init_pending(resource: dict) -> bool:
    """이 스크립트가 만들고 설정을 끝내지 못한 자원인가(맨 위 "up은 되돌리지 않는다"의 예외)."""
    return _tag_of(resource, INIT_TAG_KEY) == INIT_PENDING


def _mark_init_done(ec2, resource_id: str) -> None:
    ec2.create_tags(Resources=[resource_id], Tags=[{"Key": INIT_TAG_KEY, "Value": INIT_DONE}])


def _retry(fn: Callable[[], Any], what: str, codes: tuple[str, ...], timeout: int = 600) -> Any:
    """의존 자원이 늦게 사라지는 삭제(ALB ENI·종료 중 인스턴스)를 기다렸다 다시 부른다."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return fn()
        except ClientError as exc:
            if _code(exc) not in codes or time.monotonic() > deadline:
                raise
            print(f"[smoke]   … {what} 대기({_code(exc)})")
            time.sleep(15)


# ------------------------------------------------------------------ 조회
def _find_vpc(ec2) -> Optional[str]:
    vpcs = ec2.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": [VPC_NAME]}, _SMOKE_FILTER]
    )["Vpcs"]
    return vpcs[0]["VpcId"] if vpcs else None


def _find_subnets(ec2, vpc_id: str) -> dict[str, str]:
    """AZ 접미 → 서브넷 ID."""
    found = ec2.describe_subnets(Filters=[_vpc_filter(vpc_id)])["Subnets"]
    by_name = {_name_of(s): s["SubnetId"] for s in found}
    return {az: by_name[f"{PREFIX}-{az}"] for az in SUBNETS if f"{PREFIX}-{az}" in by_name}


def _find_security_groups(ec2, vpc_id: str) -> dict[str, dict]:
    """GroupName → 응답(GroupId·Tags·규칙)."""
    found = ec2.describe_security_groups(Filters=[_vpc_filter(vpc_id)])["SecurityGroups"]
    return {g["GroupName"]: g for g in found}


def _find_instances(ec2, vpc_id: str) -> dict[str, dict]:
    """VPC 안의 살아 있는 인스턴스 전부(Name 태그 → 응답). ASG가 띄운 것은 ID로 들어온다."""
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[_vpc_filter(vpc_id), {"Name": "instance-state-name", "Values": _LIVE_STATES}]
    )
    return {
        _name_of(i) or i["InstanceId"]: i
        for page in pages
        for r in page["Reservations"]
        for i in r["Instances"]
    }


def _find_nacl(ec2, vpc_id: str) -> Optional[dict]:
    found = ec2.describe_network_acls(
        Filters=[_vpc_filter(vpc_id), {"Name": "tag:Name", "Values": [NACL_NAME]}]
    )["NetworkAcls"]
    return found[0] if found else None


def _find_volume(ec2) -> Optional[str]:
    found = ec2.describe_volumes(
        Filters=[
            {"Name": "tag:Name", "Values": [VOLUME_NAME]},
            _SMOKE_FILTER,
            {"Name": "status", "Values": ["creating", "available", "in-use"]},
        ]
    )["Volumes"]
    return found[0]["VolumeId"] if found else None


def _find_target_group(elbv2) -> Optional[dict]:
    try:
        return elbv2.describe_target_groups(Names=[TG_NAME])["TargetGroups"][0]
    except ClientError as exc:
        if _code(exc) == "TargetGroupNotFound":
            return None
        raise


def _find_load_balancer(elbv2) -> Optional[dict]:
    try:
        return elbv2.describe_load_balancers(Names=[ALB_NAME])["LoadBalancers"][0]
    except ClientError as exc:
        if _code(exc) == "LoadBalancerNotFound":
            return None
        raise


def _user_exists(iam) -> bool:
    try:
        iam.get_user(UserName=APP_USER)
        return True
    except ClientError as exc:
        if _code(exc) == "NoSuchEntity":
            return False
        raise


def _app_policy_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:policy/{APP_POLICY}"


def _managed_policy_exists(iam, arn: str) -> bool:
    try:
        iam.get_policy(PolicyArn=arn)
        return True
    except ClientError as exc:
        if _code(exc) == "NoSuchEntity":
            return False
        raise


def _budget_exists(budgets, account: str) -> bool:
    try:
        budgets.describe_budget(AccountId=account, BudgetName=BUDGET_NAME)
        return True
    except ClientError as exc:
        if _code(exc) == "NotFoundException":
            return False
        raise


def _app_asgs(asg, subnet_ids: set[str]) -> list[str]:
    """스모크 서브넷에 걸친 ASG — ENABLE_AUTOSCALING이 만든 것. VPC 울타리로 가린다."""
    pages = asg.get_paginator("describe_auto_scaling_groups").paginate()
    return [
        g["AutoScalingGroupName"]
        for page in pages
        for g in page["AutoScalingGroups"]
        if subnet_ids & set(filter(None, g.get("VPCZoneIdentifier", "").split(",")))
    ]


def _app_launch_templates(ec2) -> list[dict]:
    return ec2.describe_launch_templates(
        Filters=[{"Name": "launch-template-name", "Values": [f"{APP_LT_PREFIX}*"]}]
    )["LaunchTemplates"]


# ------------------------------------------------------------------ 목록(status)
def inventory(clients: dict, account: str) -> list[tuple[str, str, Optional[str]]]:
    """(종류, 이름, ID 또는 None). up이 만들 것과 down이 지울 것의 목록이다."""
    ec2, elbv2, iam, budgets = clients["ec2"], clients["elbv2"], clients["iam"], clients["budgets"]
    vpc_id = _find_vpc(ec2)
    rows: list[tuple[str, str, Optional[str]]] = [("VPC", VPC_NAME, vpc_id)]
    subnets = _find_subnets(ec2, vpc_id) if vpc_id else {}
    sgs = _find_security_groups(ec2, vpc_id) if vpc_id else {}
    instances = _find_instances(ec2, vpc_id) if vpc_id else {}
    rows += [("Subnet", f"{PREFIX}-{az}", subnets.get(az)) for az in SUBNETS]
    rows += [("SecurityGroup", name, sgs[name]["GroupId"] if name in sgs else None) for name in SECURITY_GROUPS]
    acl = _find_nacl(ec2, vpc_id) if vpc_id else None
    rows.append(("NetworkAcl", NACL_NAME, acl["NetworkAclId"] if acl else None))
    rows += [
        ("Instance", spec.name, instances[spec.name]["InstanceId"] if spec.name in instances else None)
        for spec in INSTANCES
    ]
    tg, lb = _find_target_group(elbv2), _find_load_balancer(elbv2)
    rows.append(("TargetGroup", TG_NAME, tg["TargetGroupArn"] if tg else None))
    rows.append(("LoadBalancer", ALB_NAME, lb["LoadBalancerArn"] if lb else None))
    rows.append(("Volume", VOLUME_NAME, _find_volume(ec2)))
    rows.append(("IamUser", APP_USER, APP_USER if _user_exists(iam) else None))
    policy_arn = _app_policy_arn(account)
    rows.append(("IamPolicy", APP_POLICY, policy_arn if _managed_policy_exists(iam, policy_arn) else None))
    rows.append(("Budget", BUDGET_NAME, BUDGET_NAME if _budget_exists(budgets, account) else None))
    return rows


def _print_rows(rows: list[tuple[str, str, Optional[str]]]) -> None:
    for kind, name, rid in rows:
        print(f"  {'✅' if rid else '— '} {kind:<14} {name:<32} {rid or '없음'}")


def status(clients: dict, account: str) -> None:
    ec2, elbv2, asg, iam = clients["ec2"], clients["elbv2"], clients["asg"], clients["iam"]
    print("[smoke] 스모크 자원")
    _print_rows(inventory(clients, account))

    vpc_id = _find_vpc(ec2)
    if vpc_id:
        print("[smoke] 인스턴스 상태")
        # 첫 판정 가능 시각은 세 대 공통이다 — evaluate_ec2는 관측치 검사가 prod 검사보다
        # 앞이라, 그 전에는 idle-dev의 RIGHTSIZING도 web-1·web-2의 SKIP_PROD_PROTECTED도
        # 보이지 않고 셋 다 SKIP_INSUFFICIENT_DATA다.
        for name, inst in sorted(_find_instances(ec2, vpc_id).items()):
            print(f"  {name:<28} {inst['InstanceType']:<10} {inst['State']['Name']:<9} 기동 {inst['LaunchTime']:%m/%d %H:%M}")
            ready = inst["LaunchTime"] + timedelta(hours=MIN_DATAPOINTS)
            print(
                f"  └ 첫 판정 가능 ≈ {ready:%m/%d %H:%M} (최초 기동 + MIN_DATAPOINTS {MIN_DATAPOINTS}시간 — "
                "그 전에는 SKIP_INSUFFICIENT_DATA. 재시작하면 기동 시각은 바뀌어도 관측치는 이어진다)"
            )
        tg = _find_target_group(elbv2)
        if tg:
            health = elbv2.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
            for d in health["TargetHealthDescriptions"]:
                print(f"  TG 대상 {d['Target']['Id']}: {d['TargetHealth']['State']}")

        subnet_ids = set(_find_subnets(ec2, vpc_id).values())
        leftovers = _app_asgs(asg, subnet_ids)
        if leftovers:
            print(f"[smoke] 앱이 만든 ASG(과금 인스턴스를 띄운다): {leftovers}")

    lts = _app_launch_templates(ec2)
    if lts:
        print(f"[smoke] 앱이 만든 Launch Template: {[lt['LaunchTemplateName'] for lt in lts]}")
    snapshots = ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]
    if snapshots:
        # EBS_DELETE_UNATTACHED가 남기는 스냅숏에는 태그가 없어 우리 것인지 가릴 수 없다 — 보고만
        print(f"[smoke] 이 리전의 자기 소유 스냅숏 {len(snapshots)}건: {[s['SnapshotId'] for s in snapshots]}")
    if _user_exists(iam):
        for key in iam.list_access_keys(UserName=APP_USER)["AccessKeyMetadata"]:
            print(f"[smoke] 앱 키 {key['AccessKeyId'][:8]}… {key['Status']} · 발급 {key['CreateDate']:%m/%d}")


# ------------------------------------------------------------------ 생성(up)
def _ensure_vpc(ec2) -> str:
    vpc_id = _find_vpc(ec2)
    if vpc_id:
        return vpc_id
    vpc_id = ec2.create_vpc(CidrBlock=VPC_CIDR, TagSpecifications=_tag_spec("vpc", VPC_NAME))["Vpc"]["VpcId"]
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
    print(f"[smoke] VPC {vpc_id} 생성 — 인터넷 게이트웨이 없음")
    return vpc_id


def _ensure_subnets(ec2, vpc_id: str, region: str) -> dict[str, str]:
    subnets = _find_subnets(ec2, vpc_id)
    for az, cidr in SUBNETS.items():
        if az in subnets:
            continue
        name = f"{PREFIX}-{az}"
        subnets[az] = ec2.create_subnet(
            VpcId=vpc_id,
            CidrBlock=cidr,
            AvailabilityZone=f"{region}{az}",
            TagSpecifications=_tag_spec("subnet", name),
        )["Subnet"]["SubnetId"]
        print(f"[smoke] Subnet {name} {subnets[az]} 생성")
    return subnets


# AWS가 새 VPC SG에 넣는 기본 egress(전체 허용). 격리 SG는 이것까지 걷는다.
_DEFAULT_EGRESS = {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}


def _revoke_all_rules(ec2, group: dict) -> None:
    if group["IpPermissions"]:
        ec2.revoke_security_group_ingress(GroupId=group["GroupId"], IpPermissions=group["IpPermissions"])
    if group["IpPermissionsEgress"]:
        ec2.revoke_security_group_egress(GroupId=group["GroupId"], IpPermissions=group["IpPermissionsEgress"])


def _authorize_ingress(ec2, group_id: str, permissions: list[dict]) -> None:
    try:
        ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=permissions)
    except ClientError as exc:
        # 지난 up이 규칙은 넣고 표지를 바꾸기 전에 끊겼다 — 규칙이 이미 있으면 된 것이다
        if _code(exc) != "InvalidPermission.Duplicate":
            raise


def _ensure_security_groups(ec2, vpc_id: str) -> dict[str, str]:
    groups = _find_security_groups(ec2, vpc_id)
    for name in SECURITY_GROUPS:
        if name in groups:
            continue
        role = ROLE_ISOLATION if name == SG_ISOLATION else None
        group_id = ec2.create_security_group(
            GroupName=name,
            Description=f"vigilantis smoke: {name}",
            VpcId=vpc_id,
            TagSpecifications=_tag_spec("security-group", name, pending=True, role=role),
        )["GroupId"]
        # 갓 만든 그룹의 상태는 AWS 기본값(기본 egress 1개)이다 — 다시 조회하지 않는다
        groups[name] = {
            "GroupId": group_id,
            "Tags": _tags(name, pending=True, role=role),
            "IpPermissions": [],
            "IpPermissionsEgress": [_DEFAULT_EGRESS],
        }
        print(f"[smoke] SG {name} {group_id} 생성")
    ids = {name: g["GroupId"] for name, g in groups.items()}

    ingress = {
        SG_ALB: [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": VPC_CIDR}]}],
        SG_WEB: [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "UserIdGroupPairs": [{"GroupId": ids[SG_ALB]}]}],
        SG_OPEN_SSH: [{
            "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
            "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "vigilantis smoke: OpenIP bait, no public IP"}],
        }],
    }
    # 규칙은 초기화가 끝나지 않은(pending) 그룹에만 넣는다. done인 그룹은 스모크가 바꿨어도 손대지
    # 않는다(맨 위 "up은 되돌리지 않는다"). 설정이 실패하면 표지가 pending으로 남은 채 up이 여기서
    # 멈추고 — 미완성 SG의 ID는 출력되지 않는다 — 다음 up이 같은 설정을 이어서 끝낸다.
    for name in SECURITY_GROUPS:
        group = groups[name]
        if not _init_pending(group):
            continue
        if name in ingress:
            _authorize_ingress(ec2, group["GroupId"], ingress[name])
        if name == SG_ISOLATION:
            # 기본 egress(전체 허용)까지 걷어 규칙 0개로 만든다 — 격리는 나가는 길도 막는다
            _revoke_all_rules(ec2, group)
        _mark_init_done(ec2, group["GroupId"])
        print(f"[smoke] SG {name} {group['GroupId']} 규칙 설정")
    return ids


def _associate_nacl(ec2, vpc_id: str, acl_id: str, subnet_id: str) -> None:
    for acl in ec2.describe_network_acls(Filters=[_vpc_filter(vpc_id)])["NetworkAcls"]:
        for assoc in acl["Associations"]:
            if assoc.get("SubnetId") != subnet_id or acl["NetworkAclId"] == acl_id:
                continue
            ec2.replace_network_acl_association(
                AssociationId=assoc["NetworkAclAssociationId"], NetworkAclId=acl_id
            )
            print(f"[smoke] NACL {acl_id} → 서브넷 {subnet_id} 연결")


def _is_allow_all(entry: dict) -> bool:
    return entry["RuleAction"] == "allow" and entry["Protocol"] == "-1" and entry.get("CidrBlock") == "0.0.0.0/0"


def _ensure_nacl(ec2, vpc_id: str, subnet_id: str) -> str:
    acl = _find_nacl(ec2, vpc_id)
    fresh = acl is None
    if fresh:
        acl = ec2.create_network_acl(
            VpcId=vpc_id, TagSpecifications=_tag_spec("network-acl", NACL_NAME, pending=True)
        )["NetworkAcl"]
        print(f"[smoke] NACL {acl['NetworkAclId']} 생성")
    acl_id = acl["NetworkAclId"]
    if fresh or _init_pending(acl):
        # 연결보다 허용이 먼저다 — 반대 순서면 그 사이 서브넷 통신이 끊긴다. 끊긴 초기화를 이을 때는
        # 빠진 방향만 채운다. 허용이 양방향 다 서기(done) 전에는 서브넷에 붙이지 않는다.
        for egress in (False, True):
            entry = next(
                (e for e in acl.get("Entries", [])
                 if e["RuleNumber"] == NACL_ALLOW_ALL_RULE and e["Egress"] == egress),
                None,
            )
            if entry is None:
                ec2.create_network_acl_entry(
                    NetworkAclId=acl_id,
                    RuleNumber=NACL_ALLOW_ALL_RULE,
                    Protocol="-1",
                    RuleAction="allow",
                    Egress=egress,
                    CidrBlock="0.0.0.0/0",
                )
            elif not _is_allow_all(entry):
                sys.exit(
                    f"[smoke] NACL {acl_id}의 규칙 {NACL_ALLOW_ALL_RULE}({'아웃' if egress else '인'}바운드)이 "
                    "전체 허용이 아니다 — 서브넷에 붙이지 않고 멈춘다. 콘솔에서 확인할 것."
                )
        _mark_init_done(ec2, acl_id)
        print(f"[smoke] NACL {acl_id} 허용 규칙 {NACL_ALLOW_ALL_RULE}(인·아웃바운드) 설정")
    # 연결은 매번 확인한다 — 풀린 채 남으면 화면의 PROTECTED_BY 엣지와 조치 대상이 갈린다
    _associate_nacl(ec2, vpc_id, acl_id, subnet_id)
    return acl_id


def _ensure_instances(ec2, ssm, vpc_id: str, subnets: dict[str, str], sgs: dict[str, str]) -> dict[str, str]:
    live = _find_instances(ec2, vpc_id)
    ids = {name: live[name]["InstanceId"] for name in live}
    new = []
    ami = None
    for spec in INSTANCES:
        if spec.name in ids:
            continue
        ami = ami or ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
        # 공인 IP는 서브넷 기본값(비기본 VPC = 끔)을 따른다 — 인터넷 게이트웨이도 없다
        ids[spec.name] = ec2.run_instances(
            ImageId=ami,
            InstanceType=spec.instance_type,
            MinCount=1,
            MaxCount=1,
            SubnetId=subnets[spec.az],
            SecurityGroupIds=[sgs[g] for g in spec.groups],
            UserData=_USER_DATA.format(name=spec.name),
            # 버스트 초과분 과금을 막는다(unlimited가 t3 기본값)
            CreditSpecification={"CpuCredits": "standard"},
            MetadataOptions={"HttpTokens": "required"},
            TagSpecifications=(
                _tag_spec("instance", spec.name, spec.environment)
                + _tag_spec("volume", spec.name)
                + _tag_spec("network-interface", spec.name)
            ),
        )["Instances"][0]["InstanceId"]
        new.append(ids[spec.name])
        print(f"[smoke] EC2 {spec.name}({spec.instance_type}) {ids[spec.name]} 생성")
    if new:
        ec2.get_waiter("instance_running").wait(InstanceIds=new)
    return ids


def _ensure_load_balancer(elbv2, vpc_id: str, subnets: dict[str, str], sgs: dict[str, str], instances: dict[str, str]) -> str:
    tg = _find_target_group(elbv2)
    fresh = tg is None
    if fresh:
        tg = elbv2.create_target_group(
            Name=TG_NAME, Protocol="HTTP", Port=80, VpcId=vpc_id,
            TargetType="instance", HealthCheckPath="/", Tags=_tags(TG_NAME, pending=True),
        )["TargetGroups"][0]
        print(f"[smoke] TG {TG_NAME} 생성")
    tg_arn = tg["TargetGroupArn"]
    if fresh or _init_pending({"Tags": elbv2.describe_tags(ResourceArns=[tg_arn])["TagDescriptions"][0]["Tags"]}):
        # 등록은 초기화 때 한 번만 한다 — 다시 돌린 up이 격리(등록 해제)를 되돌리면 안 된다.
        # 끊긴 초기화를 이을 때는 아직 등록되지 않은 대상만 넣는다.
        health = elbv2.describe_target_health(TargetGroupArn=tg_arn)["TargetHealthDescriptions"]
        registered = {d["Target"]["Id"] for d in health}
        targets = [
            {"Id": instances[s.name]}
            for s in INSTANCES
            if s.in_target_group and instances[s.name] not in registered
        ]
        if targets:
            elbv2.register_targets(TargetGroupArn=tg_arn, Targets=targets)
        elbv2.add_tags(ResourceArns=[tg_arn], Tags=[{"Key": INIT_TAG_KEY, "Value": INIT_DONE}])
        print(f"[smoke] TG {TG_NAME} 대상 등록")
    lb = _find_load_balancer(elbv2)
    if lb is None:
        lb = elbv2.create_load_balancer(
            Name=ALB_NAME, Subnets=list(subnets.values()), SecurityGroups=[sgs[SG_ALB]],
            Scheme="internal", Type="application", IpAddressType="ipv4", Tags=_tags(ALB_NAME),
        )["LoadBalancers"][0]
        print(f"[smoke] ALB {ALB_NAME} 생성(internal)")
    if not elbv2.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])["Listeners"]:
        elbv2.create_listener(
            LoadBalancerArn=lb["LoadBalancerArn"], Protocol="HTTP", Port=80,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": tg_arn}],
        )
    return tg_arn


def _ensure_volume(ec2, region: str) -> str:
    vol_id = _find_volume(ec2)
    if vol_id:
        return vol_id
    vol_id = ec2.create_volume(
        AvailabilityZone=f"{region}a", Size=1, VolumeType="gp3",
        TagSpecifications=_tag_spec("volume", VOLUME_NAME),
    )["VolumeId"]
    print(f"[smoke] EBS {VOLUME_NAME} {vol_id} 생성(1 GiB gp3, 미연결)")
    return vol_id


def _ensure_app_user(iam, account: str, region: str, vpc_id: str) -> None:
    if not _user_exists(iam):
        iam.create_user(UserName=APP_USER, Tags=_tags(APP_USER))
        print(f"[smoke] IAM 사용자 {APP_USER} 생성 — 키는 만들지 않는다(ADR-0009 §2)")
    # 이 사용자의 권한은 아래 관리형 정책 하나뿐이다(ADR-0009 §2 — 정책은 Action Whitelist의
    # 거울). 인라인은 더 만들지 않으므로 남아 있다면 관리형 전환 전에 붙은 것이다 — 걷어야
    # 권한이 두 문서의 합집합이 되지 않는다.
    for stale in iam.list_user_policies(UserName=APP_USER)["PolicyNames"]:
        iam.delete_user_policy(UserName=APP_USER, PolicyName=stale)
        print(f"[smoke] 남아 있던 인라인 정책 {stale} 삭제 — 앱 권한은 관리형 정책 하나로 모은다")
    document = build_app_policy(account, region, vpc_id)
    arn = _app_policy_arn(account)
    try:
        policy = iam.get_policy(PolicyArn=arn)["Policy"]
    except ClientError as exc:
        if _code(exc) != "NoSuchEntity":
            raise
        iam.create_policy(PolicyName=APP_POLICY, PolicyDocument=json.dumps(document), Tags=_tags(APP_POLICY))
        print(f"[smoke] IAM 관리형 정책 {APP_POLICY} 생성")
    else:
        # VPC를 다시 세웠으면 정책의 VPC ARN도 따라가야 한다 — 문서가 바뀐 때만 새 버전을 올린다.
        # botocore가 IAM 응답의 정책 문서를 dict로 풀어 주므로 그대로 비교한다.
        current = iam.get_policy_version(
            PolicyArn=arn, VersionId=policy["DefaultVersionId"]
        )["PolicyVersion"]["Document"]
        if current != document:
            _make_room_for_policy_version(iam, arn)
            iam.create_policy_version(PolicyArn=arn, PolicyDocument=json.dumps(document), SetAsDefault=True)
            print(f"[smoke] IAM 관리형 정책 {APP_POLICY} 새 버전을 기본으로")
    # 이미 붙은 정책의 재연결 멱등성에 기대지 않고 확인한 뒤 붙인다
    attached = iam.list_attached_user_policies(UserName=APP_USER)["AttachedPolicies"]
    if all(p["PolicyArn"] != arn for p in attached):
        iam.attach_user_policy(UserName=APP_USER, PolicyArn=arn)
        print(f"[smoke] 정책 {APP_POLICY} → 사용자 {APP_USER} 연결")


def _make_room_for_policy_version(iam, arn: str) -> None:
    """관리형 정책은 버전을 5개까지만 가진다 — 새 버전 자리를 가장 오래된 비기본 버전에서 낸다."""
    versions = iam.list_policy_versions(PolicyArn=arn)["Versions"]
    stale = sorted((v for v in versions if not v["IsDefaultVersion"]), key=lambda v: v["CreateDate"])
    for version in stale[: max(0, len(versions) - _POLICY_VERSION_LIMIT + 1)]:
        iam.delete_policy_version(PolicyArn=arn, VersionId=version["VersionId"])


def _ensure_asg_service_role(iam) -> None:
    """ENABLE_AUTOSCALING의 첫 ASG 생성에 필요한 서비스 연결 역할. 앱 키에는
    iam:CreateServiceLinkedRole을 주지 않으므로 관리자 쪽에서 미리 둔다(ADR-0009 §4)."""
    try:
        iam.get_role(RoleName=ASG_SERVICE_ROLE)
    except ClientError as exc:
        if _code(exc) != "NoSuchEntity":
            raise
        iam.create_service_linked_role(AWSServiceName=ASG_SERVICE)
        print(f"[smoke] 서비스 연결 역할 {ASG_SERVICE_ROLE} 생성")


def _ensure_budget(budgets, account: str, email: str) -> None:
    if _budget_exists(budgets, account):
        return
    budgets.create_budget(
        AccountId=account,
        Budget={
            "BudgetName": BUDGET_NAME,
            "BudgetLimit": {"Amount": BUDGET_USD, "Unit": "USD"},
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
        },
        NotificationsWithSubscribers=[
            {
                "Notification": {
                    "NotificationType": kind,
                    "ComparisonOperator": "GREATER_THAN",
                    "Threshold": float(percent),
                    "ThresholdType": "PERCENTAGE",
                },
                "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}],
            }
            for kind, percent in BUDGET_ALERTS
        ],
    )
    print(f"[smoke] Budget {BUDGET_NAME} 생성 — 월 ${BUDGET_USD}, 알림 {BUDGET_ALERTS}")


def up(clients: dict, account: str, region: str, budget_email: str) -> None:
    ec2, elbv2, iam = clients["ec2"], clients["elbv2"], clients["iam"]
    # 비용 그물이 자원보다 먼저 선다
    _ensure_budget(clients["budgets"], account, budget_email)
    vpc_id = _ensure_vpc(ec2)
    subnets = _ensure_subnets(ec2, vpc_id, region)
    sgs = _ensure_security_groups(ec2, vpc_id)
    acl_id = _ensure_nacl(ec2, vpc_id, subnets[NACL_AZ])
    instances = _ensure_instances(ec2, clients["ssm"], vpc_id, subnets, sgs)
    tg_arn = _ensure_load_balancer(elbv2, vpc_id, subnets, sgs, instances)
    vol_id = _ensure_volume(ec2, region)
    _ensure_app_user(iam, account, region, vpc_id)
    _ensure_asg_service_role(iam)

    print("[smoke] 스모크 파라미터(런북이 겨누는 값 — 대본이 손으로 조립하지 않게 여기서 찍는다)")
    print(f"  EC2_ISOLATE target_group_arn : {tg_arn}")
    print(f"  EC2_ISOLATE isolation_group_id: {sgs[SG_ISOLATION]}")
    print(f"  NACL_ADD_DENY 대상            : arn:aws:ec2:{region}:{account}:network-acl/{acl_id}")
    print(f"  SG_DELETE_ISOLATED 대상       : {sgs[SG_UNUSED]}")
    print(f"  EBS_DELETE_UNATTACHED 대상    : {vol_id}")
    print("[smoke] 다음 단계(ADR-0009 §6)")
    print(f"  1. 앱 키 발급: aws iam create-access-key --user-name {APP_USER}  → .env에만 넣는다")
    print("  2. .env 전환: 키 교체 + AWS_ENDPOINT_URL 줄 삭제 → sts Arn이 앱 사용자인지 확인")
    print("  3. 앱 키로 DryRun을 1회씩 불러 정책의 조건 키를 실측한다(UnauthorizedOperation이면 정책 수정)")


# ------------------------------------------------------------------ 정리(down)
def _delete_app_asgs(asg, subnet_ids: set[str]) -> None:
    names = _app_asgs(asg, subnet_ids)
    for name in names:
        # 먼저 지우지 않으면 인스턴스를 지워도 다시 띄운다
        asg.delete_auto_scaling_group(AutoScalingGroupName=name, ForceDelete=True)
        print(f"[smoke] ASG {name} 삭제 요청")
    deadline = time.monotonic() + 600
    while names and time.monotonic() < deadline:
        names = _app_asgs(asg, subnet_ids)
        if names:
            print(f"[smoke]   … ASG 삭제 대기 {names}")
            time.sleep(15)
    if names:
        sys.exit(f"[smoke] ASG가 10분 안에 사라지지 않았다: {names} — 콘솔에서 확인 후 down을 다시 돌릴 것")


def _delete_security_groups(ec2, vpc_id: str) -> None:
    groups = [
        g for g in ec2.describe_security_groups(Filters=[_vpc_filter(vpc_id)])["SecurityGroups"]
        if g["GroupName"] != "default"
    ]
    # 서로를 참조하는 규칙(web ← alb)이 있으면 어느 쪽도 먼저 지워지지 않는다 — 규칙부터 걷는다
    for g in groups:
        _revoke_all_rules(ec2, g)
    for g in groups:
        _retry(lambda gid=g["GroupId"]: ec2.delete_security_group(GroupId=gid),
               f"SG {g['GroupName']}", ("DependencyViolation",))
        print(f"[smoke] SG {g['GroupName']} {g['GroupId']} 삭제")


def _delete_network_acls(ec2, vpc_id: str) -> None:
    acls = ec2.describe_network_acls(Filters=[_vpc_filter(vpc_id)])["NetworkAcls"]
    default = next(a["NetworkAclId"] for a in acls if a["IsDefault"])
    for acl in acls:
        if acl["IsDefault"]:
            continue
        for assoc in acl["Associations"]:
            ec2.replace_network_acl_association(
                AssociationId=assoc["NetworkAclAssociationId"], NetworkAclId=default
            )
        ec2.delete_network_acl(NetworkAclId=acl["NetworkAclId"])
        print(f"[smoke] NACL {acl['NetworkAclId']} 삭제")


def _delete_app_user(iam) -> None:
    if not _user_exists(iam):
        return
    for key in iam.list_access_keys(UserName=APP_USER)["AccessKeyMetadata"]:
        iam.delete_access_key(UserName=APP_USER, AccessKeyId=key["AccessKeyId"])
        print(f"[smoke] 앱 키 {key['AccessKeyId'][:8]}… 삭제")
    for policy in iam.list_user_policies(UserName=APP_USER)["PolicyNames"]:
        iam.delete_user_policy(UserName=APP_USER, PolicyName=policy)
    for policy in iam.list_attached_user_policies(UserName=APP_USER)["AttachedPolicies"]:
        iam.detach_user_policy(UserName=APP_USER, PolicyArn=policy["PolicyArn"])
    iam.delete_user(UserName=APP_USER)
    print(f"[smoke] IAM 사용자 {APP_USER} 삭제")


def _delete_app_policy(iam, account: str) -> None:
    """사용자를 지워도 관리형 정책은 남는다 — 따로 걷는다. 사용자 쪽 연결은 _delete_app_user가 푼다."""
    arn = _app_policy_arn(account)
    if not _managed_policy_exists(iam, arn):
        return
    # 비기본 버전을 먼저 지워야 정책이 지워진다(기본 버전은 정책과 함께 사라진다)
    for version in iam.list_policy_versions(PolicyArn=arn)["Versions"]:
        if not version["IsDefaultVersion"]:
            iam.delete_policy_version(PolicyArn=arn, VersionId=version["VersionId"])
    iam.delete_policy(PolicyArn=arn)
    print(f"[smoke] IAM 관리형 정책 {APP_POLICY} 삭제")


def down(clients: dict, account: str) -> None:
    ec2, elbv2, asg = clients["ec2"], clients["elbv2"], clients["asg"]
    vpc_id = _find_vpc(ec2)
    if vpc_id:
        subnet_ids = set(_find_subnets(ec2, vpc_id).values())
        _delete_app_asgs(asg, subnet_ids)

        lb = _find_load_balancer(elbv2)
        if lb:
            elbv2.delete_load_balancer(LoadBalancerArn=lb["LoadBalancerArn"])
            elbv2.get_waiter("load_balancers_deleted").wait(LoadBalancerArns=[lb["LoadBalancerArn"]])
            print(f"[smoke] ALB {ALB_NAME} 삭제")
        tg = _find_target_group(elbv2)
        if tg:
            _retry(lambda: elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"]),
                   "TG", ("ResourceInUse",))
            print(f"[smoke] TG {TG_NAME} 삭제")

        instance_ids = [i["InstanceId"] for i in _find_instances(ec2, vpc_id).values()]
        if instance_ids:
            ec2.terminate_instances(InstanceIds=instance_ids)
            ec2.get_waiter("instance_terminated").wait(InstanceIds=instance_ids)
            print(f"[smoke] EC2 {len(instance_ids)}대 종료: {instance_ids}")

        _delete_security_groups(ec2, vpc_id)
        _delete_network_acls(ec2, vpc_id)
        for subnet_id in subnet_ids:
            _retry(lambda sid=subnet_id: ec2.delete_subnet(SubnetId=sid),
                   f"Subnet {subnet_id}", ("DependencyViolation",))
            print(f"[smoke] Subnet {subnet_id} 삭제")
        _retry(lambda: ec2.delete_vpc(VpcId=vpc_id), "VPC", ("DependencyViolation",))
        print(f"[smoke] VPC {vpc_id} 삭제")

    for lt in _app_launch_templates(ec2):
        ec2.delete_launch_template(LaunchTemplateId=lt["LaunchTemplateId"])
        print(f"[smoke] Launch Template {lt['LaunchTemplateName']} 삭제")
    vol_id = _find_volume(ec2)
    if vol_id:
        ec2.delete_volume(VolumeId=vol_id)
        print(f"[smoke] EBS {vol_id} 삭제")
    _delete_app_user(clients["iam"])
    _delete_app_policy(clients["iam"], account)
    # Budget은 남긴다 — 정리 뒤 남은 과금을 월말까지 잡는 그물이다. 서비스 연결 역할도 무료라 둔다.
    print(f"[smoke] Budget {BUDGET_NAME}은 남겼다 — 월말 청구 확인 뒤 콘솔에서 지운다")
    print("[smoke] 정리 끝 — status로 잔여 0건을 확인할 것")


# ------------------------------------------------------------------ main
def main() -> None:
    # Windows 콘솔(cp949)은 em dash 등 출력 시 UnicodeEncodeError로 죽는다 — UTF-8로 강제
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Vigilantis 실 AWS 스모크 환경 (ADR-0009)")
    parser.add_argument("command", choices=("status", "up", "down", "policy"))
    parser.add_argument("--account", required=True, help="대상 AWS 계정 ID — 호출 주체(sts)와 같아야 한다")
    parser.add_argument("--budget-email", help="up: AWS Budgets 알림을 받을 주소")
    parser.add_argument("--yes", action="store_true", help="up·down: 실제로 바꾼다(없으면 목록만 보여 준다)")
    args = parser.parse_args()

    if args.command == "up" and rightsizing_target_type(IDLE_DEV_TYPE) is None:
        sys.exit(
            f"[smoke] {IDLE_DEV_TYPE}에 다운사이징 목표가 없다 — schemas/rightsizing_policy.py 변경을 따라 "
            "IDLE_DEV_TYPE을 고칠 것."
        )
    _require_real_aws(args.account)
    region = default_region()
    clients = {
        "ec2": aws_client("ec2", region),
        "elbv2": aws_client("elbv2", region),
        "asg": aws_client("autoscaling", region),
        "ssm": aws_client("ssm", region),
        "iam": aws_client("iam", region),
        # Budgets는 전역 서비스이며 us-east-1 엔드포인트만 받는다
        "budgets": aws_client("budgets", "us-east-1"),
    }

    if args.command == "policy":
        vpc_id = _find_vpc(clients["ec2"]) or "<VPC 미생성>"
        print(json.dumps(build_app_policy(args.account, region, vpc_id), ensure_ascii=False, indent=2))
    elif args.command == "status":
        status(clients, args.account)
    elif args.command == "up":
        if not args.budget_email:
            sys.exit("up에는 --budget-email이 필요하다 — 비용 알림 없이 과금 자원을 세우지 않는다.")
        if not args.yes:
            missing = [row for row in inventory(clients, args.account) if row[2] is None]
            print(f"[smoke] 생성 예정 {len(missing)}건 — 실제로 만들려면 --yes")
            _print_rows(missing)
            return
        up(clients, args.account, region, args.budget_email)
    else:
        if not args.yes:
            print("[smoke] 삭제 대상(스모크 태그 자원 + 스모크 VPC 안 전부) — 실제로 지우려면 --yes")
            _print_rows([row for row in inventory(clients, args.account) if row[2]])
            return
        down(clients, args.account)


if __name__ == "__main__":
    main()
