"""자동 원복 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip). (Issue #241)

AWS 호출 분기는 services/tests/test_execute_revert_size.py가, 가드레일 문맥 판정은
ai/tests/test_guardrail_steps.py가 맡는다. 여기서 보는 것은 **발동과 확정**이다 —
원본당 원복이 한 번인가, 원복 값이 백업 레코드에서만 오는가, 자식의 결과가 원본과
Incident를 어디로 옮기는가, 거절이 자동 재시도로 이어지지 않는가.

이 넷이 어긋나면 나타나는 사고는 전부 조용하다: 두 번 발동하면 되돌린 것을 다시
되돌리고, 원본이 확정되지 않으면 인시던트가 영원히 진행 중으로 남으며, 거절이
재시도로 이어지면 ADR-0004 정책 ④가 코드에서 사라진다.
"""

import logging
import sys
import uuid
from datetime import datetime, timezone
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
from db.repositories import assets as assets_repo  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import guardrails as guardrails_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.backups import BackupType  # noqa: E402
from schemas.candidates import CandidateStatus, RunbookCandidateData  # noqa: E402
from schemas.executions import ExecutionStepResult, ExecutionStepStatus  # noqa: E402
from schemas.guardrails import GuardrailDecision, GuardrailStep  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws import rollback as rb  # noqa: E402

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"

BACKUP_TYPE = "t3.xlarge"   # 조치 이전 = 되돌릴 값
APPLIED_TYPE = "t3.medium"  # 원본 RIGHTSIZING이 적용한 값

STOP_RESPONSE = {
    "StoppingInstances": [{"InstanceId": INSTANCE, "PreviousState": {"Name": "running"}}]
}


def dry_run_ok() -> ClientError:
    """DryRun 통과 신호 — 예외 없이 돌아오면 플래그가 안 먹은 것으로 읽힌다."""
    return ClientError({"Error": {"Code": "DryRunOperation"}}, "Op")


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
            if kwargs.get("DryRun"):
                # 가드레일 ④는 DryRunOperation 예외만 통과로 인정한다
                raise self._state["overrides"].get("dry_run", dry_run_ok())
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if operation == "describe_instances":
                return {
                    "Reservations": [
                        {
                            "Instances": [
                                {
                                    "InstanceId": INSTANCE,
                                    "InstanceType": self._state["current_type"],
                                    "State": {"Name": self._state["current_state"]},
                                }
                            ]
                        }
                    ]
                }
            if operation == "stop_instances":
                return STOP_RESPONSE
            return {}

        return call


@pytest.fixture
def aws(monkeypatch):
    """캡처·실행·판정이 같은 가짜 EC2를 본다. 기본은 §3-2 ② 경로다."""
    state = {
        "overrides": {},
        "calls": [],
        "current_type": APPLIED_TYPE,
        "current_state": "running",
    }

    def factory(service, region=None, **_):
        return FakeEc2(state)

    for module in (bk, ex, rb):
        monkeypatch.setattr(module, "aws_client", factory)

    def configure(current_type=None, current_state=None, **overrides):
        if current_type is not None:
            state["current_type"] = current_type
        if current_state is not None:
            state["current_state"] = current_state
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    return configure


def _collected_asset(db):
    """가드레일 ③은 수집된 자산만 통과시킨다 — 원복 대상도 예외가 아니다."""
    run = assets_repo.start_collection_run(
        db,
        account_id=ACCOUNT,
        region=REGION,
        mode="localstack",
        lookback_days=14,
        period_seconds=3600,
    )
    assets_repo.upsert_asset(
        db,
        arn=INSTANCE_ARN,
        asset_type=AssetType.EC2,
        resource_id=INSTANCE,
        account_id=ACCOUNT,
        region=REGION,
        spec={"instance_type": APPLIED_TYPE},
        collection_run_id=run.collection_run_id,
        collected_at=datetime.now(timezone.utc),
    )


