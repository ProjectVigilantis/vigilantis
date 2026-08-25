"""executor.precheck() LocalStack 통합 테스트 (Issue #129, ADR-0007).

가짜 클라이언트가 아니라 실제 AWS 응답으로 판정을 확인한다. 판정 분기 전수는
test_precheck_dispatch.py가 맡고, 여기서는 "실물에서 실제로 그렇게 되는가"만 본다.

로컬 실행 전제: LocalStack 기동 + scripts/seed_localstack.py 완료. 미기동 시 전체 skip.
"""

import os
import sys
import urllib.request
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
os.environ.setdefault("AWS_ENDPOINT_URL", ENDPOINT)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws.client import account_id, aws_client, default_region  # noqa: E402

R = PrecheckReasonCode

# 이 테스트가 만들고 지우는 NACL 규칙 번호 — 시드 자산과 겹치지 않는 값
PROBE_RULE = 31337


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)


@pytest.fixture(scope="module")
def fx():
    ec2 = aws_client("ec2")
    region, account = default_region(), account_id()

    def arn(resource_type, resource_id):
        return f"arn:aws:ec2:{region}:{account}:{resource_type}/{resource_id}"

    instance = next(
        i
        for r in ec2.describe_instances()["Reservations"]
        for i in r["Instances"]
        if i["State"]["Name"] == "running"
    )
    group = next(
        g
        for g in ec2.describe_security_groups()["SecurityGroups"]
        if g["GroupName"].startswith("vigilantis-seed-unused")
    )["GroupId"]
    volume = ec2.describe_volumes(
        Filters=[{"Name": "status", "Values": ["available"]}]
    )["Volumes"][0]["VolumeId"]
    acl = ec2.describe_network_acls()["NetworkAcls"][0]["NetworkAclId"]
    return {
        "ec2": ec2,
        "arn": arn,
        "instance": instance,
        "group": group,
        "volume": volume,
        "acl": acl,
        "tg_arn": (
            f"arn:aws:elasticloadbalancing:{region}:{account}:targetgroup/vigilantis/abc"
        ),
    }


class Loader:
    def __init__(self, record):
        self.record = record

    def get(self, backup_record_id):
        return self.record if self.record.backup_record_id == backup_record_id else None

    def latest_for_target(self, target_arn, backup_type, payload_match=None):
        record = self.record
        if record.target_arn != target_arn or record.backup_type != backup_type:
            return None
        if payload_match and any(
            record.payload.get(key) != value for key, value in payload_match.items()
        ):
            return None
        return record


def backup(target_arn, backup_type, payload):
    return Loader(ex.BackupRecordView("bk-1", target_arn, backup_type, payload))


def add_probe_rule(ec2, acl):
    ec2.create_network_acl_entry(
        NetworkAclId=acl,
        RuleNumber=PROBE_RULE,
        Protocol="-1",
        RuleAction="deny",
        Egress=False,
        CidrBlock="203.0.113.5/32",
    )


# ------------------------------------------------------------------ P0 4종
# 9/13 게이트 시연 경로 — 로컬에서 통과 경로가 있어야 한다(ADR-0007 §Consequences).


def test_rightsizing_passes(fx):
    instance = fx["instance"]
    outcome = ex.precheck(
        "RUNBOOK_EC2_RIGHTSIZING",
        fx["arn"]("instance", instance["InstanceId"]),
        {
            "instance_id": instance["InstanceId"],
            "current_instance_type": instance["InstanceType"],
            "target_instance_type": "t3.micro",
            "evidence_id": "ev-1",
        },
    )
    assert outcome.passed, outcome.verification_summary
    assert outcome.verification_summary.startswith("DRY_RUN(ec2.modify_instance_attribute)")


def test_revert_size_passes_with_the_backup_record(fx):
    instance = fx["instance"]
    target_arn = fx["arn"]("instance", instance["InstanceId"])
    outcome = ex.precheck(
        "RUNBOOK_EC2_REVERT_SIZE",
        target_arn,
        {
            "instance_id": instance["InstanceId"],
            "backup_record_id": "bk-1",
            "evidence_id": "ev-1",
        },
        backup_loader=backup(
            target_arn,
            ex.BACKUP_INSTANCE_SPEC,
            {"instance_type": instance["InstanceType"]},
        ),
    )
    assert outcome.passed, outcome.verification_summary


def test_nacl_add_deny_passes_and_creates_nothing(fx):
    """조회 대체 경로가 실제로 규칙을 만들지 않는지 — ADR-0006 §4 5행 재발 방지."""
    ec2, acl = fx["ec2"], fx["acl"]
    before = ec2.describe_network_acls(NetworkAclIds=[acl])["NetworkAcls"][0]["Entries"]

    outcome = ex.precheck(
        "RUNBOOK_NACL_ADD_DENY",
        fx["arn"]("network-acl", acl),
        {
            "network_acl_id": acl,
            "rule_number": PROBE_RULE,
            "cidr_block": "203.0.113.5/32",
            "protocol": "-1",
            "evidence_id": "ev-1",
        },
    )
    after = ec2.describe_network_acls(NetworkAclIds=[acl])["NetworkAcls"][0]["Entries"]

    assert outcome.passed, outcome.verification_summary
    assert outcome.verification_summary.startswith("DESCRIBE(ec2.describe_network_acls)")
    assert len(after) == len(before), "확인 단계가 규칙을 실제로 만들었습니다"


