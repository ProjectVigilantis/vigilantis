"""실행 Dispatcher 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip). (Issue #232)

AWS 호출 분기는 services/tests/test_execute_rightsizing.py가, 실행 순서·단계 기록은
test_rightsizing_workflow.py가 맡는다. 여기서는 **누구를 집고, 어디로 확정하고,
언제 알리는가**를 본다 — 선점, 실행 결과에 따른 종료 상태, 그 종료가 Incident를
어디로 옮기는가, 그리고 발행이 commit 뒤인가.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, WaiterError

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
import workflows  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.api.ws import WsEventType  # noqa: E402
from schemas.candidates import CandidateStatus, RunbookCandidateData  # noqa: E402
from schemas.executions import (  # noqa: E402
    EXECUTION_RECOVERABLE_STATUSES,
    ExecutionEffect,
    ExecutionStepResult,
    ExecutionStepStatus,
)
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws import rollback as rb  # noqa: E402

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
VOLUME_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0abc123456789def0"
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
    def __init__(self, state, name):
        self._state = state
        self._name = name

    def wait(self, **kwargs):
        self._state["calls"].append((f"wait:{self._name}", kwargs))
        outcome = self._state["overrides"].get(f"waiter:{self._name}")
        if isinstance(outcome, BaseException):
            raise outcome


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
            if operation == "describe_instances":
                return INSTANCE_RESPONSE
            if operation == "stop_instances":
                return STOP_RESPONSE
            return {}

        return call


@pytest.fixture
def aws(monkeypatch):
    """캡처(backup)·실행(executor)·판정(rollback)이 같은 가짜 EC2를 본다."""
    state = {"overrides": {}, "calls": []}

    def factory(service, region=None, **_):
        return FakeEc2(state)

    monkeypatch.setattr(bk, "aws_client", factory)
    monkeypatch.setattr(ex, "aws_client", factory)
    monkeypatch.setattr(rb, "aws_client", factory)
    # 판정 대기는 실제로 자지 않는다 — 이 테스트가 보는 것은 대기가 아니라 라우팅이다
    monkeypatch.setattr(
        rb,
        "get_settings",
        lambda: type(
            "S",
            (),
            {"STATUS_CHECK_WAIT_DELAY_SECONDS": 1, "STATUS_CHECK_WAIT_MAX_ATTEMPTS": 1},
        )(),
    )

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    return configure


def _incident(db):
    return incidents_repo.create_incident(
        db, subject_arn=INSTANCE_ARN, category=IncidentCategory.FINOPS
    )


def _candidate(db, incident_id, *, runbook, target_arn, parameters, status):
    return incidents_repo.add_candidate(
        db,
        RunbookCandidateData(
            candidate_id=str(uuid.uuid4()),
            incident_id=incident_id,
            runbook_id=runbook,
            target_arn=target_arn,
            parameters=parameters,
            evidence_ids=["ev-1"],
            status=status,
        ),
    )


def _reserved(db, *, runbook=RunbookId.RUNBOOK_EC2_RIGHTSIZING):
    """접수 직후 상태 — IN_PROGRESS 실행 1건 + CLAIMED 후보, Incident는 조치 진행 중.

    ORM 객체가 아니라 식별자를 돌려준다. 스캔은 자기가 만든 세션을 닫으므로(운영
    경로와 같다) 스캔 너머로 들고 간 객체는 detached가 된다.
    """
    incident = _incident(db)
    candidate = _candidate(
        db,
        incident.incident_id,
        runbook=runbook,
        target_arn=INSTANCE_ARN,
        parameters={"target_instance_type": CANDIDATE_TYPE}
        if runbook is RunbookId.RUNBOOK_EC2_RIGHTSIZING
        else {},
        status=CandidateStatus.CLAIMED,
    )
    incidents_repo.update_incident_status(
        db,
        incident.incident_id,
        expected=incident.status,
        next_status=IncidentStatus.ACTION_IN_PROGRESS,
    )
    execution = exec_repo.create_execution(
        db,
        incident_id=incident.incident_id,
        runbook_id=runbook,
        target_arn=INSTANCE_ARN,
        trigger_source=TriggerSource.USER_APPROVAL,
        candidate_id=candidate.candidate_id,
    )
    db.commit()
    return incident.incident_id, execution.execution_id


def cycle(db, publish=None):
    """스캔 1회. 세션을 닫는 바깥 껍질(run_dispatch_cycle)은 부르지 않는다 —
    테스트 세션은 픽스처가 소유하고 종료 시 전부 rollback한다."""
    return dispatcher.dispatch_pending(db, publish)


def operations(aws):
    return [name for name, _ in aws.calls]


def status_of(db, incident_id, execution_id):
    incident = incidents_repo.get_incident(db, incident_id)
    execution = exec_repo.get_execution(db, execution_id)
    return incident.status, execution.status


# ------------------------------------------------------------------ 디스패치


def test_reserved_execution_is_dispatched_to_aws(db, aws):
    """접수만 되고 멈춰 있던 예약이 사람 개입 없이 실행으로 넘어간다."""
    _reserved(db)

    report = cycle(db)

    assert report.scanned == 1 and report.started == 1
    assert operations(aws)[:1] == ["describe_instances"]  # 백업이 먼저다
    assert "modify_instance_attribute" in operations(aws)


def test_successful_execution_waits_for_the_status_check(db, aws):
    """기동 요청 접수는 성공의 경계가 아니다 — 2/2 판정 전에는 확정하지 않는다."""
    incident_id, execution_id = _reserved(db)

    report = cycle(db)

    assert report.awaiting_status_check == 1 and report.closed == 0
    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.ACTION_IN_PROGRESS,
        ExecutionStatus.IN_PROGRESS,
    )


def test_unsupported_runbook_is_not_dispatched(db, aws):
    """실행 함수가 없는 런북을 실패로 확정하면 미구현이 조치 실패로 둔갑한다."""
    incident_id, execution_id = _reserved(
        db, runbook=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED
    )

    report = cycle(db)

    assert report.unsupported == 1 and report.started == 0
    assert aws.calls == []
    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.ACTION_IN_PROGRESS,
        ExecutionStatus.IN_PROGRESS,
    )


def test_execution_with_steps_is_judged_not_rerun(db, aws):
    """단계가 남았다는 것은 자산이 이미 만져졌을 수 있다는 뜻이다 — 재실행이 아니라 판정.

    호출이 끝나지 않은 채(IN_PROGRESS) 남은 단계는 적용 여부를 알 수 없으므로
    되돌릴 것이 있다고 본다. 다시 돌리면 백업 없는 두 번째 AWS 변경이 된다.
    """
    incident_id, execution_id = _reserved(db)
    exec_repo.add_step(
        db,
        ExecutionStepResult(
            sequence=1,
            affected_arn=INSTANCE_ARN,
            step_type=ex.STEP_STOP_INSTANCE,
            aws_operation="ec2.stop_instances",
            status=ExecutionStepStatus.IN_PROGRESS,
            occurred_at=datetime.now(timezone.utc),
        ),
        execution_id=execution_id,
    )
    db.commit()

    report = cycle(db)

    assert report.judged == 1 and report.started == 0
    assert report.rollback_initiated == 1
    assert aws.calls == []  # 조치가 미완인 것이 이미 확정이라 2/2를 물을 이유가 없다
    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.ACTION_IN_PROGRESS,
        ExecutionStatus.ROLLBACK_INITIATED,
    )


# ------------------------------------------------------ 종료 확정과 Incident 전이


def test_failed_execution_closes_the_incident_too(db, aws):
    """변경 없이 실패한 실행(1단계 4xx 거절)만 FAILED로 확정된다."""
    incident_id, execution_id = _reserved(db)
    aws(stop_instances=client_error("IncorrectInstanceState"))

    report = cycle(db)

    assert report.closed == 1
    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.FAILED,
        ExecutionStatus.FAILED,
    )
    row = exec_repo.get_execution(db, execution_id)
    assert row.finished_at is not None
    assert "IncorrectInstanceState" in row.error_summary


def test_incident_returns_to_awaiting_approval_when_a_proposal_remains(db, aws):
    """실패해도 실행 가능한 제안이 남아 있으면 관제자가 고를 것이 있다."""
    incident_id, execution_id = _reserved(db)
    _candidate(
        db,
        incident_id,
        runbook=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED,
        target_arn=VOLUME_ARN,
        parameters={},
        status=CandidateStatus.EXECUTABLE,
    )
    db.commit()
    aws(stop_instances=client_error("IncorrectInstanceState"))

    cycle(db)

    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.AWAITING_APPROVAL,
        ExecutionStatus.FAILED,
    )


def test_incident_stays_in_progress_while_another_execution_runs(db, aws):
    """진행 중인 실행이 하나라도 남으면 나가지 않는다 — 상세 응답 계약이 그것을 요구한다."""
    incident_id, execution_id = _reserved(db)
    other_id = exec_repo.create_execution(
        db,
        incident_id=incident_id,
        runbook_id=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED,
        target_arn=VOLUME_ARN,
        trigger_source=TriggerSource.USER_APPROVAL,
    ).execution_id
    db.commit()
    aws(stop_instances=client_error("IncorrectInstanceState"))

    cycle(db)

    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.ACTION_IN_PROGRESS,
        ExecutionStatus.FAILED,
    )
    assert exec_repo.get_execution(db, other_id).status is (
        ExecutionStatus.IN_PROGRESS
    )


def test_second_close_is_a_no_op(db, aws):
    """이미 확정된 실행에 두 번째 확정이 들어와도 상태와 사유를 덮어쓰지 않는다."""
    _incident_id, execution_id = _reserved(db)
    aws(stop_instances=client_error("IncorrectInstanceState"))
    cycle(db)

    again = workflows.close_execution(
        db,
        execution_id,
        next_status=ExecutionStatus.SUCCESS,
        error_summary="두 번째 확정",
    )

    assert again is None
    row = exec_repo.get_execution(db, execution_id)
    assert row.status is ExecutionStatus.FAILED
    assert "두 번째 확정" not in (row.error_summary or "")


def test_one_poisoned_execution_does_not_starve_the_scan(db, aws, monkeypatch):
    """실행·확정 1건의 예외는 그 행에서 멈춘다 — 스캔의 나머지는 계속 돈다.

    예외를 스캔 루프까지 새게 두면 깨진 행 하나가 매 주기 같은 자리에서 스캔을
    끊어, 뒤의 모든 예약이 영영 디스패치되지 않는다.
    """
    _p_incident, poisoned_id = _reserved(db)
    _h_incident, healthy_id = _reserved(db)

    real_runner = workflows.run_rightsizing_execution

    def runner(session, execution_id):
        if execution_id == poisoned_id:
            raise RuntimeError("배선 오류 재현")
        return real_runner(session, execution_id)

    monkeypatch.setitem(
        dispatcher._RUNNERS, RunbookId.RUNBOOK_EC2_RIGHTSIZING, runner
    )

    report = cycle(db)

    # 스캔 순서와 무관하게: 독이 든 1건은 errored, 나머지 1건은 정상 경로
    assert report.scanned == 2
    assert report.errored == 1 and report.awaiting_status_check == 1
    # 독이 든 행은 종료로 확정되지 않고 남는다 — 다음 주기가 다시 본다
    assert exec_repo.get_execution(db, poisoned_id).status is (
        ExecutionStatus.IN_PROGRESS
    )
    assert exec_repo.get_execution(db, healthy_id).status is (
        ExecutionStatus.IN_PROGRESS
    )


def test_close_failure_is_also_contained(db, aws, monkeypatch):
    """확정(close_execution) 단계의 예외도 같은 우산 안이다 — 그 1건만 errored."""
    _p_incident, poisoned_id = _reserved(db)
    _h_incident, healthy_id = _reserved(db)
    aws(stop_instances=client_error("IncorrectInstanceState"))

    real_close = workflows.close_execution

    def close(session, execution_id, **kwargs):
        if execution_id == poisoned_id:
            raise RuntimeError("확정 단계 오류 재현")
        return real_close(session, execution_id, **kwargs)

    monkeypatch.setattr(workflows, "close_execution", close)

    report = cycle(db)

    assert report.scanned == 2
    assert report.errored == 1 and report.closed == 1
    # 확정이 끊긴 행은 IN_PROGRESS로 남고(전이 롤백), 나머지는 정상 확정된다
    assert exec_repo.get_execution(db, poisoned_id).status is (
        ExecutionStatus.IN_PROGRESS
    )
    assert exec_repo.get_execution(db, healthy_id).status is (
        ExecutionStatus.FAILED
    )


def test_partially_applied_failure_initiates_rollback(db, aws):
    """자산이 바뀐 채 실패한 실행은 FAILED가 아니라 ROLLBACK_INITIATED다.

    1단계 정지가 APPLIED로 끝난 뒤 2단계 타입 변경이 실패하면 인스턴스는 정지된
    채 남는다. FAILED는 계약상 "변경 없이 실패"라(schemas/executions.py 복구 가능
    상태 주석) 그것으로 확정하면 관제자 복구 목록이 닫힌다. 2/2를 물을 이유도
    없다 — 조치가 제 갈 데까지 가지 못한 것이 이미 확정이다.
    """
    incident_id, execution_id = _reserved(db)
    aws(modify_instance_attribute=client_error("InvalidParameterValue"))
    events = []

    report = cycle(db, events.append)

    assert report.rollback_initiated == 1 and report.closed == 0
    assert [event.event_type for event in events] == [
        WsEventType.EXECUTION_UPDATED,
        WsEventType.INCIDENT_UPDATED,
    ]
    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.ACTION_IN_PROGRESS,
        ExecutionStatus.ROLLBACK_INITIATED,
    )
    row = exec_repo.get_execution(db, execution_id)
    assert row.finished_at is None  # 비종료 상태다 — 자동 원복이 아직 남았다
    # 관제자 복구 목록이 열려 있다(EXECUTION_RECOVERABLE_STATUSES)
    assert row.status in EXECUTION_RECOVERABLE_STATUSES
    # 다음 주기는 이 행을 다시 집지 않는다 — 자동 원복은 #241 몫이다
    assert cycle(db).skipped == 1


# ------------------------------------------------- 2/2 Status Check 판정 (#240)


def waiter_timeout() -> WaiterError:
    return WaiterError(
        name=rb.WAITER_NAME, reason="Max attempts exceeded", last_response={}
    )


def _ran_and_awaiting(db, aws):
    """1주기 = 실행. 단계가 남고 IN_PROGRESS인 채로 판정을 기다린다."""
    incident_id, execution_id = _reserved(db)
    assert cycle(db).awaiting_status_check == 1
    aws.calls.clear()
    return incident_id, execution_id


def test_status_check_ok_closes_the_execution_as_success(db, aws):
    """성공한 실행이 IN_PROGRESS로 남지 않는다 — 2/2가 SUCCESS 확정의 경계다."""
    incident_id, execution_id = _ran_and_awaiting(db, aws)

    report = cycle(db)

    assert report.judged == 1 and report.closed == 1
    assert f"wait:{rb.WAITER_NAME}" in operations(aws)
    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.AWAITING_CLOSURE,
        ExecutionStatus.SUCCESS,
    )
    assert exec_repo.get_execution(db, execution_id).finished_at is not None


def test_status_check_failure_initiates_rollback(db, aws):
    """부팅 실패는 원복 개시다 — 원본이 비종료로 남아 복구 경로가 열린다."""
    incident_id, execution_id = _ran_and_awaiting(db, aws)
    aws(
        **{f"waiter:{rb.WAITER_NAME}": waiter_timeout()},
        describe_instance_status={
            "InstanceStatuses": [
                {
                    "InstanceId": INSTANCE,
                    "InstanceState": {"Name": "stopped"},
                    "SystemStatus": {"Status": "not-applicable"},
                    "InstanceStatus": {"Status": "not-applicable"},
                }
            ]
        },
    )

    report = cycle(db)

    assert report.judged == 1 and report.rollback_initiated == 1
    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.ACTION_IN_PROGRESS,
        ExecutionStatus.ROLLBACK_INITIATED,
    )
    row = exec_repo.get_execution(db, execution_id)
    assert row.status in EXECUTION_RECOVERABLE_STATUSES
    assert "stopped" in row.error_summary


def test_status_check_timeout_initiates_rollback(db, aws):
    """제한 시간 안에 2/2가 오지 않아도 성공으로 확정할 근거는 없다."""
    _incident_id, execution_id = _ran_and_awaiting(db, aws)
    aws(
        **{f"waiter:{rb.WAITER_NAME}": waiter_timeout()},
        describe_instance_status={
            "InstanceStatuses": [
                {
                    "InstanceId": INSTANCE,
                    "InstanceState": {"Name": "pending"},
                    "SystemStatus": {"Status": "initializing"},
                    "InstanceStatus": {"Status": "initializing"},
                }
            ]
        },
    )

    assert cycle(db).rollback_initiated == 1
    row = exec_repo.get_execution(db, execution_id)
    assert row.status is ExecutionStatus.ROLLBACK_INITIATED
    assert rb.StatusCheckVerdict.TIMED_OUT.value in row.error_summary


def test_instance_that_was_never_started_skips_the_status_check(db, aws):
    """조치 직전 stopped였던 인스턴스는 기동하지 않는다(executor ③단계 NOT_APPLIED).

    켜지 않은 인스턴스에 2/2를 물으면 영원히 오지 않아 타임아웃 뒤 멀쩡한 자산을
    되돌리게 된다 — 묻기 전에 성공으로 확정해야 한다.
    """
    incident_id, execution_id = _reserved(db)
    aws(
        stop_instances={
            "StoppingInstances": [
                {"InstanceId": INSTANCE, "PreviousState": {"Name": "stopped"}}
            ]
        }
    )
    assert cycle(db).awaiting_status_check == 1
    started = [
        s
        for s in exec_repo.list_steps(db, execution_id)
        if s.step_type == ex.STEP_START_INSTANCE
    ]
    assert started and started[-1].effect is ExecutionEffect.NOT_APPLIED
    aws.calls.clear()

    report = cycle(db)

    assert report.closed == 1
    assert f"wait:{rb.WAITER_NAME}" not in operations(aws)
    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.AWAITING_CLOSURE,
        ExecutionStatus.SUCCESS,
    )


def test_judge_without_a_judge_leaves_the_execution_open(db, aws, monkeypatch):
    """판정 주체가 없는 런북을 실패로 확정하면 미구현이 조치 실패로 둔갑한다."""
    incident_id, execution_id = _ran_and_awaiting(db, aws)
    monkeypatch.delitem(dispatcher._JUDGES, RunbookId.RUNBOOK_EC2_RIGHTSIZING)

    report = cycle(db)

    assert report.unsupported == 1 and report.judged == 0
    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.ACTION_IN_PROGRESS,
        ExecutionStatus.IN_PROGRESS,
    )


def test_one_poisoned_judgement_does_not_starve_the_scan(db, aws, monkeypatch):
    """판정 1건의 예외도 그 행에서 멈춘다 — 실행 경로와 같은 우산이다."""
    _p_incident, poisoned_id = _reserved(db)
    _h_incident, healthy_id = _reserved(db)
    # 두 건을 한 주기에 실행시켜 둘 다 판정 대기로 만든다 — 따로 돌리면 앞 건이
    # 뒤 건의 실행 주기에 판정까지 끝나 버린다
    assert cycle(db).awaiting_status_check == 2
    aws.calls.clear()

    real_judge = workflows.judge_rightsizing_boot

    def judge(session, execution_id):
        if execution_id == poisoned_id:
            raise RuntimeError("판정 배선 오류 재현")
        return real_judge(session, execution_id)

    monkeypatch.setitem(dispatcher._JUDGES, RunbookId.RUNBOOK_EC2_RIGHTSIZING, judge)

    report = cycle(db)

    assert report.scanned == 2
    assert report.errored == 1 and report.closed == 1
    assert exec_repo.get_execution(db, poisoned_id).status is (
        ExecutionStatus.IN_PROGRESS
    )
    assert exec_repo.get_execution(db, healthy_id).status is ExecutionStatus.SUCCESS


def test_incident_waits_for_approval_when_a_proposal_survives_success(db, aws):
    """성공했어도 남은 제안이 있으면 종료 판단이 아니라 승인 대기다(v1.6 ⑤)."""
    incident_id, execution_id = _ran_and_awaiting(db, aws)
    _candidate(
        db,
        incident_id,
        runbook=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED,
        target_arn=VOLUME_ARN,
        parameters={},
        status=CandidateStatus.EXECUTABLE,
    )
    db.commit()

    cycle(db)

    assert status_of(db, incident_id, execution_id) == (
        IncidentStatus.AWAITING_APPROVAL,
        ExecutionStatus.SUCCESS,
    )


# ------------------------------------------------------------------ 실시간 발행


def test_events_are_published_only_after_commit(db, aws, monkeypatch):
    """commit 전에 보내면 받는 쪽이 아직 없는 상태를 조회한다(realtime.py 규약)."""
    incident_id, _execution_id = _reserved(db)
    aws(stop_instances=client_error("IncorrectInstanceState"))

    log = []
    real_commit = db.commit

    def traced_commit():
        real_commit()
        log.append("commit")

    monkeypatch.setattr(db, "commit", traced_commit)
    events = []

    def publish(event):
        log.append("publish")
        events.append(event)

    cycle(db, publish)

    assert log[-3:] == ["commit", "publish", "publish"]
    assert [event.event_type for event in events] == [
        WsEventType.EXECUTION_UPDATED,
        WsEventType.INCIDENT_UPDATED,
    ]
    assert events[0].data.status is ExecutionStatus.FAILED
    assert events[0].data.incident_id == incident_id
    assert events[1].data.incident_id == incident_id


def test_nothing_is_published_when_no_execution_closes(db, aws):
    """성공은 아직 확정이 아니라 알릴 상태 변화도 없다."""
    _reserved(db)
    events = []

    cycle(db, events.append)

    assert events == []


# ------------------------------------------------ 인접 조회 회귀 (상세 응답 계약)


def test_detail_survives_the_transition_with_nothing_left(client_pg, db, aws):
    """전이 뒤 상세 조회가 200이어야 한다 — 나눠 커밋하면 여기가 500이 된다."""
    incident_id, _execution_id = _reserved(db)
    aws(stop_instances=client_error("IncorrectInstanceState"))
    cycle(db)

    response = client_pg.get(f"/api/v1/incidents/{incident_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == IncidentStatus.FAILED.value
    assert body["recommendations"] == []
    assert body["executions"][0]["status"] == ExecutionStatus.FAILED.value


def test_detail_survives_the_transition_with_a_proposal_left(client_pg, db, aws):
    """AWAITING_APPROVAL은 제안 1개 이상을 요구한다 — 빈 채로 옮기면 500이다."""
    incident_id, _execution_id = _reserved(db)
    _candidate(
        db,
        incident_id,
        runbook=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED,
        target_arn=VOLUME_ARN,
        parameters={},
        status=CandidateStatus.EXECUTABLE,
    )
    db.commit()
    aws(stop_instances=client_error("IncorrectInstanceState"))
    cycle(db)

    response = client_pg.get(f"/api/v1/incidents/{incident_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == IncidentStatus.AWAITING_APPROVAL.value
    assert [r["runbook_id"] for r in body["recommendations"]] == [
        RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED.value
    ]


def test_detail_survives_the_success_transition(client_pg, db, aws):
    """성공 확정 뒤 상세 조회가 200이어야 한다.

    AWAITING_CLOSURE는 "수행된 실행 1건 이상 + 진행 중 실행 없음 + 제안 없음"을
    요구한다(api/incidents.py). 그 조합을 만들지 못하면 성공 직후 조회가 500이다.
    """
    incident_id, _execution_id = _ran_and_awaiting(db, aws)
    cycle(db)

    response = client_pg.get(f"/api/v1/incidents/{incident_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == IncidentStatus.AWAITING_CLOSURE.value
    assert body["recommendations"] == []
    assert body["executions"][0]["status"] == ExecutionStatus.SUCCESS.value
    # 성공한 원본은 관제자 복구(REVERT_SIZE)를 연다
    assert body["executions"][0]["available_recovery_runbook_ids"] == [
        RunbookId.RUNBOOK_EC2_REVERT_SIZE.value
    ]


def test_resolved_from_awaiting_closure(client_pg, db, aws):
    """종료 판단이 AWAITING_CLOSURE에서 열린다 — 안 열리면 그 상태가 막다른 길이다."""
    incident_id, _execution_id = _ran_and_awaiting(db, aws)
    cycle(db)

    response = client_pg.post(
        f"/api/v1/incidents/{incident_id}/resolve", json={"resolution": "JUSTIFIED"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == IncidentStatus.RESOLVED.value
