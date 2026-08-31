"""RIGHTSIZING 실행 워크플로 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip).

AWS 호출 분기는 services/tests/test_execute_rightsizing.py가 맡고, 여기서는
**순서와 기록**을 본다 — 백업이 commit된 뒤에만 변경이 시작되는가, 단계가 남는가,
종료 상태가 실제 결과와 같은가. 이 셋이 어긋나면 자동 원복이 근거를 잃는다.
"""

import sys
import uuid
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
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.incidents import IncidentCategory  # noqa: E402
from schemas.candidates import CandidateStatus, RunbookCandidateData  # noqa: E402
from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
CANDIDATE_TYPE = "t3.medium"

INSTANCE_RESPONSE = {
    "Reservations": [
        {
            "Instances": [
                {
                    "InstanceId": INSTANCE,
                    "InstanceType": "t3.xlarge",
                    "State": {"Name": "running"},
                }
            ]
        }
    ]
}
STOP_RESPONSE = {
    "StoppingInstances": [{"InstanceId": INSTANCE, "PreviousState": {"Name": "running"}}]
}


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Op",
    )


class FakeWaiter:
    def __init__(self, state):
        self._state = state

    def wait(self, **kwargs):
        self._state["calls"].append(("wait", kwargs))


class FakeEc2:
    def __init__(self, state):
        self._state = state

    def get_waiter(self, name):
        return FakeWaiter(self._state)

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((operation, kwargs))
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if operation == "describe_instances":
                return INSTANCE_RESPONSE
            if operation == "stop_instances":
                return STOP_RESPONSE
            return {}

        return call


@pytest.fixture
def aws(monkeypatch):
    """캡처(backup)와 실행(executor)이 같은 가짜 EC2를 본다 — 호출 순서를 한 줄로 읽는다."""
    state = {"overrides": {}, "calls": []}

    def factory(service, region=None, **_):
        return FakeEc2(state)

    monkeypatch.setattr(bk, "aws_client", factory)
    monkeypatch.setattr(ex, "aws_client", factory)

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    return configure


def _execution(db, *, runbook=RunbookId.RUNBOOK_EC2_RIGHTSIZING, with_candidate=True, **kwargs):
    incident = incidents_repo.create_incident(
        db, subject_arn=INSTANCE_ARN, category=IncidentCategory.FINOPS
    )
    candidate_id = None
    if with_candidate:
        candidate = incidents_repo.add_candidate(
            db,
            RunbookCandidateData(
                candidate_id=str(uuid.uuid4()),
                incident_id=incident.incident_id,
                runbook_id=runbook,
                target_arn=INSTANCE_ARN,
                parameters={"target_instance_type": CANDIDATE_TYPE},
                evidence_ids=["ev-1"],
                status=CandidateStatus.CLAIMED,
            ),
        )
        candidate_id = candidate.candidate_id
    return exec_repo.create_execution(
        db,
        incident_id=incident.incident_id,
        runbook_id=runbook,
        target_arn=INSTANCE_ARN,
        trigger_source=TriggerSource.USER_APPROVAL,
        candidate_id=candidate_id,
        **kwargs,
    )


def operations(aws):
    return [name for name, _ in aws.calls]


# ------------------------------------------------------------------ 성공 경로


def test_backup_is_committed_before_any_change(db, aws):
    """변경과 백업 사이에서 죽으면 되돌릴 값이 남지 않는다(ADR-0004 정책 ③)."""
    execution = _execution(db)

    outcome = workflows.run_rightsizing_execution(db, execution.execution_id)

    assert outcome.succeeded
    assert operations(aws).index("describe_instances") < operations(aws).index("stop_instances")
    assert execution.backup_record_id is not None
    record = exec_repo.get_backup_record(db, execution.backup_record_id)
    assert record.payload["instance_type"] == "t3.xlarge"


def test_steps_are_stored_in_order(db, aws):
    execution = _execution(db)

    workflows.run_rightsizing_execution(db, execution.execution_id)

    steps = exec_repo.list_steps(db, execution.execution_id)
    assert [(s.sequence, s.step_type, s.status, s.effect) for s in steps] == [
        (1, ex.STEP_STOP_INSTANCE, S.SUCCESS, E.APPLIED),
        (2, ex.STEP_MODIFY_INSTANCE_TYPE, S.SUCCESS, E.APPLIED),
        (3, ex.STEP_START_INSTANCE, S.SUCCESS, E.APPLIED),
    ]