def test_nacl_restore_passes_and_deletes_nothing(fx):
    ec2, acl = fx["ec2"], fx["acl"]
    target_arn = fx["arn"]("network-acl", acl)
    add_probe_rule(ec2, acl)
    try:
        outcome = ex.precheck(
            "RUNBOOK_NACL_RESTORE",
            target_arn,
            {
                "network_acl_id": acl,
                "rule_number": PROBE_RULE,
                "egress": False,
                "evidence_id": "ev-1",
            },
            backup_loader=backup(
                target_arn,
                ex.BACKUP_NACL_RULE_INDEX,
                {"rule_number": PROBE_RULE, "egress": False},
            ),
        )
        entries = ec2.describe_network_acls(NetworkAclIds=[acl])["NetworkAcls"][0]["Entries"]
        assert outcome.passed, outcome.verification_summary
        assert any(
            e["RuleNumber"] == PROBE_RULE and not e["Egress"] for e in entries
        ), "확인 단계가 규칙을 실제로 지웠습니다"
    finally:
        ec2.delete_network_acl_entry(NetworkAclId=acl, RuleNumber=PROBE_RULE, Egress=False)


def test_nacl_add_deny_rejects_a_number_already_in_use(fx):
    ec2, acl = fx["ec2"], fx["acl"]
    add_probe_rule(ec2, acl)
    try:
        outcome = ex.precheck(
            "RUNBOOK_NACL_ADD_DENY",
            fx["arn"]("network-acl", acl),
            {
                "network_acl_id": acl,
                "rule_number": PROBE_RULE,
                "cidr_block": "203.0.113.5/32",
                "protocol": "-1",
                "evidence_id": "ev-1",
            },
        )
        assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_INVALID_STATE)
    finally:
        ec2.delete_network_acl_entry(NetworkAclId=acl, RuleNumber=PROBE_RULE, Egress=False)


# ------------------------------------------------------------------ P1 3종
def test_sg_delete_passes(fx):
    group = fx["group"]
    outcome = ex.precheck(
        "RUNBOOK_SG_DELETE_ISOLATED",
        fx["arn"]("security-group", group),
        {"group_id": group, "evidence_id": "ev-1"},
    )
    assert outcome.passed, outcome.verification_summary


def test_ebs_delete_passes(fx):
    volume = fx["volume"]
    outcome = ex.precheck(
        "RUNBOOK_EBS_DELETE_UNATTACHED",
        fx["arn"]("volume", volume),
        {"volume_id": volume, "evidence_id": "ev-1"},
    )
    assert outcome.passed, outcome.verification_summary


def test_sg_recreate_passes_with_the_backup_record(fx):
    group = fx["group"]
    target_arn = fx["arn"]("security-group", group)
    outcome = ex.precheck(
        "RUNBOOK_SG_RECREATE",
        target_arn,
        {"backup_record_id": "bk-1", "evidence_id": "ev-1"},
        backup_loader=backup(
            target_arn,
            ex.BACKUP_SG_FULL_RULES,
            {
                "group_name": "vigilantis-restored",
                "description": "restored by vigilantis",
                "vpc_id": fx["instance"]["VpcId"],
                "ingress_permissions": [],
                "egress_permissions": [],
            },
        ),
    )
    assert outcome.passed, outcome.verification_summary


# ------------------------------------------------------------------ P2 3종
# elbv2·autoscaling은 Community에 없다(ADR-0006 §4 4행). 여기서의 실패는 결함이
# 아니라 ADR-0006 §3(코드 분기 금지)을 지킨 결과다 — 통과 조건 확정은 실 AWS 스모크.


def test_isolate_fails_locally_on_the_pro_only_service(fx):
    instance = fx["instance"]
    outcome = ex.precheck(
        "RUNBOOK_EC2_ISOLATE",
        fx["arn"]("instance", instance["InstanceId"]),
        {
            "instance_id": instance["InstanceId"],
            "target_group_arn": fx["tg_arn"],
            "isolation_group_id": fx["group"],
            "evidence_id": "ev-1",
        },
    )
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_AWS_ERROR)
    # 앞 단계(ENI 교체 DryRun·격리용 SG 조회)까지는 실제로 통과했다는 기록이 남아야 한다
    assert "ENI 교체 DryRun 통과" in outcome.verification_summary


def test_unisolate_fails_locally_on_the_pro_only_service(fx):
    instance = fx["instance"]
    target_arn = fx["arn"]("instance", instance["InstanceId"])
    outcome = ex.precheck(
        "RUNBOOK_EC2_UNISOLATE",
        target_arn,
        {
            "instance_id": instance["InstanceId"],
            "backup_record_id": "bk-1",
            "evidence_id": "ev-1",
        },
        backup_loader=backup(
            target_arn,
            ex.BACKUP_SG_AND_TG_MAPPING,
            {"security_group_ids": [fx["group"]], "target_group_arn": fx["tg_arn"]},
        ),
    )
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_AWS_ERROR)
    assert "복원 대상 SG 현존" in outcome.verification_summary


def test_enable_autoscaling_fails_locally_on_the_pro_only_service(fx):
    instance = fx["instance"]
    outcome = ex.precheck(
        "RUNBOOK_EC2_ENABLE_AUTOSCALING",
        fx["arn"]("instance", instance["InstanceId"]),
        {
            "instance_id": instance["InstanceId"],
            "min_size": 1,
            "max_size": 4,
            "evidence_id": "ev-1",
        },
    )
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_AWS_ERROR)
    assert "Launch Template DryRun 통과" in outcome.verification_summary
