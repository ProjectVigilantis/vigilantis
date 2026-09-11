"""executor.execute_nacl_restore() 단위 테스트 (Issue #298).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **슬롯 대조 3분기와 단계 effect**를
본다. 삭제는 되돌릴 수 없으므로, 지우는 경로가 "우리 규칙임이 확인된 경우" 하나뿐인지가
이 파일의 요점이다(ADR-0008 §5).

LocalStack 실물 왕복(차단 → 해제)은 test_execute_nacl_localstack.py가 맡는다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.backups import NaclRuleIndexBackup  # noqa: E402
from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
ACL = "acl-0abc123456789def0"
ACL_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:network-acl/{ACL}"
RULE_NUMBER = 100
CIDR = "203.0.113.10/32"

DELETE_RESPONSE = {"ResponseMetadata": {"RequestId": "req-delete"}}


def client_error(code: str, status: int | None = 400) -> ClientError:
    metadata: dict = {"RequestId": "req-err"}
    if status is not None:
        metadata["HTTPStatusCode"] = status
    return ClientError(
        {"Error": {"Code": code, "Message": ""}, "ResponseMetadata": metadata},
        "Op",
    )


def our_entry(**overrides) -> dict:
    """차단이 넣은 그 규칙 — 아래 backup()과 fingerprint가 맞는다."""
    values = {
        "RuleNumber": RULE_NUMBER,
        "Egress": False,
        "CidrBlock": CIDR,
        "Protocol": "6",
        "RuleAction": "deny",
    }
    values.update(overrides)
    return values


def acl_with(*entries) -> dict:
    return {"NetworkAcls": [{"NetworkAclId": ACL, "Entries": list(entries)}]}


def backup(**overrides) -> NaclRuleIndexBackup:
    values = {
        "rule_number": RULE_NUMBER,
        "egress": False,
        "cidr_block": CIDR,
        "protocol": "6",
        "rule_action": "deny",
    }
    values.update(overrides)
    return NaclRuleIndexBackup(**values)


class FakeEc2:
    def __init__(self, state):
        self._state = state

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((operation, kwargs))
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if operation == "describe_network_acls":
                return acl_with(our_entry())
            return DELETE_RESPONSE

        return call


@pytest.fixture
def aws(monkeypatch):
    """기본은 우리 규칙이 슬롯에 있고 삭제가 성공하는 경로다."""
    state = {"overrides": {}, "calls": [], "clients": []}

    def factory(service, region=None, **_):
        state["clients"].append((service, region))
        return FakeEc2(state)

    monkeypatch.setattr(ex, "aws_client", factory)

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    configure.clients = state["clients"]
    return configure


def run(recorded=None, *, target_arn=ACL_ARN, expected=None):
    return ex.execute_nacl_restore(
        target_arn,
        backup=expected or backup(),
        record_step=None if recorded is None else recorded.append,
    )


def operations(aws):
    return [name for name, _ in aws.calls]


# ------------------------------------------------------------------ ② 우리 규칙 → 삭제


def test_deletes_the_rule_the_backup_points_at(aws):
    outcome = run()

    assert outcome.succeeded
    assert operations(aws) == ["describe_network_acls", "delete_network_acl_entry"]
    sent = aws.calls[1][1]
    assert sent == {"NetworkAclId": ACL, "RuleNumber": RULE_NUMBER, "Egress": False}


def test_the_delete_step_says_the_asset_changed(aws):
    """대조는 기록하지 않는다 — 삭제로 진행하는 경우 단계는 삭제 하나다."""
    recorded = []

    outcome = run(recorded)

    assert [(s.sequence, s.step_type, s.status, s.effect) for s in outcome.steps] == [
        (1, ex.STEP_DELETE_NACL_ENTRY, S.SUCCESS, E.APPLIED)
    ]
    assert outcome.steps[0].aws_operation == "ec2.delete_network_acl_entry"
    assert outcome.steps[0].aws_request_id == "req-delete"
    # 호출 직전 IN_PROGRESS가 먼저 저장돼야 회수가 "만져졌을 수 있다"를 알아본다
    assert [s.status for s in recorded] == [S.IN_PROGRESS, S.SUCCESS]


def test_uses_the_region_from_the_target_arn(aws):
    run()

    assert {region for _, region in aws.clients} == {REGION}


def test_an_outbound_slot_is_deleted_as_outbound(aws):
    """슬롯은 (rule_number, egress) 짝이다 — 같은 번호의 인바운드를 지우면 안 된다."""
    aws(describe_network_acls=acl_with(our_entry(Egress=True)))

    run(expected=backup(egress=True))

    assert aws.calls[1][1]["Egress"] is True


# ------------------------------------------------------------------ ① 이미 비었다


def test_an_empty_slot_is_already_released(aws):
    """관제자가 원한 상태가 이미 서 있다 — AWS 변경 없이 성공이고, 그 사실을 남긴다."""
    aws(describe_network_acls=acl_with())

    outcome = run()

    assert outcome.succeeded
    assert "delete_network_acl_entry" not in operations(aws)
    assert [(s.step_type, s.status, s.effect) for s in outcome.steps] == [
        (ex.STEP_COMPARE_NACL_ENTRY, S.SUCCESS, E.NOT_APPLIED)
    ]


# ------------------------------------------------------------------ ③ 제3자 규칙


@pytest.mark.parametrize(
    "differs",
    [
        {"CidrBlock": "10.0.0.0/8"},
        {"Protocol": "17"},
        {"RuleAction": "allow"},
    ],
)
def test_a_third_party_rule_is_never_deleted(aws, differs):
    """승인 대기 동안 우리 규칙이 지워지고 같은 번호에 남의 규칙이 들어올 수 있다.

    ④는 후보 생성 시점에 1회 돌았으므로 그 사이를 메우는 것이 이 대조다. 삭제는
    되돌릴 수 없다.
    """
    aws(describe_network_acls=acl_with(our_entry(**differs)))

    outcome = run()

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert "delete_network_acl_entry" not in operations(aws)
    # 자산을 바꾸지 않았다 — 되돌릴 것 없는 실패로 확정된다
    assert [(s.step_type, s.effect) for s in outcome.steps] == [
        (ex.STEP_COMPARE_NACL_ENTRY, E.NOT_APPLIED)
    ]


def test_the_other_direction_of_the_same_number_is_not_our_slot(aws):
    """아웃바운드 100번에 우리 것과 같은 규칙이 있어도 인바운드 100번이 비었으면 해제됐다."""
    aws(describe_network_acls=acl_with(our_entry(Egress=True)))

    outcome = run()

    assert outcome.succeeded
    assert "delete_network_acl_entry" not in operations(aws)


# ------------------------------------------------------------------ 대조 불가·대상 없음


def test_a_failed_read_defers_without_touching_the_asset(aws):
    """조회를 못 했으면 판정 근거가 없다 — 실패가 아니라 보류이고, 단계를 남기지 않는다."""
    aws(describe_network_acls=client_error("RequestLimitExceeded", status=503))

    outcome = run()

    assert outcome.deferred
    assert outcome.steps == ()
    assert "delete_network_acl_entry" not in operations(aws)


def test_a_missing_nacl_is_a_settled_failure(aws):
    """NACL이 없으면 지울 대상이 없다 — 다시 물어도 답이 같으므로 보류하지 않는다."""
    aws(describe_network_acls={"NetworkAcls": []})

    outcome = run()

    assert not outcome.succeeded and not outcome.deferred
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND
    assert outcome.steps == ()


@pytest.mark.parametrize(
    "arn",
    [f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc123456789def0", "acl-0abc", ""],
)
def test_rejects_a_target_that_is_not_a_nacl(aws, arn):
    outcome = run(target_arn=arn)

    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


# ------------------------------------------------------------------ 삭제 실패


def test_a_rule_gone_between_compare_and_delete_is_change_free(aws):
    """대조와 삭제 사이에 규칙이 사라지면 4xx로 온다 — 되돌릴 것 없는 실패다."""
    aws(delete_network_acl_entry=client_error("InvalidNetworkAclEntry.NotFound"))

    outcome = run()

    assert not outcome.succeeded
    assert outcome.steps[0].status is S.FAILED
    assert outcome.steps[0].effect is E.NOT_APPLIED


@pytest.mark.parametrize(
    "error",
    [
        client_error("InternalError", status=500),
        EndpointConnectionError(endpoint_url="https://ec2"),
    ],
)
def test_an_unknown_delete_outcome_is_not_written_optimistically(aws, error):
    """적용 여부를 모르면 UNKNOWN이다 — 다음 주기의 현물 판정(judge_nacl_restore)이 답한다."""
    aws(delete_network_acl_entry=error)

    outcome = run()

    assert not outcome.succeeded
    assert outcome.steps[0].effect is E.UNKNOWN
