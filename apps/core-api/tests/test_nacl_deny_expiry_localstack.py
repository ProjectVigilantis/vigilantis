"""차단 24시간 만료 → 실제 LocalStack NACL에서 규칙이 사라지는가. (Issue #597 · T-11)

가짜 EC2가 아니라 LocalStack의 NACL에 deny 엔트리를 넣고, 25시간 전에 끝난 SUCCESS 차단 행 +
그 규칙을 가리키는 백업 레코드를 시드한 뒤 dispatcher 한 주기로 해제 SUCCESS와 엔트리 삭제를 본다.

로컬 실행 전제: LocalStack 기동. 미기동 시 전체 skip.
"""

import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
os.environ.setdefault("AWS_ENDPOINT_URL", ENDPOINT)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import dispatcher  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws.client import account_id, aws_client, default_region  # noqa: E402


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)

CIDR = "203.0.113.10/32"
RULE_NUMBER = 90  # 기본 NACL의 100번(전체 허용)과 겹치지 않는 슬롯


@pytest.fixture()
def nacl():
    ec2 = aws_client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.97.0.0/16")["Vpc"]["VpcId"]
    acl_id = ec2.describe_network_acls(Filters=[{"Name": "vpc-id", "Values": [vpc]}])[
        "NetworkAcls"
    ][0]["NetworkAclId"]
    ec2.create_network_acl_entry(
        NetworkAclId=acl_id,
        RuleNumber=RULE_NUMBER,
        Protocol="6",
        RuleAction="deny",
        Egress=False,
        CidrBlock=CIDR,
    )
    yield ec2, acl_id
    ec2.delete_vpc(VpcId=vpc)


def _rule_numbers(ec2, acl_id):
    [acl] = ec2.describe_network_acls(NetworkAclIds=[acl_id])["NetworkAcls"]
    return {e["RuleNumber"] for e in acl["Entries"] if not e["Egress"]}


def test_an_expired_deny_entry_is_removed_from_localstack(
    db, nacl, make_incident, make_execution
):
    ec2, acl_id = nacl
    arn = f"arn:aws:ec2:{default_region()}:{account_id()}:network-acl/{acl_id}"
    assert RULE_NUMBER in _rule_numbers(ec2, acl_id)
    incident = make_incident(
        db, category=IncidentCategory.SECOPS, subject_arn=arn, status=IncidentStatus.RESOLVED
    )
    incident.resolution = None
    block = make_execution(
        db,
        incident,
        runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
        target_arn=arn,
        status=ExecutionStatus.SUCCESS,
    )
    record = exec_repo.create_backup_record(
        db,
        execution_id=block.execution_id,
        target_arn=arn,
        backup_type=ex.BACKUP_NACL_RULE_INDEX,
        payload={
            "rule_number": RULE_NUMBER,
            "egress": False,
            "cidr_block": CIDR,
            "protocol": "6",
            "rule_action": "deny",
        },
    )
    assert exec_repo.bind_backup_record(db, block.execution_id, record.backup_record_id)
    block.finished_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db.commit()

    report = dispatcher.dispatch_pending(db)

    assert report.expiry_reserved == 1
    [restore] = [
        r
        for r in exec_repo.list_by_incident(db, incident.incident_id)
        if r.runbook_id is RunbookId.RUNBOOK_NACL_RESTORE
    ]
    assert restore.trigger_source is TriggerSource.NACL_DENY_EXPIRED_24H
    assert restore.status is ExecutionStatus.SUCCESS
    assert RULE_NUMBER not in _rule_numbers(ec2, acl_id)