def test_execution_stays_in_progress_until_the_dispatcher_closes_it(db, aws):
    """종료 상태는 여기서 확정하지 않는다.

    실행만 먼저 종료로 옮기면 Incident는 ACTION_IN_PROGRESS인데 진행 중 실행이
    없는 조합이 되고, 상세 응답 계약(api/incidents.py)이 그것을 거절해 조회가
    500이 된다. 실행 종료와 Incident 전이는 dispatcher.py가 한 트랜잭션에서 한다.
    """
    execution = _execution(db)

    outcome = workflows.run_rightsizing_execution(db, execution.execution_id)

    assert outcome.succeeded and outcome.reason_code is None
    row = exec_repo.get_execution(db, execution.execution_id)
    assert row.status is ExecutionStatus.IN_PROGRESS
    assert row.finished_at is None and row.error_summary is None


def test_target_type_comes_from_the_candidate(db, aws):
    execution = _execution(db)

    workflows.run_rightsizing_execution(db, execution.execution_id)

    modify = next(kwargs for name, kwargs in aws.calls if name == "modify_instance_attribute")
    assert modify["InstanceType"] == {"Value": CANDIDATE_TYPE}


def test_validated_command_wins_over_the_candidate(db, aws):
    """Guardrail PASS의 불변 실행 명령이 있으면 그것이 원천이다."""
    execution = _execution(db, validated_command={"target_instance_type": "t3.nano"})

    workflows.run_rightsizing_execution(db, execution.execution_id)

    modify = next(kwargs for name, kwargs in aws.calls if name == "modify_instance_attribute")
    assert modify["InstanceType"] == {"Value": "t3.nano"}


# ------------------------------------------------------------------ 실패 경로


def test_backup_failure_stops_before_the_change(db, aws):
    """백업이 없으면 조치를 시작하지 않는다 — 원복 근거 없는 변경은 만들지 않는다."""
    execution = _execution(db)
    aws(describe_instances=client_error("InvalidInstanceID.NotFound"))

    outcome = workflows.run_rightsizing_execution(db, execution.execution_id)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND
    assert outcome.error_summary and outcome.steps == ()
    assert "stop_instances" not in operations(aws)
    # 실패도 종료 상태로 옮기지 않는다 — Incident 전이와 함께 dispatcher가 확정한다
    assert exec_repo.get_execution(db, execution.execution_id).status is ExecutionStatus.IN_PROGRESS


def test_missing_target_type_fails_without_touching_aws(db, aws):
    execution = _execution(db, with_candidate=False)

    outcome = workflows.run_rightsizing_execution(db, execution.execution_id)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


def test_failed_change_keeps_the_step_trace(db, aws):
    """실행이 어디서 멈췄고 자산이 바뀌었는지가 남아야 원복이 판단할 수 있다."""
    execution = _execution(db)
    aws(modify_instance_attribute=client_error("InvalidParameterValue"))

    outcome = workflows.run_rightsizing_execution(db, execution.execution_id)

    assert not outcome.succeeded
    steps = exec_repo.list_steps(db, execution.execution_id)
    assert [(s.sequence, s.status, s.effect) for s in steps] == [
        (1, S.SUCCESS, E.APPLIED),
        (2, S.FAILED, E.NOT_APPLIED),
    ]
    # 종료 상태를 확정할 dispatcher가 읽을 재료 — 어디까지 바뀌었는지가 반환값에 실린다
    assert [(s.sequence, s.effect) for s in outcome.steps] == [
        (1, E.APPLIED),
        (2, E.NOT_APPLIED),
    ]
    assert "InvalidParameterValue" in outcome.error_summary


# ------------------------------------------------------------------ 배선 오류


def test_other_runbooks_are_rejected(db, aws):
    """런북마다 단계와 백업 종류가 다르다 — 조용히 진행시키지 않는다."""
    execution = _execution(
        db, runbook=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED, with_candidate=False
    )

    with pytest.raises(ValueError):
        workflows.run_rightsizing_execution(db, execution.execution_id)


def test_finished_execution_is_not_run_again(db, aws):
    """다시 돌리면 백업 없는 두 번째 변경이 된다."""
    execution = _execution(db)
    exec_repo.update_execution_status(
        db,
        execution.execution_id,
        expected=ExecutionStatus.IN_PROGRESS,
        next_status=ExecutionStatus.SUCCESS,
    )

    with pytest.raises(ValueError):
        workflows.run_rightsizing_execution(db, execution.execution_id)


def test_unknown_execution_is_a_wiring_error(db, aws):
    with pytest.raises(ValueError):
        workflows.run_rightsizing_execution(db, str(uuid.uuid4()))
