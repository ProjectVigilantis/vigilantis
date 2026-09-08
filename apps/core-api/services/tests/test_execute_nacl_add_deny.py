"""executor.execute_nacl_add_deny() 단위 테스트 (Issue #297).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **보내는 값과 단계 effect**를
본다. 두 축 모두 뒤에서 되돌릴 때 쓰이는 것이라 여기서 굳혀 둔다.

  - 보내는 Protocol이 **번호 표기**여야 한다. 이름을 그대로 보내면 실 AWS는 번호로
    정규화하고 LocalStack은 문자열을 그대로 저장해(2026-09-08 실측), 백업
    fingerprint 대조가 한쪽 환경에서만 맞는다.
  - effect는 자동 원복이 "자산이 실제로 바뀌었는가"를 판단하는 유일한 입력이다.

LocalStack 실물 검증은 test_execute_nacl_localstack.py가 맡는다.
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
CIDR = "198.51.100.0/24"

CREATE_RESPONSE = {"ResponseMetadata": {"RequestId": "req-create"}}


def client_error(code: str, status: int | None = 400) -> ClientError:
    metadata: dict = {"RequestId": "req-err"}
    if status is not None:
        metadata["HTTPStatusCode"] = status
    return ClientError(
        {"Error": {"Code": code, "Message": ""}, "ResponseMetadata": metadata},
        "CreateNetworkAclEntry",
    )


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
            return CREATE_RESPONSE

        return call


@pytest.fixture
def aws(monkeypatch):
    """기본은 삽입 성공. configure(...)로 실패시킬 호출만 바꾼다."""
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


@pytest.fixture
def recorded():
    steps = []
    return steps


def run(recorded=None, *, target_arn=ACL_ARN, protocol="tcp", rule_number=RULE_NUMBER):
    return ex.execute_nacl_add_deny(
        target_arn,
        rule_number=rule_number,
        cidr_block=CIDR,
        protocol=protocol,
        record_step=None if recorded is None else recorded.append,
    )


# ------------------------------------------------------------------ 성공 경로


def test_inserts_one_inbound_deny_rule(aws):
    outcome = run()

    assert outcome.succeeded
    assert [name for name, _ in aws.calls] == ["create_network_acl_entry"]
    sent = aws.calls[0][1]
    assert sent["NetworkAclId"] == ACL
    assert sent["RuleNumber"] == RULE_NUMBER
    assert sent["CidrBlock"] == CIDR
    assert sent["RuleAction"] == "deny"
    # ADD_DENY는 인바운드 차단 규칙이다 — 파라미터 표에 egress가 없다(ADR-0007 §5)
    assert sent["Egress"] is False


@pytest.mark.parametrize(
    ("protocol", "expected"),
    [("tcp", "6"), ("udp", "17"), ("icmp", "1"), ("-1", "-1")],
)
def test_sends_the_protocol_as_an_aws_number(aws, protocol, expected):
    """이름이 아니라 번호를 보낸다.

    실 AWS는 이름을 번호로 정규화하지만 LocalStack은 보낸 문자열을 그대로 저장한다
    (2026-09-08 실측). 이름을 보내면 저장 값이 환경마다 갈리고, 그 값이 곧 백업
    fingerprint의 대조 상대라 NACL_RESTORE가 한쪽 환경에서만 규칙을 찾게 된다.
    """
    run(protocol=protocol)

    assert aws.calls[0][1]["Protocol"] == expected


def test_success_step_says_the_asset_changed(aws, recorded):
    """effect가 자동 원복의 유일한 입력이다 — 규칙이 들어갔으면 APPLIED다."""
    outcome = run(recorded)

    assert len(outcome.steps) == 1
    step = outcome.steps[0]
    assert (step.sequence, step.status, step.effect) == (1, S.SUCCESS, E.APPLIED)
    assert step.step_type == ex.STEP_CREATE_NACL_ENTRY
    assert step.aws_operation == "ec2.create_network_acl_entry"
    assert step.aws_request_id == "req-create"


def test_records_in_progress_before_the_aws_call(aws, recorded):
    """호출 직전 IN_PROGRESS가 먼저 저장돼야 회수(dispatcher)가 "자산이 만져졌을 수
    있다"를 알아본다 — 단계 0건은 재실행 대상이라는 규약이 여기 걸려 있다."""
    run(recorded)

    assert [s.status for s in recorded] == [S.IN_PROGRESS, S.SUCCESS]
    assert recorded[0].effect is None