def _rolled_back_origin(
    db, *, with_backup=True, with_candidate=True, collected=True, backup_state="running"
):
    """2/2 Status Check가 실패로 갈린 직후 상태 — 되돌려야 하는 원본 1건.

    식별자만 돌려준다. 스캔이 커밋을 하므로 들고 간 ORM 객체는 곧 낡는다.
    """
    if collected:
        _collected_asset(db)
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
                runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
                target_arn=INSTANCE_ARN,
                parameters={"target_instance_type": APPLIED_TYPE},
                evidence_ids=["ev-1"],
                status=CandidateStatus.CLAIMED,
            ),
        )
        candidate_id = candidate.candidate_id
    incidents_repo.update_incident_status(
        db,
        incident.incident_id,
        expected=incident.status,
        next_status=IncidentStatus.ACTION_IN_PROGRESS,
    )
    origin = exec_repo.create_execution(
        db,
        incident_id=incident.incident_id,
        runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING,
        target_arn=INSTANCE_ARN,
        trigger_source=TriggerSource.USER_APPROVAL,
        candidate_id=candidate_id,
    )
    if with_backup:
        record = exec_repo.create_backup_record(
            db,
            execution_id=origin.execution_id,
            target_arn=INSTANCE_ARN,
            backup_type=BackupType.SAVE_INSTANCE_SPEC_JSON.value,
            payload={
                "instance_id": INSTANCE,
                "instance_type": BACKUP_TYPE,
                "state": backup_state,
            },
        )
        exec_repo.bind_backup_record(db, origin.execution_id, record.backup_record_id)
    exec_repo.update_execution_status(
        db,
        origin.execution_id,
        expected=ExecutionStatus.IN_PROGRESS,
        next_status=ExecutionStatus.ROLLBACK_INITIATED,
        error_summary="FAILED: 기동 실패 — 인스턴스 상태가 stopped입니다",
    )
    db.commit()
    return incident.incident_id, origin.execution_id


def reserve_manual_revert(db, incident_id):
    """관제자 [원클릭 원복] 접수 — 자동 발동과 갈라진 진입점(HTTP가 쓰는 것과 같다)."""
    reservation = workflows.reserve_execution(
        db,
        ExecuteActionRequest(
            incident_id=incident_id,
            runbook_id=RunbookId.RUNBOOK_EC2_REVERT_SIZE,
            idempotency_key=str(uuid.uuid4()),
        ),
    )
    db.commit()
    return reservation.response.execution_id


def cycle(db, publish=None):
    return dispatcher.dispatch_pending(db, publish)


def children(db, origin_id):
    return exec_repo.list_rollback_children(db, origin_id)


def only_child(db, origin_id):
    rows = children(db, origin_id)
    assert len(rows) == 1
    return rows[0]


def operations(aws):
    return [name for name, _ in aws.calls]


def statuses(db, incident_id, origin_id):
    incident = incidents_repo.get_incident(db, incident_id)
    origin = exec_repo.get_execution(db, origin_id)
    return incident.status, origin.status


# ------------------------------------------------------------------ 발동


def test_rollback_initiated_origin_grows_a_revert_child(db, aws):
    """되돌려야 한다고 남은 실행이 사람 개입 없이 원복 자식을 낳는다."""
    _, origin_id = _rolled_back_origin(db)

    report = cycle(db)

    assert report.rollback_started == 1
    child = only_child(db, origin_id)
    assert child.runbook_id is RunbookId.RUNBOOK_EC2_REVERT_SIZE
    assert child.trigger_source is TriggerSource.AUTO_ON_FAILURE
    assert child.status is ExecutionStatus.IN_PROGRESS
    # 접수는 실행 행까지다 — AWS는 다음 주기의 실행이 부른다
    assert aws.calls == []


def test_auto_rollback_happens_at_most_once_per_origin(db, aws):
    """원본당 1회다. 관문은 자식의 존재이며 관제자 복구 접수와 같은 관문이다."""
    _, origin_id = _rolled_back_origin(db)

    cycle(db)  # 접수
    cycle(db)  # 실행
    cycle(db)  # 원본은 이미 확정 — 다시 발동하지 않는다

    assert len(children(db, origin_id)) == 1


def test_revert_child_binds_the_backup_it_loaded(db, aws):
    """어느 레코드로 되돌렸는지가 자기 행에 남아야 사후에 검증된다(ADR-0008 §4)."""
    _, origin_id = _rolled_back_origin(db)

    cycle(db)

    origin = exec_repo.get_execution(db, origin_id)
    assert only_child(db, origin_id).backup_record_id == origin.backup_record_id


def test_missing_backup_settles_the_origin_instead_of_retrying(db, aws):
    """되돌릴 값이 없으면 다시 시도해도 답이 같다 — 확정하고 사람을 부른다."""
    incident_id, origin_id = _rolled_back_origin(db, with_backup=False)

    report = cycle(db)

    assert children(db, origin_id) == []
    assert report.closed == 1
    assert statuses(db, incident_id, origin_id) == (
        IncidentStatus.FAILED,
        ExecutionStatus.ROLLBACK_FAILED,
    )


