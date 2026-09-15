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
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다. AWS를 부르지 않는다(④는 대역).

from __future__ import annotations

import copy
import fnmatch
import importlib.util
import os
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

from schemas.rightsizing_policy import rightsizing_target_type  # noqa: E402
from schemas.runbook_parameters import RuleNumber  # noqa: E402
from services.aws import executor, rollback  # noqa: E402

REGION = "ap-northeast-2"
VPC_ID = "vpc-0123456789abcdef0"
POLICY = smoke.build_app_policy("123456789012", REGION, VPC_ID)

# boto3 클라이언트 이름 → IAM 서비스 접두
_IAM_SERVICE = {"ec2": "ec2", "elbv2": "elasticloadbalancing", "autoscaling": "autoscaling"}


def _iam_action(operation: str) -> str:
    """"ec2.modify_instance_attribute" → "ec2:ModifyInstanceAttribute"."""
    service, name = operation.split(".", 1)
    return f"{_IAM_SERVICE[service]}:{''.join(part.capitalize() for part in name.split('_'))}"


def _code_operations() -> set[str]:
    """코드가 부르는 AWS 작업 — precheck 명세(RUNBOOK_SPECS)와 실행·판정의 작업 상수."""
    operations = {op for spec in executor.RUNBOOK_SPECS.values() for op in spec.operations}
    for module in (executor, rollback):
        operations |= {
            value for name, value in vars(module).items()
            if name.startswith("_OP_") and isinstance(value, str)
        }
    return operations


def _granted() -> list[str]:
    return [action for statement in POLICY["Statement"] for action in statement["Action"]]


def _is_granted(action: str) -> bool:
    return any(fnmatch.fnmatchcase(action, pattern) for pattern in _granted())


def _resources(statement: dict) -> list[str]:
    resources = statement["Resource"]
    return resources if isinstance(resources, list) else [resources]


def test_policy_grants_every_aws_operation_the_code_calls():
    missing = sorted(a for a in {_iam_action(op) for op in _code_operations()} if not _is_granted(a))
    assert not missing, f"앱 정책에 없는 작업 — build_app_policy에 더할 것: {missing}"


def test_policy_grants_no_change_the_code_does_not_make():
    needed = {_iam_action(op) for op in _code_operations()}
    extra = sorted(a for a in _granted() if a not in smoke.READ_ACTIONS and a not in needed)
    assert not extra, f"코드가 부르지 않는 조치 권한: {extra}"


def test_every_change_statement_is_fenced():
    """조회 문장만 Resource "*"를 쓰고, 그것도 리전 조건으로 묶인다. 조치 문장은 리전이
    박힌 ARN이며, 특정 VPC 하나를 가리키지 않는 한 태그·VPC 조건이 붙는다."""
    for statement in POLICY["Statement"]:
        resources = _resources(statement)
        if set(statement["Action"]) <= set(smoke.READ_ACTIONS):
            assert statement["Condition"] == {"StringEquals": {"aws:RequestedRegion": REGION}}
            continue
        assert "*" not in resources, statement["Sid"]
        assert all(f":{REGION}:" in r for r in resources), statement["Sid"]
        pinned = all(r.endswith(VPC_ID) for r in resources)
        new_resource_only = statement["Sid"] in {
            "SnapshotsOfSmokeVolumes", "NewSecurityGroups", "LaunchTemplatesForAutoscaling",
        }
        assert "Condition" in statement or pinned or new_resource_only, statement["Sid"]


