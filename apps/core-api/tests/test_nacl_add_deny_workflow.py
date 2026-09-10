"""NACL_ADD_DENY 실행 워크플로 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip).

AWS 호출 분기는 services/tests/test_execute_nacl_add_deny.py가 맡고, 여기서는
**순서와 기록과 판정**을 본다.

  - 백업이 commit된 뒤에만 규칙 삽입이 시작되는가 (ADR-0008 §1)
  - 같은 실행에 두 번 불러도 레코드가 하나인가 (§1 ③)
  - 끊긴 실행의 종료 판정이 **실자산**을 보고, 남의 규칙을 우리 것으로 세지 않는가 (§5)

이 셋이 어긋나면 NACL_RESTORE가 되돌릴 근거를 잃거나, 더 나쁘게는 제3자 규칙을
지운다 — 삭제는 되돌릴 수 없다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import workflows  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.backups import BackupType  # noqa: E402
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import RunbookId  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
ACL = "acl-0abc123456789def0"
ACL_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:network-acl/{ACL}"
RULE_NUMBER = 100
CIDR = "203.0.113.5/32"
CANDIDATE_PARAMS = {"rule_number": RULE_NUMBER, "cidr_block": CIDR, "protocol": "tcp"}

EMPTY_ACL = {"NetworkAcls": [{"NetworkAclId": ACL, "Entries": []}]}


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Op",
    )


def our_entry(**overrides) -> dict:
    """조치가 넣은 그 규칙 — 백업 fingerprint와 일치하는 모양이다."""
    values = {
        "RuleNumber": RULE_NUMBER,
        "Egress": False,
        "CidrBlock": CIDR,
        "Protocol": "6",
        "RuleAction": "deny",
    }
    values.update(overrides)
    return values


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
                return EMPTY_ACL
            return {}

        return call


@pytest.fixture
def aws(monkeypatch):
    """캡처(backup)와 실행·판정(executor)이 같은 가짜 EC2를 본다 — 호출 순서를 한 줄로 읽는다."""
    state = {"overrides": {}, "calls": []}

    def factory(service, region=None, **_):
        return FakeEc2(state)

    monkeypatch.setattr(bk, "aws_client", factory)
    monkeypatch.setattr(ex, "aws_client", factory)

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    return configure


@pytest.fixture()
def reserved_execution(db, make_incident, make_candidate, make_execution):
    """차단 실행 1건 — SECOPS Incident + (선택) CLAIMED 후보 위에 선다."""

    def _make(*, with_candidate=True, parameters=None, **kwargs):
        incident = make_incident(
            db,
            category=IncidentCategory.SECOPS,
            subject_arn=ACL_ARN,
            status=IncidentStatus.ANALYZING,
        )
        candidate = None
        if with_candidate:
            candidate = make_candidate(
                db,
                incident,
                runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
                target_arn=ACL_ARN,
                parameters=CANDIDATE_PARAMS if parameters is None else parameters,
                status=CandidateStatus.CLAIMED,
            )
        return make_execution(
            db,
            incident,
            runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
            target_arn=ACL_ARN,
            candidate=candidate,
            **kwargs,
        )

    return _make


def operations(aws):
    return [name for name, _ in aws.calls]


def run(db, execution):
    return workflows.run_nacl_add_deny_execution(db, execution.execution_id)


# ------------------------------------------------------------------ 성공 경로


def test_backup_is_committed_before_any_change(db, reserved_execution, aws):
    """규칙을 넣고 나면 그것이 우리 것인지 말해 줄 근거가 없다(ADR-0004 정책 ③)."""
    execution = reserved_execution()

    outcome = run(db, execution)

    assert outcome.succeeded
    names = operations(aws)
    assert names.index("describe_network_acls") < names.index("create_network_acl_entry")
    assert execution.backup_record_id is not None


def test_backup_records_the_slot_and_the_fingerprint(db, reserved_execution, aws):
    execution = reserved_execution()

    run(db, execution)

    record = exec_repo.get_backup_record(db, execution.backup_record_id)
    assert record.backup_type == BackupType.RECORD_NACL_RULE_INDEX.value
    assert record.target_arn == ACL_ARN
    assert record.payload == {
        "rule_number": RULE_NUMBER,
        "egress": False,
        "cidr_block": CIDR,
        # 이름이 아니라 AWS 번호로 저장한다 — 대조 상대가 describe의 Protocol이다
        "protocol": "6",
        "rule_action": "deny",
    }


def test_the_rule_that_goes_in_is_the_rule_the_backup_points_at(
    db, reserved_execution, aws
):
    """백업과 삽입이 같은 값을 써야 NACL_RESTORE가 그 규칙을 찾는다."""
    execution = reserved_execution()

    run(db, execution)

    sent = next(k for n, k in aws.calls if n == "create_network_acl_entry")
    payload = exec_repo.get_backup_record(db, execution.backup_record_id).payload
    assert sent["RuleNumber"] == payload["rule_number"]
    assert sent["CidrBlock"] == payload["cidr_block"]
    assert sent["Protocol"] == payload["protocol"]
    assert sent["RuleAction"] == payload["rule_action"]
    assert sent["Egress"] == payload["egress"]


def test_one_step_is_stored_with_the_applied_effect(db, reserved_execution, aws):
    execution = reserved_execution()

    run(db, execution)

    steps = exec_repo.list_steps(db, execution.execution_id)
    assert [(s.sequence, s.step_type, s.status, s.effect) for s in steps] == [
        (1, ex.STEP_CREATE_NACL_ENTRY, S.SUCCESS, E.APPLIED)
    ]


def test_execution_stays_in_progress_for_the_dispatcher_to_close(
    db, reserved_execution, aws
):
    """종료 확정은 close_execution 하나가 한다 — 실행은 상태를 옮기지 않는다."""
    execution = reserved_execution()

    run(db, execution)

    assert execution.status is ExecutionStatus.IN_PROGRESS


def test_reading_the_parameters_from_the_validated_command(db, reserved_execution, aws):
    """Guardrail PASS의 불변 실행 명령이 채워지면 그것이 원천이다."""
    execution = reserved_execution(
        with_candidate=False,
        validated_command={
            "runbook_id": RunbookId.RUNBOOK_NACL_ADD_DENY.value,
            "target_arn": ACL_ARN,
            "parameters": {
                "network_acl_id": ACL,
                "rule_number": 120,
                "cidr_block": "198.51.100.0/24",
                "protocol": "udp",
                "evidence_id": "ev-1",
            },
            "evidence_ids": ["ev-1"],
        },
    )

    assert run(db, execution).succeeded

    sent = next(k for n, k in aws.calls if n == "create_network_acl_entry")
    assert (sent["RuleNumber"], sent["Protocol"]) == (120, "17")


# ------------------------------------------------------------------ 백업 계약


def test_the_same_execution_never_gets_a_second_backup(db, reserved_execution, aws):
    """재시도가 새 레코드를 만들면 "조치 직전"이 아닌 값이 원복 근거가 된다(ADR-0008 §1 ③)."""
    execution = reserved_execution()
    params = workflows._nacl_add_deny_params(db, execution)

    first = workflows.store_nacl_rule_index_backup(db, execution.execution_id, params)
    second = workflows.store_nacl_rule_index_backup(db, execution.execution_id, params)

    assert first.created and not second.created
    assert first.record.backup_record_id == second.record.backup_record_id


def test_a_taken_slot_stops_the_action_before_aws_is_changed(
    db, reserved_execution, aws
):
    """승인 대기 동안 제3자가 그 번호를 썼다 — 백업이 남의 규칙을 가리키게 두지 않는다."""
    aws(describe_network_acls={"NetworkAcls": [{"NetworkAclId": ACL, "Entries": [our_entry()]}]})
    execution = reserved_execution()

    outcome = run(db, execution)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert "create_network_acl_entry" not in operations(aws)
    assert execution.backup_record_id is None
    # 자산을 만지지 않았으므로 되돌릴 것이 없다 — 단계도 남지 않는다
    assert exec_repo.list_steps(db, execution.execution_id) == []


def test_a_failed_capture_stops_the_action(db, reserved_execution, aws):
    aws(describe_network_acls=client_error("UnauthorizedOperation"))
    execution = reserved_execution()

    outcome = run(db, execution)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_UNAUTHORIZED
    assert "create_network_acl_entry" not in operations(aws)


def test_missing_parameters_stop_the_action(db, reserved_execution, aws):
    execution = reserved_execution(with_candidate=False)

    outcome = run(db, execution)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


def test_the_backup_store_refuses_another_runbook(db, reserved_execution, aws):
    """배선 오류는 판정으로 삼키지 않는다 — 엉뚱한 백업 종류를 달고 진행하면 안 된다."""
    execution = reserved_execution()
    params = workflows._nacl_add_deny_params(db, execution)
    execution.runbook_id = RunbookId.RUNBOOK_EC2_RIGHTSIZING
    db.flush()

    with pytest.raises(ValueError):
        workflows.store_nacl_rule_index_backup(db, execution.execution_id, params)


# ------------------------------------------- 끊긴 실행의 종료 판정 (ADR-0008 §6)


@pytest.fixture()
def interrupted(db, reserved_execution, aws):
    """실행이 끝난 뒤의 상태 — 단계와 백업이 남고 실행은 IN_PROGRESS다.

    판정 경로가 보는 것이 정확히 이 모양이다(dispatcher._dispatch_one: 단계가
    1건이라도 있으면 재실행이 아니라 판정으로 간다).
    """

    def _make():
        execution = reserved_execution()
        run(db, execution)
        return execution

    return _make


def judge(db, execution):
    return workflows.judge_nacl_add_deny(db, execution.execution_id)


def test_judge_confirms_success_when_our_rule_is_there(db, interrupted, aws):
    execution = interrupted()
    aws(describe_network_acls={"NetworkAcls": [{"NetworkAclId": ACL, "Entries": [our_entry()]}]})

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.SUCCESS
    assert judgement.error_summary is None


def test_judge_fails_when_the_slot_is_empty(db, interrupted, aws):
    """삽입되지 않았다 — 자산이 바뀌지 않았으므로 확정해도 되돌릴 것이 남지 않는다."""
    execution = interrupted()

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.FAILED
    assert str(RULE_NUMBER) in judgement.error_summary


def test_judge_fails_when_the_slot_holds_someone_elses_rule(db, interrupted, aws):
    """성공으로 확정하면 남의 규칙이 우리 차단으로 기록되고, NACL_RESTORE가 그 기록을
    근거로 그것을 삭제한다 — 삭제는 되돌릴 수 없다(ADR-0008 §5)."""
    execution = interrupted()
    aws(
        describe_network_acls={
            "NetworkAcls": [
                {"NetworkAclId": ACL, "Entries": [our_entry(CidrBlock="10.0.0.0/8")]}
            ]
        }
    )

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.FAILED
    assert "수동 개입" in judgement.error_summary


def test_judge_defers_when_aws_cannot_be_asked(db, interrupted, aws):
    """자산 상태를 본 적이 없다 — 확정하면 검증기의 실패가 조치의 실패로 저장된다."""
    execution = interrupted()
    aws(describe_network_acls=client_error("RequestLimitExceeded", status=503))

    judgement = judge(db, execution)

    assert judgement.deferred
    assert judgement.next_status is None
    # 보류 사유는 저장하지 않는다(Issue #249의 계약이 서기 전까지) — 로그 몫이다
    assert judgement.error_summary is None


def test_a_missing_nacl_is_a_failed_block_not_a_deferral(db, interrupted, aws):
    """NACL 자체가 사라졌으면 그 차단은 지금 효력이 없다 — 결론이 있는 사건이다."""
    execution = interrupted()
    aws(describe_network_acls={"NetworkAcls": []})

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.FAILED


def test_judge_refuses_a_backup_payload_that_breaks_the_contract(
    db, interrupted, aws, make_execution
):
    """우리가 쓴 것과 같은 모델로 읽는다 — 걸리면 이 경로가 만든 레코드가 아니다.
    대조할 fingerprint가 없으므로 성공이라 말할 수 없다."""
    execution = interrupted()
    record = exec_repo.get_backup_record(db, execution.backup_record_id)
    record.payload = {"rule_number": RULE_NUMBER, "egress": False}
    db.flush()

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.FAILED
    assert "계약" in judgement.error_summary