def test_missing_backup_is_logged_as_critical(db, aws, caplog):
    _rolled_back_origin(db, with_backup=False)

    with caplog.at_level("CRITICAL", logger="vigilantis.workflow"):
        cycle(db)

    assert "auto_rollback_abandoned" in caplog.text


# ------------------------------------------------------------------ 실행과 확정


def test_successful_revert_settles_both_child_and_origin(db, aws):
    """자식 SUCCESS면 원본은 ROLLED_BACK이고 인시던트는 종료 판단 대기로 간다."""
    incident_id, origin_id = _rolled_back_origin(db)

    cycle(db)
    cycle(db)

    assert only_child(db, origin_id).status is ExecutionStatus.SUCCESS
    assert statuses(db, incident_id, origin_id) == (
        IncidentStatus.AWAITING_CLOSURE,
        ExecutionStatus.ROLLED_BACK,
    )


def test_revert_restores_the_backup_type_not_the_applied_type(db, aws):
    """원복 값의 원천은 백업 레코드 하나다(ADR-0004 정책 ③)."""
    _rolled_back_origin(db)

    cycle(db)
    cycle(db)

    modify = [
        kwargs
        for name, kwargs in aws.calls
        if name == "modify_instance_attribute" and not kwargs.get("DryRun")
    ]
    assert modify == [{"InstanceId": INSTANCE, "InstanceType": {"Value": BACKUP_TYPE}}]