# 생성 작업 → (새로 생기는 자원, 생성 장소 자원)의 ARN 조각. AWS 서비스 권한 참조상 새 자원이
# 받는 조건 키는 요청 태그 계열뿐이라 ec2:Vpc·aws:ResourceTag로는 가둘 수 없다 — 붙이면 그 Allow가
# 영영 성립하지 않아 권한 거부로 끝난다(#348 리뷰). 울타리는 생성 장소 쪽 문장이 친다.
_CREATE_ACTIONS = {
    "ec2:CreateSecurityGroup": (":security-group/", ":vpc/"),
    "ec2:CreateSnapshot": ("::snapshot/", ":volume/"),
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
            keys = [k for operator in statement.get("Condition", {}).values() for k in operator]
            unsupported = [k for k in keys if not k.startswith(_NEW_RESOURCE_KEYS)]
            assert not unsupported, f"{statement['Sid']}: 새 자원에 없는 조건 키 {unsupported}"
        for statement in place:
            pinned = all(r.endswith(VPC_ID) for r in _resources(statement))
            assert "Condition" in statement or pinned, f"{statement['Sid']}: 생성 장소가 열려 있다"


def test_nacl_allow_rule_sits_at_the_top_of_what_ai_can_pick():
    """허용 규칙이 AI가 고를 수 있는 번호의 맨 끝이어야 어떤 차단 규칙도 먼저 평가된다."""
    adapter = TypeAdapter(RuleNumber)
    assert adapter.validate_python(smoke.NACL_ALLOW_ALL_RULE) == smoke.NACL_ALLOW_ALL_RULE
    with pytest.raises(ValidationError):
        adapter.validate_python(smoke.NACL_ALLOW_ALL_RULE + 1)


def test_non_production_instance_is_a_rightsizing_candidate():
    """production은 SKIP_PROD_PROTECTED로 빠지므로 FinOps 경로는 non-prod 대상에 달려 있다."""
    candidates = [s for s in smoke.INSTANCES if s.environment != "production"]
    assert candidates
    assert all(rightsizing_target_type(s.instance_type) for s in candidates)


def test_refuses_localstack_endpoint_before_calling_aws():
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "AWS_ENDPOINT_URL": "http://localhost:4566"}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "status", "--account", "000000000000"],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
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
                "NetworkAclId": "acl-default", "IsDefault": True, "Tags": [], "Entries": [],
                "Associations": [{"NetworkAclAssociationId": "aclassoc-0", "SubnetId": SUBNET}],
            }
        }

    def create_tags(self, Resources, Tags):
        self._write("create_tags")
        keys = {t["Key"] for t in Tags}
        for resource_id in Resources:
            resource = self.groups.get(resource_id) or self.acls[resource_id]
            resource["Tags"] = [t for t in resource["Tags"] if t["Key"] not in keys] + copy.deepcopy(Tags)

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": copy.deepcopy(list(self.groups.values()))}

    def create_security_group(self, GroupName, Description, VpcId, TagSpecifications):
        self._write("create_security_group")
        group_id = self._new_id("sg")
        self.groups[group_id] = {
            "GroupId": group_id, "GroupName": GroupName,
            "Tags": copy.deepcopy(TagSpecifications[0]["Tags"]),
            "IpPermissions": [],
            # AWS가 새 VPC SG에 넣는 기본 egress
            "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": _ALL}]}],
        }
        return {"GroupId": group_id}

    def authorize_security_group_ingress(self, GroupId, IpPermissions):
        self._write("authorize_security_group_ingress")
        rules = self.groups[GroupId]["IpPermissions"]
        if any(p in rules for p in IpPermissions):
            raise _client_error("InvalidPermission.Duplicate", "AuthorizeSecurityGroupIngress")
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
            a for a in self.acls.values()
            if names is None or next((t["Value"] for t in a["Tags"] if t["Key"] == "Name"), None) in names
        ]
        return {"NetworkAcls": copy.deepcopy(acls)}

    def create_network_acl(self, VpcId, TagSpecifications):
        self._write("create_network_acl")
        acl_id = self._new_id("acl")
        self.acls[acl_id] = {
            "NetworkAclId": acl_id, "IsDefault": False,
            "Tags": copy.deepcopy(TagSpecifications[0]["Tags"]),
            # 실 AWS의 커스텀 NACL은 deny-all(32767)만 갖고 태어난다
            "Entries": [
                {"RuleNumber": 32767, "Egress": egress, "RuleAction": "deny", "Protocol": "-1", "CidrBlock": _ALL}
                for egress in (False, True)
            ],
            "Associations": [],
        }
        return {"NetworkAcl": copy.deepcopy(self.acls[acl_id])}

    def create_network_acl_entry(self, NetworkAclId, RuleNumber, Protocol, RuleAction, Egress, CidrBlock):
        self._write("create_network_acl_entry")
        entries = self.acls[NetworkAclId]["Entries"]
        if any(e["RuleNumber"] == RuleNumber and e["Egress"] == Egress for e in entries):
            raise _client_error("NetworkAclEntryAlreadyExists", "CreateNetworkAclEntry")
        entries.append({
            "RuleNumber": RuleNumber, "Egress": Egress, "RuleAction": RuleAction,
            "Protocol": Protocol, "CidrBlock": CidrBlock,
        })

    def replace_network_acl_association(self, AssociationId, NetworkAclId):
        self._write("replace_network_acl_association")
        for acl in self.acls.values():
            for assoc in [a for a in acl["Associations"] if a["NetworkAclAssociationId"] == AssociationId]:
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
        self.tg = {"TargetGroupArn": f"arn:aws:elasticloadbalancing:{REGION}:123456789012:targetgroup/{Name}/1"}
        self.tags = copy.deepcopy(Tags)
        return {"TargetGroups": [dict(self.tg)]}

    def describe_tags(self, ResourceArns):
        return {"TagDescriptions": [{"ResourceArn": ResourceArns[0], "Tags": copy.deepcopy(self.tags)}]}

    def add_tags(self, ResourceArns, Tags):
        self._write("add_tags")
        keys = {t["Key"] for t in Tags}
        self.tags = [t for t in self.tags if t["Key"] not in keys] + copy.deepcopy(Tags)

    def describe_target_health(self, TargetGroupArn):
        return {"TargetHealthDescriptions": [
            {"Target": {"Id": target}, "TargetHealth": {"State": "healthy"}} for target in sorted(self.targets)
        ]}

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
    return smoke._ensure_load_balancer(elbv2, VPC, subnets, {smoke.SG_ALB: "sg-alb"}, INSTANCE_IDS)


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


