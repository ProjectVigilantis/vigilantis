"""두 레인의 실제 디스패치 통합 — 별도 세션·commit된 행·실제 dispatch_pending. (Issue #446)

test_dispatcher_lanes의 스케줄러 대역 테스트는 래퍼가 서로 독립임만 보인다. 여기서는 각
레인이 **자기 세션으로 실제 스캔·선점·실행**을 하는 동안 ASG 실행이 AWS 호출 도중 멈춰
있어도 main 레인의 실행이 확정되는지, 선점이 겹치지 않는지를 본다.
"""

import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
from db import models  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from schemas.api.actions import ExecutionStatus  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.autoscaling import AutoscalingInspection  # noqa: E402
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from services.aws import executor as ex  # noqa: E402

from tests.test_ec2_autoscaling_workflow import (  # noqa: E402
    INSTANCE_ARN,
    in_progress,
    make_plan,
    step,
)

VOLUME_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:volume/vol-0abc123456789def0"
WAIT = 10


@pytest.fixture()
def committed(pg_engine, make_incident, make_candidate):
    """commit된 시드 — ASG 실행 A(IN_PROGRESS)와 main 실행 B(EBS 삭제). 종료 때 지운다."""
    factory = sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        incident_a = make_incident(
            session,
            category=IncidentCategory.FINOPS,
            subject_arn=INSTANCE_ARN,
            status=IncidentStatus.ACTION_IN_PROGRESS,
        )
        candidate = make_candidate(
            session,
            incident_a,
            runbook_id=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
            target_arn=INSTANCE_ARN,
            status=CandidateStatus.CLAIMED,
        )
        incident_b = make_incident(
            session,
            category=IncidentCategory.FINOPS,
            subject_arn=VOLUME_ARN,
            status=IncidentStatus.ACTION_IN_PROGRESS,
        )
        a = exec_repo.create_execution(
            session,
            incident_id=incident_a.incident_id,
            runbook_id=RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING,
            target_arn=INSTANCE_ARN,
            trigger_source=TriggerSource.USER_APPROVAL,
            candidate_id=candidate.candidate_id,
        )
        b = exec_repo.create_execution(
            session,
            incident_id=incident_b.incident_id,
            runbook_id=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED,
            target_arn=VOLUME_ARN,
            trigger_source=TriggerSource.USER_APPROVAL,
        )
        session.commit()
        ids = {
            "a": a.execution_id,
            "b": b.execution_id,
            "incidents": [incident_a.incident_id, incident_b.incident_id],
        }
    try:
        yield factory, ids
    finally:
        with factory() as session:
            exec_ids = [ids["a"], ids["b"]]
            session.execute(
                delete(models.ExecutionStep).where(
                    models.ExecutionStep.execution_id.in_(exec_ids)
                )
            )
            session.execute(
                delete(models.ActionExecution).where(
                    models.ActionExecution.execution_id.in_(exec_ids)
                )
            )
            session.execute(
                delete(models.RunbookCandidate).where(
                    models.RunbookCandidate.incident_id.in_(ids["incidents"])
                )
            )
            session.execute(
                delete(models.Incident).where(models.Incident.incident_id.in_(ids["incidents"]))
            )
            session.commit()


def _status(factory, execution_id):
    with factory() as session:
        return session.get(models.ActionExecution, execution_id).status


def test_blocked_autoscaling_runner_does_not_hold_back_the_main_lane(
    committed, monkeypatch
):
    factory, ids = committed
    entered = threading.Event()
    release = threading.Event()
    claims = []
    claims_lock = threading.Lock()
    seen_by_prepare = {}

    original_claim = dispatcher._claim

    def recording_claim(db, execution_id):
        with claims_lock:
            claims.append((threading.current_thread().name, execution_id))
        return original_claim(db, execution_id)

    def prepare(target_arn, *, execution_id, min_size, max_size):
        seen_by_prepare["id"] = execution_id
        return make_plan(execution_id, min_size=min_size, max_size=max_size), None, None

    def execute(target_arn, *, plan, record_step):
        record_step(in_progress(1))  # 호출 직전 단계가 commit된 채로 멈춘다
        entered.set()
        assert release.wait(WAIT), "ASG 실행이 해제되지 않았다"
        done = [step(n) for n in (1, 2, 3, 4)]
        for result in done:
            record_step(result)
        return ex.ExecutionOutcome(steps=tuple(done))

    main_ran = []

    def delete_volume(target_arn, *, record_step):
        main_ran.append(target_arn)
        return ex.ExecutionOutcome()

    monkeypatch.setattr(dispatcher, "_claim", recording_claim)
    monkeypatch.setattr(ex, "prepare_ec2_autoscaling_plan", prepare, raising=False)
    monkeypatch.setattr(ex, "execute_ec2_enable_autoscaling", execute, raising=False)
    monkeypatch.setattr(
        ex,
        "inspect_ec2_autoscaling",
        lambda plan: AutoscalingInspection(status="ready", summary="구성 확인"),
        raising=False,
    )
    monkeypatch.setattr(ex, "execute_ebs_delete_unattached", delete_volume)

    reports = {}
    errors = []

    def asg_cycle():
        try:
            reports["asg1"] = dispatcher.run_dispatch_cycle(
                factory, None, dispatcher.LANE_AUTOSCALING
            )
        except BaseException as exc:  # noqa: BLE001 — 스레드 안의 실패를 테스트로 올린다
            errors.append(exc)

    worker = threading.Thread(target=asg_cycle, name="asg")
    worker.start()
    try:
        assert entered.wait(WAIT), "ASG 러너가 시작되지 않았다"
        # ASG 사이클은 AWS 호출 도중에 멈춰 있다 — 그 사이 main 레인이 자기 실행을 확정한다
        threading.current_thread().name = "main"
        reports["main"] = dispatcher.run_dispatch_cycle(factory, None, dispatcher.LANE_MAIN)
        assert worker.is_alive()
        assert _status(factory, ids["b"]) is ExecutionStatus.SUCCESS
        assert _status(factory, ids["a"]) is ExecutionStatus.IN_PROGRESS
        # 멈춰 있는 동안 계획과 첫 단계는 이미 다른 세션에서 보인다
        with factory() as session:
            assert exec_repo.get_autoscaling_plan(session, ids["a"]) is not None
            assert [s.sequence for s in exec_repo.list_steps(session, ids["a"])] == [1]
    finally:
        release.set()
        worker.join(WAIT)
    assert not worker.is_alive() and errors == []

    # 선점은 레인별로 한 번씩, 서로의 행을 건드리지 않는다
    assert sorted(claims) == sorted([("asg", ids["a"]), ("main", ids["b"])])
    assert main_ran == [VOLUME_ARN]
    assert (reports["main"].lane, reports["main"].closed) == (dispatcher.LANE_MAIN, 1)
    assert (reports["asg1"].lane, reports["asg1"].started) == (dispatcher.LANE_AUTOSCALING, 1)
    assert reports["asg1"].awaiting_status_check == 1
    assert _status(factory, ids["a"]) is ExecutionStatus.IN_PROGRESS

    # 다음 ASG 주기가 같은 실행을 재실행 없이 판정한다 — main은 건드리지 않는다
    second = dispatcher.run_dispatch_cycle(factory, None, dispatcher.LANE_AUTOSCALING)
    assert (second.judged, second.closed, second.started) == (1, 1, 0)
    assert _status(factory, ids["a"]) is ExecutionStatus.SUCCESS
    assert main_ran == [VOLUME_ARN]
