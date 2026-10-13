"""디스패치 스캔 레인 테스트 — Auto Scaling 전용 레인과 main 레인의 분할. (Issue #446)

분할이 **선점 이전**에 일어나는가, 해제 제안 스캔이 main만 도는가, 두 레인이 서로의
대기에 묶이지 않는가, 종료 때 같은 스케줄러 하나로 함께 내려가는가를 본다. 실행·판정
본체는 test_dispatcher·test_ec2_autoscaling_workflow의 몫이다.
"""

import asyncio
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402

ASG = RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING
OTHERS = (
    RunbookId.RUNBOOK_EC2_RIGHTSIZING,
    RunbookId.RUNBOOK_NACL_ADD_DENY,
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED,
)
ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc123456789def0"
WAIT = 5  # 데드락 때 테스트가 영원히 매달리지 않게 하는 상한(초)


@pytest.fixture()
def mixed(db, make_incident):
    """ASG 실행 1건과 다른 런북 실행 3건 — 전부 IN_PROGRESS."""
    incident = make_incident(
        db,
        category=IncidentCategory.FINOPS,
        subject_arn=ARN,
        status=IncidentStatus.ACTION_IN_PROGRESS,
    )
    ids = {}
    for runbook in (ASG, *OTHERS):
        ids[runbook] = exec_repo.create_execution(
            db,
            incident_id=incident.incident_id,
            runbook_id=runbook,
            target_arn=ARN,
            trigger_source=TriggerSource.USER_APPROVAL,
        ).execution_id
    db.commit()
    return ids


@pytest.fixture()
def claims(monkeypatch):
    """선점 시도를 기록한다 — 분할이 선점 전이라면 다른 레인의 행은 여기 오지 않는다."""
    seen = []

    def fake_claim(db, execution_id):
        seen.append(execution_id)

    monkeypatch.setattr(dispatcher, "_claim", fake_claim)
    return seen


# --- 분할 ----------------------------------------------------------------------


def test_main_lane_excludes_the_autoscaling_runbook_before_claiming(db, mixed, claims):
    report = dispatcher.dispatch_pending(db, lane=dispatcher.LANE_MAIN)

    assert set(claims) == {mixed[r] for r in OTHERS}
    assert mixed[ASG] not in claims
    assert report.scanned == len(OTHERS) and report.lane == dispatcher.LANE_MAIN


def test_autoscaling_lane_includes_only_the_autoscaling_runbook(db, mixed, claims):
    report = dispatcher.dispatch_pending(db, lane=dispatcher.LANE_AUTOSCALING)

    assert claims == [mixed[ASG]]
    assert report.scanned == 1 and report.lane == dispatcher.LANE_AUTOSCALING


def test_default_lane_is_main(db, mixed, claims):
    report = dispatcher.dispatch_pending(db)

    assert report.lane == dispatcher.LANE_MAIN
    assert mixed[ASG] not in claims


def test_unknown_lane_is_rejected_before_scanning(db, mixed, claims):
    with pytest.raises(ValueError):
        dispatcher.dispatch_pending(db, lane="side")
    assert claims == []


def test_lane_partition_covers_every_registered_runbook_exactly_once():
    for runbook in dispatcher._RUNNERS:
        owners = [lane for lane in dispatcher._LANES if dispatcher._in_lane(runbook, lane)]
        assert len(owners) == 1, runbook


# --- 해제 제안 스캔 · 로깅 --------------------------------------------------------


def test_release_offer_scan_runs_in_the_main_lane_only(db, monkeypatch):
    scans = []

    def fake_pending(db, *, incident_statuses):
        scans.append(1)
        return []

    monkeypatch.setattr(exec_repo, "list_release_offer_pending", fake_pending)

    dispatcher.dispatch_pending(db, lane=dispatcher.LANE_AUTOSCALING)
    assert scans == []
    dispatcher.dispatch_pending(db, lane=dispatcher.LANE_MAIN)
    assert scans == [1]


@pytest.mark.parametrize("lane", [dispatcher.LANE_MAIN, dispatcher.LANE_AUTOSCALING])
def test_cycle_log_carries_the_lane(db, caplog, lane):
    with caplog.at_level(logging.INFO, logger="vigilantis.dispatcher"):
        dispatcher.dispatch_pending(db, lane=lane)

    (record,) = [r for r in caplog.records if r.getMessage() == "dispatch_cycle_done"]
    assert record.lane == lane


# --- 세션 · 동시성 ---------------------------------------------------------------


class RecordingSessions:
    """세션 팩토리 대역 — 주기마다 새 세션을 내주고 닫힘을 기록한다."""

    def __init__(self):
        self.created = []

    def __call__(self):
        session = SimpleNamespace(closed=False)
        session.close = lambda: setattr(session, "closed", True)
        self.created.append(session)
        return session


def test_each_cycle_uses_its_own_session_and_closes_it(monkeypatch):
    sessions = RecordingSessions()
    used = []
    monkeypatch.setattr(
        dispatcher,
        "dispatch_pending",
        lambda db, publish=None, policy=None, lane=dispatcher.LANE_MAIN: used.append(
            (db, lane)
        ),
    )

    dispatcher.run_dispatch_cycle(sessions, None, dispatcher.LANE_MAIN)
    dispatcher.run_dispatch_cycle(sessions, None, dispatcher.LANE_AUTOSCALING)
    dispatcher.run_dispatch_cycle(sessions)

    assert len(sessions.created) == 3 and len({id(s) for s in sessions.created}) == 3
    assert all(s.closed for s in sessions.created)
    assert [lane for _, lane in used] == [
        dispatcher.LANE_MAIN,
        dispatcher.LANE_AUTOSCALING,
        dispatcher.LANE_MAIN,
    ]