def test_sg_left_pending_after_its_rule_landed_gets_no_duplicate_rule():
    """규칙은 들어갔는데 표지를 바꾸기 전에 끊긴 경우 — 다시 넣다 Duplicate로 멈추지 않는다."""
    ec2 = FakeEc2()
    ec2.faults["create_tags"] = [True]  # 첫 그룹(alb)의 규칙이 들어간 뒤 표지에서 끊긴다
    with pytest.raises(ClientError):
        smoke._ensure_security_groups(ec2, VPC)

    ids = smoke._ensure_security_groups(ec2, VPC)
    assert len(ec2.groups[ids[smoke.SG_ALB]]["IpPermissions"]) == 1
    assert all(_done(g) for g in ec2.groups.values())


def test_nacl_missing_an_allow_rule_is_completed_before_it_is_attached():
    """아웃바운드 허용이 빠진 NACL을 서브넷에 붙이면 서브넷 경계 통신이 끊긴다(#348 리뷰 재현)."""
    ec2 = FakeEc2()
    ec2.faults["create_network_acl_entry"] = [False, True]  # 인바운드는 들어가고 아웃바운드에서 끊긴다
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
    assert elbv2.targets == {INSTANCE_IDS[s.name] for s in smoke.INSTANCES if s.in_target_group}
    assert _done({"Tags": elbv2.tags})


def test_rerun_leaves_initialized_resources_as_the_smoke_left_them():
    """초기화가 끝난 자원은 스모크 도중 바뀐 그대로 둔다 — 격리·차단을 조용히 되돌리지 않는다."""
    ec2, elbv2 = FakeEc2(), FakeElbv2()
    ids = smoke._ensure_security_groups(ec2, VPC)
    acl_id = smoke._ensure_nacl(ec2, VPC, SUBNET)
    _ensure_tg(elbv2)
    # 스모크 도중의 변경 — 차단 규칙 추가(NACL_ADD_DENY) · web-1 등록 해제(EC2_ISOLATE) · 미끼 규칙 제거
    ec2.acls[acl_id]["Entries"].append(
        {"RuleNumber": 100, "Egress": False, "RuleAction": "deny", "Protocol": "-1", "CidrBlock": "198.51.100.0/24"}
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
