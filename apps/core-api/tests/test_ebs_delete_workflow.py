"""EBS 삭제 실행 워크플로 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip). (Issue #369)

AWS 호출 분기는 services/tests/test_execute_ebs_delete.py가 맡고, 여기서는 **배선과
판정**을 본다.

  - 실행 함수와 판정 함수가 **짝으로** 등록됐는가 (ADR-0008 §6)
  - 단계 기록이 실제로 남는가 — 끊긴 실행을 판정으로 보내는 유일한 표시다
  - **되돌릴 수 없는 실패의 판정이 실자산을 보는가** — 삭제가 `UNKNOWN`으로 끝나면
    자동 원복 짝이 없어 현물 판정으로 가고, 그 판정은 볼륨 존재 여부로만 갈린다

마지막 축이 이 런북의 핵심이다. 삭제는 되돌릴 수 없어서, 판정이 단계 기록을 근거로
성공이라 말하면 지우지 못한 볼륨이 조용히 닫힌다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
import workflows  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import RunbookId  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
VOLUME = "vol-0abc123456789def0"
VOLUME_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/{VOLUME}"
SNAPSHOT = "snap-0abc123456789def0"

UNATTACHED = {"Volumes": [{"VolumeId": VOLUME, "State": "available", "Attachments": []}]}
SNAPSHOT_RESPONSE = {"SnapshotId": SNAPSHOT, "State": "pending"}


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Op",
    )


class FakeWaiter:
    def __init__(self, state, name):
        self._state = state
        self._name = name

    def wait(self, **kwargs):
        self._state["calls"].append((f"waiter:{self._name}", kwargs))
        outcome = self._state["overrides"].get(f"waiter:{self._name}")
        if isinstance(outcome, BaseException):
            raise outcome
        return None


class FakeEc2:
    def __init__(self, state):
        self._state = state

    def get_waiter(self, name):
        return FakeWaiter(self._state, name)

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((operation, kwargs))
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if operation == "describe_volumes":
                return UNATTACHED
            if operation == "create_snapshot":
                return SNAPSHOT_RESPONSE
            return {}

        return call


@pytest.fixture
def aws(monkeypatch):
    state = {"overrides": {}, "calls": []}
    monkeypatch.setattr(ex, "aws_client", lambda service, region=None, **_: FakeEc2(state))

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    return configure


@pytest.fixture()
def reserved_execution(db, make_incident, make_candidate, make_execution):
    """삭제 실행 1건 — FINOPS Incident + CLAIMED 후보 위에 선다."""

    def _make(**kwargs):
        incident = make_incident(
            db,
            category=IncidentCategory.FINOPS,
            subject_arn=VOLUME_ARN,
            status=IncidentStatus.ANALYZING,
        )
        candidate = make_candidate(
            db,
            incident,
            runbook_id=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED,
            target_arn=VOLUME_ARN,
            status=CandidateStatus.CLAIMED,
        )
        return make_execution(
            db,
            incident,
            runbook_id=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED,
            target_arn=VOLUME_ARN,
            candidate=candidate,
            **kwargs,
        )

    return _make


def operations(aws):
    return [name for name, _ in aws.calls]


def run(db, execution):
    return workflows.run_ebs_delete_unattached_execution(db, execution.execution_id)


def judge(db, execution):
    return workflows.judge_ebs_delete_unattached(db, execution.execution_id)


# ------------------------------------------------------------------ 디스패치 배선


def test_runner_and_judge_are_registered_as_a_pair():
    """짝이 어긋나면 import 시점에 기동이 막힌다(dispatcher.py) — 그 계약을 여기서도 고정한다.

    runner만 등록하면 승인은 접수되는데 실행이 매 주기 미지원으로 넘어가 IN_PROGRESS에
    갇힌다. 이 런북이 실제로 그 상태였고(2026-09-21 실측), 그것이 이 카드의 출발점이다.
    """
    runbook = RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED
    assert dispatcher._RUNNERS[runbook] is workflows.run_ebs_delete_unattached_execution
    assert dispatcher._JUDGES[runbook] is workflows.judge_ebs_delete_unattached
    assert set(dispatcher._RUNNERS) == set(dispatcher._JUDGES)


def test_this_runbook_has_no_auto_rollback_pair():
    """등록 롤백이 없다 — 자산이 바뀐 채 실패해도 원복이 아니라 현물 판정으로 간다."""
    assert (
        RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED
        not in dispatcher._AUTO_ROLLBACK_ON_ASSET_CHANGE
    )


# ------------------------------------------------------------------ 실행 경로


def test_records_the_three_steps_in_order(db, reserved_execution, aws):
    execution = reserved_execution()

    outcome = run(db, execution)

    assert outcome.succeeded
    assert operations(aws) == [
        "describe_volumes",
        "create_snapshot",
        "waiter:snapshot_completed",
        "delete_volume",
    ]
    assert [step.step_type for step in outcome.steps] == [
        ex.STEP_CREATE_SNAPSHOT,
        ex.STEP_WAIT_SNAPSHOT,
        ex.STEP_DELETE_VOLUME,
    ]


def test_uses_no_backup_record(db, reserved_execution, aws):
    """BackupType 4종에 EBS가 없다 — 되돌릴 근거는 DB가 아니라 AWS 스냅숏이다."""
    execution = reserved_execution()

    run(db, execution)

    assert execution.backup_record_id is None


def test_refuses_to_run_a_finished_execution(db, reserved_execution, aws):
    """끝난 실행을 다시 돌리면 이미 지운 볼륨에 두 번째 삭제가 나간다."""
    execution = reserved_execution(status=ExecutionStatus.SUCCESS)

    with pytest.raises(ValueError):
        run(db, execution)
    assert operations(aws) == []


def test_refuses_another_runbooks_execution(db, make_incident, make_execution, aws):
    incident = make_incident(db, category=IncidentCategory.FINOPS, subject_arn=VOLUME_ARN)
    execution = make_execution(
        db,
        incident,
        runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
        target_arn=VOLUME_ARN,
    )

    with pytest.raises(ValueError):
        run(db, execution)


# ------------------------------------------------------------------ 종료 판정


def test_judges_success_when_the_volume_is_gone(db, reserved_execution, aws):
    """성공의 경계는 실자산이다 — 단계 기록이 아니라 지금 볼륨이 있는지가 답한다."""
    execution = reserved_execution()
    aws(describe_volumes=client_error("InvalidVolume.NotFound", 400))

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.SUCCESS


def test_judges_failed_when_the_volume_is_still_there(db, reserved_execution, aws):
    """삭제가 UNKNOWN으로 끝난 실행이 오는 자리 — 볼륨이 남았으면 미완이다."""
    execution = reserved_execution()

    judgement = judge(db, execution)

    assert judgement.next_status is ExecutionStatus.FAILED
    assert VOLUME in judgement.error_summary


def test_defers_when_the_volume_lookup_fails(db, reserved_execution, aws):
    """AWS에 물어보지 못했다 — 확정하면 검증기의 실패가 조치의 실패로 저장된다(#249)."""
    execution = reserved_execution()
    aws(describe_volumes=EndpointConnectionError(endpoint_url="https://ec2"))

    judgement = judge(db, execution)

    assert judgement.next_status is None
    assert judgement.defer_code is R.PRECHECK_AWS_ERROR


def test_interrupted_delete_goes_to_the_judge_not_to_a_rollback(db, reserved_execution, aws):
    """5xx로 끊긴 삭제 — effect가 UNKNOWN이라 '자산이 바뀌었을 수 있다'로 읽힌다.

    짝 롤백이 없으므로 여기서 갈 곳은 자동 원복이 아니라 현물 판정이고, 그 판정이
    볼륨 존재 여부로 성공·실패를 가른다.
    """
    execution = reserved_execution()
    aws(delete_volume=client_error("InternalError", 500))

    outcome = run(db, execution)

    assert not outcome.succeeded
    assert outcome.steps[-1].effect is E.UNKNOWN
    assert dispatcher._changed_the_asset(outcome)
    assert (
        RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED
        not in dispatcher._AUTO_ROLLBACK_ON_ASSET_CHANGE
    )