def test_uses_the_region_from_the_target_arn(aws):
    run()

    assert aws.clients == [("ec2", REGION)]


# ------------------------------------------------------------------ 거절 (AWS 미호출)


@pytest.mark.parametrize(
    "arn",
    [
        f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc123456789def0",
        "acl-0abc123456789def0",
        "",
    ],
)
def test_rejects_a_target_that_is_not_a_nacl(aws, arn):
    outcome = run(target_arn=arn)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    # AWS를 부르지 않았으므로 되돌릴 것이 없다 — 단계도 남지 않는다
    assert outcome.steps == ()
    assert aws.calls == []


def test_rejects_an_unknown_protocol_spelling(aws):
    """계약(NaclProtocol)이 이미 거르는 값이지만, 여기서도 AWS에 닿기 전에 끝낸다."""
    outcome = run(protocol="TCP")

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


# ------------------------------------------------------------------ 실패 경로


def test_slot_taken_is_a_change_free_failure(aws, recorded):
    """규칙 번호가 그사이 점유되면 NetworkAclEntryAlreadyExists(4xx)로 온다(실측).

    되돌릴 것이 없는 실패라 NOT_APPLIED여야 한다 — UNKNOWN으로 적으면 자동 원복이
    아무것도 바꾸지 않은 실행을 되돌리러 간다.
    """
    aws(create_network_acl_entry=client_error("NetworkAclEntryAlreadyExists"))

    outcome = run(recorded)

    assert not outcome.succeeded
    assert outcome.steps[0].status is S.FAILED
    assert outcome.steps[0].effect is E.NOT_APPLIED


def test_unreachable_aws_leaves_the_effect_unknown(aws, recorded):
    """AWS에 닿지 못한 실패는 삽입 여부를 알 수 없다 — 낙관적으로 적지 않는다."""
    aws(create_network_acl_entry=EndpointConnectionError(endpoint_url="https://ec2"))

    outcome = run(recorded)

    assert not outcome.succeeded
    assert outcome.steps[0].effect is E.UNKNOWN


def test_server_error_leaves_the_effect_unknown(aws, recorded):
    aws(create_network_acl_entry=client_error("InternalError", status=500))

    outcome = run(recorded)

    assert outcome.steps[0].effect is E.UNKNOWN


# ------------------------------------------------- fingerprint 대조 (ADR-0008 §5)


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


def entry(**overrides) -> dict:
    values = {
        "RuleNumber": RULE_NUMBER,
        "Egress": False,
        "CidrBlock": CIDR,
        "Protocol": "6",
        "RuleAction": "deny",
    }
    values.update(overrides)
    return values


def test_fingerprint_matches_our_own_rule():
    assert ex.nacl_entry_fingerprint_matches(entry(), backup())


@pytest.mark.parametrize(
    "differs",
    [
        {"CidrBlock": "203.0.113.0/24"},
        {"Protocol": "17"},
        {"RuleAction": "allow"},
    ],
)
def test_fingerprint_rejects_a_third_party_rule(differs):
    """슬롯 번호는 재사용된다 — 3항목 중 하나라도 다르면 우리 규칙이 아니다.

    삭제는 되돌릴 수 없으므로 이 대조가 NACL_RESTORE의 통과 조건이다.
    """
    assert not ex.nacl_entry_fingerprint_matches(entry(**differs), backup())


def test_fingerprint_rejects_a_name_spelled_protocol():
    """백업이 이름 표기로 저장돼 있으면 실 AWS 엔트리("6")와 영영 맞지 않는다.

    번호로 저장하기로 한 결정이 무너지면 이 테스트가 먼저 깨진다.
    """
    assert not ex.nacl_entry_fingerprint_matches(entry(Protocol="tcp"), backup())