def test_failed_revert_settles_the_origin_as_rollback_failed(db, aws):
    """되돌리지도 못한 채 끝났으면 종료 판단이 아니라 수동 개입이 남는다."""
    incident_id, origin_id = _rolled_back_origin(db)
    aws(modify_instance_attribute=ClientError(
        {"Error": {"Code": "InvalidParameterValue"}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        "Op",
    ))

    cycle(db)
    cycle(db)

    assert only_child(db, origin_id).status is ExecutionStatus.FAILED
    assert statuses(db, incident_id, origin_id) == (
        IncidentStatus.FAILED,
        ExecutionStatus.ROLLBACK_FAILED,
    )


def test_third_party_drift_stops_the_revert(db, aws):
    """제3자가 그사이 타입을 바꿨으면 덮어쓰지 않는다(ADR-0008 §3-2 ③)."""
    incident_id, origin_id = _rolled_back_origin(db)
    aws(current_type="c5.large")

    cycle(db)
    cycle(db)

    assert only_child(db, origin_id).status is ExecutionStatus.FAILED
    assert statuses(db, incident_id, origin_id)[1] is ExecutionStatus.ROLLBACK_FAILED
    assert "stop_instances" not in operations(aws)


def test_probe_failure_defers_without_settling(db, aws):
    """대조를 못 한 것은 원복 실패가 아니다 — 확정하지 않고 다음 주기가 다시 묻는다."""
    incident_id, origin_id = _rolled_back_origin(db)
    aws(describe_instances=EndpointConnectionError(endpoint_url="https://ec2"))

    cycle(db)
    report = cycle(db)

    assert report.deferred == 1
    assert only_child(db, origin_id).status is ExecutionStatus.IN_PROGRESS
    assert statuses(db, incident_id, origin_id)[1] is ExecutionStatus.ROLLBACK_INITIATED


# ------------------------------------------------------------------ 가드레일


def test_guardrail_rejection_stops_the_revert_without_touching_aws(db, aws):
    """③이 막으면 AWS 변경은 시작되지 않는다 — 거절된 명령으로 자산을 만지지 않는다."""
    _, origin_id = _rolled_back_origin(db, collected=False)

    cycle(db)
    cycle(db)

    assert only_child(db, origin_id).status is ExecutionStatus.FAILED
    assert "stop_instances" not in operations(aws)
    assert "modify_instance_attribute" not in operations(aws)


def test_guardrail_rejection_is_not_retried(db, aws):
    """자동 재시도 없이 CRITICAL 후 수동 개입이다(ADR-0004 정책 ④).

    재시도를 막는 것은 상태가 아니라 자식 행의 존재다 — 원본은 ROLLBACK_FAILED로
    확정되지만, 그 전에 이미 자식이 멱등 관문 노릇을 한다.
    """
    _, origin_id = _rolled_back_origin(db, collected=False)

    cycle(db)
    cycle(db)
    cycle(db)

    assert len(children(db, origin_id)) == 1


def test_guardrail_rejection_is_recorded_for_the_console(db, aws):
    """거절이 로그로만 남으면 "왜 원복이 멈췄는가"를 화면에서 답할 자리가 없다."""
    _, origin_id = _rolled_back_origin(db, collected=False)

    cycle(db)
    cycle(db)

    child = only_child(db, origin_id)
    evaluation = guardrails_repo.latest_for_execution(db, child.execution_id)
    assert evaluation is not None
    assert evaluation.result is GuardrailDecision.FAIL
    assert evaluation.failed_step is GuardrailStep.ARN_MATCH
    # 통과하지 못한 명령은 불변 실행 명령으로 남기지 않는다
    assert evaluation.validated_command is None


def test_passing_guardrail_records_the_validated_command(db, aws):
    _, origin_id = _rolled_back_origin(db)

    cycle(db)
    cycle(db)

    child = only_child(db, origin_id)
    evaluation = guardrails_repo.latest_for_execution(db, child.execution_id)
    assert evaluation.result is GuardrailDecision.PASS
    # 원복 값(instance_type)은 명령에 실리지 않는다 — 원천은 백업 레코드다
    assert set(evaluation.validated_command["parameters"]) == {
        "instance_id",
        "backup_record_id",
        "evidence_id",
    }


# ------------------------------------------------------------------ 중단 복구


def test_interrupted_revert_is_judged_not_rerun(db, aws):
    """단계를 남긴 채 끊긴 원복은 재실행하지 않고 실자산 대조로 확정한다(ADR-0008 §6·§7)."""
    incident_id, origin_id = _rolled_back_origin(db)
    cycle(db)
    child = only_child(db, origin_id)
    # 정지까지 갔다가 프로세스가 끊긴 모양을 만든다
    workflows._step_recorder(db, child.execution_id)(
        ExecutionStepResult(
            sequence=1,
            affected_arn=INSTANCE_ARN,
            step_type=ex.STEP_STOP_INSTANCE,
            aws_operation="ec2.stop_instances",
            status=ExecutionStepStatus.IN_PROGRESS,
            occurred_at=datetime.now(timezone.utc),
        )
    )
    aws(current_type=BACKUP_TYPE)  # 실제로는 되돌아간 뒤에 끊겼다

    report = cycle(db)

    assert report.judged == 1 and report.started == 0
    assert only_child(db, origin_id).status is ExecutionStatus.SUCCESS
    assert statuses(db, incident_id, origin_id)[1] is ExecutionStatus.ROLLED_BACK


# ---------------------------------------------------- 관제자 접수 원복 (PR #256 리뷰 ②)


def test_manual_revert_child_is_bound_to_the_origin_backup(db, aws):
    """관제자 접수 자식도 되돌릴 값의 출처를 자기 행에 결속한다.

    실행(run_revert_size_execution)은 요청도 후보도 보지 않고 **자기 행의
    backup_record_id 하나**만 읽는다(ADR-0004 정책 ③). 접수가 그것을 박아 주지 않으면
    정상 접수된 원복이 실행 단계에서 "원복 근거 없음"으로 실패한다 — 자동 발동에는
    있고 관제자 경로에만 없던 구멍이다.
    """
    incident_id, origin_id = _rolled_back_origin(db)

    child_id = reserve_manual_revert(db, incident_id)

    child = exec_repo.get_execution(db, child_id)
    origin = exec_repo.get_execution(db, origin_id)
    assert child.trigger_source is TriggerSource.USER_APPROVAL
    assert child.parent_execution_id == origin_id
    assert origin.backup_record_id is not None
    assert child.backup_record_id == origin.backup_record_id


def test_manual_revert_runs_and_settles_both(db, aws):
    """접수 → 실행 → 확정. 확정의 근거는 발동 주체가 아니라 자식의 결과다."""
    incident_id, origin_id = _rolled_back_origin(db)
    reserve_manual_revert(db, incident_id)

    cycle(db)

    child = only_child(db, origin_id)
    assert child.trigger_source is TriggerSource.USER_APPROVAL
    assert child.status is ExecutionStatus.SUCCESS
    assert statuses(db, incident_id, origin_id) == (
        IncidentStatus.AWAITING_CLOSURE,
        ExecutionStatus.ROLLED_BACK,
    )


def test_manual_revert_restores_the_backup_type(db, aws):
    """관제자 경로도 백업 값으로 되돌린다 — 원본이 적용한 값이 아니다."""
    incident_id, _ = _rolled_back_origin(db)
    reserve_manual_revert(db, incident_id)

    cycle(db)

    modify = [
        kwargs
        for name, kwargs in aws.calls
        if name == "modify_instance_attribute" and not kwargs.get("DryRun")
    ]
    assert modify == [{"InstanceId": INSTANCE, "InstanceType": {"Value": BACKUP_TYPE}}]


def test_manual_revert_does_not_double_with_auto(db, aws):
    """접수된 원복이 있으면 자동 발동은 걸리지 않는다 — 관문이 자식의 존재라서다."""
    incident_id, origin_id = _rolled_back_origin(db)
    reserve_manual_revert(db, incident_id)

    cycle(db)
    cycle(db)

    assert len(children(db, origin_id)) == 1


# --------------------------------------------- 중단된 원복의 성공 경계 (PR #256 리뷰 ①)


def _interrupted_after_type_restore(db, origin_id):
    """정지 → 타입 원복까지 갔다가 **기동 전에** 끊긴 모양을 만든다."""
    child = only_child(db, origin_id)
    workflows._step_recorder(db, child.execution_id)(
        ExecutionStepResult(
            sequence=1,
            affected_arn=INSTANCE_ARN,
            step_type=ex.STEP_STOP_INSTANCE,
            aws_operation="ec2.stop_instances",
            status=ExecutionStepStatus.IN_PROGRESS,
            occurred_at=datetime.now(timezone.utc),
        )
    )
    return child


def test_interrupted_revert_stopped_before_start_is_not_success(db, aws):
    """타입만 되돌아왔고 인스턴스가 멈춰 있으면 성공이 아니다.

    실행 절차의 마지막 칸은 기동이다(executor.STEP_START_INSTANCE). 그 앞에서 끊기면
    타입은 이미 백업 값이라, 타입만 보는 판정은 자식을 SUCCESS로 닫고 원본까지
    ROLLED_BACK으로 확정한다 — 멈춘 자산을 아무도 다시 보지 않게 된다.
    """
    incident_id, origin_id = _rolled_back_origin(db)
    cycle(db)
    _interrupted_after_type_restore(db, origin_id)
    aws(current_type=BACKUP_TYPE, current_state="stopped")

    report = cycle(db)

    assert report.judged == 1 and report.started == 0
    assert only_child(db, origin_id).status is ExecutionStatus.FAILED
    assert statuses(db, incident_id, origin_id) == (
        IncidentStatus.FAILED,
        ExecutionStatus.ROLLBACK_FAILED,
    )


def test_interrupted_revert_not_restarted_is_critical(db, aws, caplog):
    """멈춘 채 끝난 원복은 수동 개입이 남는다 — 조용히 닫지 않는다."""
    _, origin_id = _rolled_back_origin(db)
    cycle(db)
    _interrupted_after_type_restore(db, origin_id)
    aws(current_type=BACKUP_TYPE, current_state="stopped")

    with caplog.at_level(logging.CRITICAL):
        cycle(db)

    assert [r.message for r in caplog.records if r.levelno == logging.CRITICAL] == [
        "revert_size_not_restarted"
    ]


def test_interrupted_revert_pending_counts_as_started(db, aws):
    """기동 **요청**이 원복 성공의 경계다 — 2/2 Status Check는 묻지 않는다(ADR-0008 §6).

    pending을 성공에서 빼면 방금 켠 인스턴스가 다음 주기에 미완으로 확정된다.
    """
    incident_id, origin_id = _rolled_back_origin(db)
    cycle(db)
    _interrupted_after_type_restore(db, origin_id)
    aws(current_type=BACKUP_TYPE, current_state="pending")

    cycle(db)

    assert only_child(db, origin_id).status is ExecutionStatus.SUCCESS
    assert statuses(db, incident_id, origin_id)[1] is ExecutionStatus.ROLLED_BACK


def test_interrupted_revert_of_a_stopped_instance_succeeds(db, aws):
    """조치 이전에 멈춰 있었으면 멈춰 있는 것이 원복의 완료다.

    되돌려야 할 상태의 원천은 백업 레코드의 state다(ADR-0008 §4). 여기서 실자산 상태만
    보고 running을 요구하면, 원복이 조치 이전에 없던 기동을 만들어 낸 셈이 된다.
    """
    incident_id, origin_id = _rolled_back_origin(db, backup_state="stopped")
    cycle(db)
    _interrupted_after_type_restore(db, origin_id)
    aws(current_type=BACKUP_TYPE, current_state="stopped")

    cycle(db)

    assert only_child(db, origin_id).status is ExecutionStatus.SUCCESS
    assert statuses(db, incident_id, origin_id)[1] is ExecutionStatus.ROLLED_BACK
