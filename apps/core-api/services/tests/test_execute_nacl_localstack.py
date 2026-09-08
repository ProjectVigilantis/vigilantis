"""NACL_ADD_DENY LocalStack 통합 테스트 (Issue #297, ADR-0006).

단계 분기 전수는 test_execute_nacl_add_deny.py가 맡고, 여기서는 **실물에서 실제로
규칙이 들어가고, 저장된 백업 fingerprint가 그 규칙과 맞는가**만 본다.

이 파일이 필요한 이유가 NACL에는 특히 크다. 이 자원의 두 쓰기 API는 LocalStack이
`DryRun=True`를 무시하고 실제로 수행해 버리는 바로 그 경로이고(ADR-0006 §4 5행),
`Protocol` 값도 보낸 문자열을 그대로 저장한다(2026-09-08 실측). 그래서 "호출은
받았는데 우리가 기대한 모양으로 남지 않는" 어긋남이 실제로 가능하다 — 조용히
통과하면 게이트 시연에서 처음 발견된다.

시드 자산을 쓰지 않고 **이 파일이 VPC·NACL을 직접 만들고 지운다.** 규칙 삽입은
슬롯을 점유하는 조치라, 공용 자원에 남기면 다음 실행이 NetworkAclEntryAlreadyExists로
깨진다(probe_dryrun.py가 파괴적 작업에 자기 자원을 쓰는 것과 같은 이유).

로컬 실행 전제: LocalStack 기동. 미기동 시 전체 skip.
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

from schemas.backups import BackupType, NaclRuleIndexBackup  # noqa: E402
from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws.client import account_id, aws_client, default_region  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

RULE_NUMBER = 100
CIDR = "198.51.100.0/24"


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)


@pytest.fixture
def nacl():
    """빈 NACL 1개를 만들어 쓰고 VPC까지 지운다. (network_acl_id, target_arn) 짝."""
    ec2 = aws_client("ec2")
    vpc_id = ec2.create_vpc(CidrBlock="10.97.0.0/16")["Vpc"]["VpcId"]
    acl_id = ec2.create_network_acl(VpcId=vpc_id)["NetworkAcl"]["NetworkAclId"]
    arn = f"arn:aws:ec2:{default_region()}:{account_id()}:network-acl/{acl_id}"
    try:
        yield acl_id, arn
    finally:
        ec2.delete_network_acl(NetworkAclId=acl_id)
        ec2.delete_vpc(VpcId=vpc_id)


def _inbound_entry(acl_id: str, rule_number: int):
    acl = aws_client("ec2").describe_network_acls(NetworkAclIds=[acl_id])["NetworkAcls"][0]
    for entry in acl["Entries"]:
        if entry["RuleNumber"] == rule_number and not entry["Egress"]:
            return entry
    return None


def test_the_rule_actually_lands_in_the_nacl(nacl):
    """에뮬레이터가 호출을 받아 놓고 아무것도 바꾸지 않는 경우가 실제로 있었다."""
    acl_id, arn = nacl

    outcome = ex.execute_nacl_add_deny(
        arn, rule_number=RULE_NUMBER, cidr_block=CIDR, protocol="tcp"
    )

    assert outcome.succeeded
    entry = _inbound_entry(acl_id, RULE_NUMBER)
    assert entry is not None
    assert entry["RuleAction"] == "deny"
    assert entry["CidrBlock"] == CIDR


def test_the_stored_protocol_is_the_aws_number(nacl):
    """이름을 그대로 보내면 LocalStack은 "tcp"를 저장하고 실 AWS는 "6"을 저장한다.

    저장 값이 곧 백업 fingerprint의 대조 상대라 환경마다 갈리면 NACL_RESTORE가
    한쪽에서만 규칙을 찾는다. 번호로 보내기로 한 결정이 실물에서 지켜지는지 본다.
    """
    acl_id, arn = nacl

    ex.execute_nacl_add_deny(arn, rule_number=RULE_NUMBER, cidr_block=CIDR, protocol="tcp")

    assert _inbound_entry(acl_id, RULE_NUMBER)["Protocol"] == "6"


def test_the_step_records_the_change(nacl):
    _, arn = nacl
    steps = []

    outcome = ex.execute_nacl_add_deny(
        arn,
        rule_number=RULE_NUMBER,
        cidr_block=CIDR,
        protocol="tcp",
        record_step=steps.append,
    )

    assert [(s.sequence, s.status, s.effect) for s in outcome.steps] == [
        (1, S.SUCCESS, E.APPLIED)
    ]
    assert [s.status for s in steps] == [S.IN_PROGRESS, S.SUCCESS]


def test_capture_then_execute_produces_a_matching_fingerprint(nacl):
    """백업이 실물 엔트리와 맞아야 NACL_RESTORE가 그 규칙을 우리 것으로 알아본다.

    단위 테스트는 우리가 만든 가짜 응답끼리 맞춰 볼 뿐이라, 대조가 실물에서도
    성립하는지는 여기서만 확인된다.
    """
    acl_id, arn = nacl

    capture = bk.capture_nacl_rule_index(
        acl_id, default_region(), rule_number=RULE_NUMBER, cidr_block=CIDR, protocol="tcp"
    )
    assert capture.captured
    assert capture.backup_type == BackupType.RECORD_NACL_RULE_INDEX.value

    ex.execute_nacl_add_deny(arn, rule_number=RULE_NUMBER, cidr_block=CIDR, protocol="tcp")

    stored = NaclRuleIndexBackup.model_validate(capture.payload)
    entry = _inbound_entry(acl_id, RULE_NUMBER)
    assert ex.nacl_entry_fingerprint_matches(entry, stored)


def test_capture_refuses_a_slot_that_is_already_used(nacl):
    """조치 직전 확인이 실물에서도 걸리는가 — 남의 규칙을 가리키는 백업을 막는 관문이다."""
    acl_id, arn = nacl
    ex.execute_nacl_add_deny(arn, rule_number=RULE_NUMBER, cidr_block=CIDR, protocol="tcp")

    capture = bk.capture_nacl_rule_index(
        acl_id,
        default_region(),
        rule_number=RULE_NUMBER,
        cidr_block="203.0.113.0/24",
        protocol="udp",
    )

    assert not capture.captured
    assert capture.reason_code is R.PRECHECK_INVALID_STATE


def test_a_taken_slot_fails_without_changing_the_asset(nacl):
    """실행 직전에 슬롯이 점유되면 AWS가 거절한다 — 되돌릴 것 없는 실패여야 한다.

    실측(2026-09-08): NetworkAclEntryAlreadyExists(4xx). effect가 UNKNOWN으로
    적히면 자동 원복이 아무것도 바꾸지 않은 실행을 되돌리러 간다.
    """
    _, arn = nacl
    ex.execute_nacl_add_deny(arn, rule_number=RULE_NUMBER, cidr_block=CIDR, protocol="tcp")

    outcome = ex.execute_nacl_add_deny(
        arn, rule_number=RULE_NUMBER, cidr_block="203.0.113.0/24", protocol="udp"
    )

    assert not outcome.succeeded
    assert outcome.steps[0].effect is E.NOT_APPLIED


def test_the_judge_read_sees_the_same_slot(nacl):
    """실행과 종료 판정이 같은 축을 같은 방법으로 읽는가(current_nacl_entry)."""
    acl_id, arn = nacl

    before, code = ex.current_nacl_entry(
        acl_id, default_region(), rule_number=RULE_NUMBER, egress=False
    )
    assert (before, code) == (None, None)

    ex.execute_nacl_add_deny(arn, rule_number=RULE_NUMBER, cidr_block=CIDR, protocol="tcp")

    after, code = ex.current_nacl_entry(
        acl_id, default_region(), rule_number=RULE_NUMBER, egress=False
    )
    assert code is None
    assert after["CidrBlock"] == CIDR


def test_a_missing_nacl_reads_as_target_not_found():
    """슬롯이 빈 것과 NACL이 없는 것은 다른 사건이다 — 판정이 둘을 섞으면 안 된다."""
    entry, code = ex.current_nacl_entry(
        "acl-00000000000000000", default_region(), rule_number=RULE_NUMBER, egress=False
    )

    assert entry is None
    assert code is R.PRECHECK_TARGET_NOT_FOUND