def run_with_scheduler(monkeypatch, body):
    """기동한 스케줄러로 body(scheduler)를 돌리고 반드시 내린다 — 잡은 직접 호출한다."""
    monkeypatch.setattr(
        dispatcher,
        "get_settings",
        lambda: SimpleNamespace(DISPATCH_ENABLED=True, DISPATCH_INTERVAL_SECONDS=3600),
    )

    async def main():
        scheduler = dispatcher.start_dispatcher()
        try:
            body(scheduler)
        finally:
            # asyncio 스케줄러의 종료는 루프에 예약된다 — 한 바퀴 돌려 반영시킨다
            await asyncio.sleep(0.05)
            if scheduler.running:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0.05)
        return scheduler

    return asyncio.run(main())


def test_two_jobs_share_one_scheduler_and_never_overlap_themselves(monkeypatch):
    seen = {}

    def body(scheduler):
        jobs = {job.id: job for job in scheduler.get_jobs()}
        seen["ids"] = set(jobs)
        seen["max_instances"] = {job_id: job.max_instances for job_id, job in jobs.items()}

    run_with_scheduler(monkeypatch, body)

    assert seen["ids"] == {dispatcher.JOB_ID, dispatcher.AUTOSCALING_JOB_ID}
    assert set(seen["max_instances"].values()) == {1}


def test_blocked_autoscaling_cycle_does_not_block_the_main_lane(monkeypatch):
    sessions = RecordingSessions()
    monkeypatch.setattr(dispatcher, "get_session_factory", lambda: sessions)
    asg_entered = threading.Event()
    asg_release = threading.Event()
    main_done = threading.Event()
    lanes_in_flight = []

    def fake_dispatch(db, publish=None, policy=None, lane=dispatcher.LANE_MAIN):
        lanes_in_flight.append(lane)
        if lane == dispatcher.LANE_AUTOSCALING:
            asg_entered.set()
            assert asg_release.wait(WAIT), "ASG 레인이 해제되지 않았다"
        else:
            # ASG 레인이 막힌 채로 들어왔을 때만 이 테스트가 의미가 있다
            assert asg_entered.wait(WAIT), "ASG 레인이 시작되지 않았다"
            main_done.set()
        return dispatcher.DispatchReport(lane=lane)

    monkeypatch.setattr(dispatcher, "dispatch_pending", fake_dispatch)
    outcome = {}

    def body(scheduler):
        jobs = {job.id: job for job in scheduler.get_jobs()}
        asg = threading.Thread(target=jobs[dispatcher.AUTOSCALING_JOB_ID].func)
        main = threading.Thread(target=jobs[dispatcher.JOB_ID].func)
        asg.start()
        assert asg_entered.wait(WAIT)
        main.start()
        # ASG 레인이 아직 막혀 있는 동안 main 레인이 끝난다
        outcome["main_finished_while_asg_blocked"] = (
            main_done.wait(WAIT) and not asg_release.is_set() and asg.is_alive()
        )
        asg_release.set()
        asg.join(WAIT)
        main.join(WAIT)
        outcome["alive"] = asg.is_alive() or main.is_alive()

    run_with_scheduler(monkeypatch, body)

    assert outcome["main_finished_while_asg_blocked"] is True
    assert outcome["alive"] is False
    assert set(lanes_in_flight) == {dispatcher.LANE_MAIN, dispatcher.LANE_AUTOSCALING}
    # 레인마다 자기 세션이고 둘 다 닫혔다
    assert len(sessions.created) == 2 and all(s.closed for s in sessions.created)


def test_shutdown_does_not_wait_for_an_inflight_autoscaling_cycle(monkeypatch):
    sessions = RecordingSessions()
    monkeypatch.setattr(dispatcher, "get_session_factory", lambda: sessions)
    entered = threading.Event()
    release = threading.Event()

    def fake_dispatch(db, publish=None, policy=None, lane=dispatcher.LANE_MAIN):
        entered.set()
        assert release.wait(WAIT)
        return dispatcher.DispatchReport(lane=lane)

    monkeypatch.setattr(dispatcher, "dispatch_pending", fake_dispatch)
    state = {}

    def body(scheduler):
        job = scheduler.get_job(dispatcher.AUTOSCALING_JOB_ID)
        worker = threading.Thread(target=job.func)
        worker.start()
        assert entered.wait(WAIT)
        scheduler.shutdown(wait=False)  # main.py lifespan과 같은 호출
        state["still_working"] = worker.is_alive()
        release.set()
        worker.join(WAIT)
        state["finished"] = not worker.is_alive()

    scheduler = run_with_scheduler(monkeypatch, body)

    assert scheduler.running is False
    assert state == {
        "still_working": True,  # 진행 중인 사이클은 취소되지 않고 끝까지 돈다
        "finished": True,
    }
    assert len(sessions.created) == 1 and sessions.created[0].closed


def test_disabled_dispatcher_registers_no_lane(monkeypatch):
    monkeypatch.setattr(
        dispatcher, "get_settings", lambda: SimpleNamespace(DISPATCH_ENABLED=False)
    )

    assert dispatcher.start_dispatcher() is None
