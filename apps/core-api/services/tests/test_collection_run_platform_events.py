# ==============================================================================
# [파일 설명]  담당: 김승철 (DATA)
# 수집 회차의 플랫폼 활동 로그(#589 B · COLLECTION_RUN) — 실제 PostgreSQL 행으로 본다.
#
# 지키는 것
#   - **회차 상태가 직전과 달라질 때만 한 행이다.** 같은 (고객사, 리전)의 직전 끝난 회차와 상태가 같으면
#     남기지 않고, 그 리전의 첫 회차는 남긴다. 다른 리전 · 다른 고객사의 회차와 끝나지 않은 회차는 비교
#     기준이 아니다.
#   - 행위자는 SYSTEM, 대상은 그 회차, 소속은 회차의 고객사이고, details 는 리전과 회차 상태다. 요청 밖이라
#     요청 문맥 칸은 비어 있다. 자산 하나하나는 남기지 않는다.
#   - 일부 조회를 못 본 회차(PARTIAL)는 끝까지 돈 회차라 성공으로 남는다. 실패 사유 지도(error_summary)는
#     옮기지 않는다.
#   - 리전 전체를 못 수집한 회차(FAILED)만 실패이고 사유는 그 예외의 코드다. 다른 고객사의 회차는 그대로 남는다.
#   - 적재 쪽은 회차를 끝내는 쓰기와 같은 트랜잭션이다 — 되돌려지면 행도 없고, 행을 쓰다 DB 오류가 나면 그
#     리전은 실패한 회차로 끝난다.
#   - 실패 기록 쪽은 활동 로그를 쓰지 못해도 FAILED 회차를 남긴다 — 수집이 멈췄다는 흔적이 사라지지 않는다.
#   - 기록 계약에 맞지 않는 값은 수집을 막지 않는다.
#   - 남이 연 회차(collection_run_id 를 받은 적재)는 이 호출이 끝내지 않으므로 남기지 않는다.
#
# 수집 · 적재 자체는 test_tenant_collection.py · test_persistence_pipeline.py 가 지킨다.
# ==============================================================================

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import services.collector as C  # noqa: E402
from db import models  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from db.repositories import platform_events as platform_events_repo  # noqa: E402
from schemas.assets import AssetInventory, AutoScalingGroupAsset  # noqa: E402
from schemas.collections import CollectionRunStatus  # noqa: E402
from schemas.platform_events import (  # noqa: E402
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventTargetType,
)

from services.tests import test_tenant_collection as harness  # noqa: E402
from services.tests.test_tenant_collection import world  # noqa: E402, F401 — 픽스처

# db·pg_engine 픽스처는 services/tests/conftest.py 가 등록한다

SEOUL, TOKYO = harness.SEOUL, harness.TOKYO
S, FAILED = PlatformEventOutcome.SUCCEEDED, PlatformEventOutcome.FAILED
SUCCESS, PARTIAL, RUN_FAILED = (
    CollectionRunStatus.SUCCESS, CollectionRunStatus.PARTIAL, CollectionRunStatus.FAILED,
)
CFG = {"lookback_days": 14, "period_seconds": 3600}


# ----- 도우미 ---------------------------------------------------------------------------


def _region() -> str:
    """이 테스트만 쓰는 리전 이름 — 소속 없는 회차는 리전으로만 갈리므로, 다른 테스트가 남긴 회차와 섞이지 않게 한다."""
    return f"test-{uuid.uuid4().hex[:12]}"


def _events(db, *, tenant_id=..., run_ids=None, region=None):
    """그 고객사(또는 그 회차 · 리전)의 수집 회차 행 — 시각순."""
    db.expire_all()
    query = select(models.PlatformEvent).where(
        models.PlatformEvent.action == PlatformEventAction.COLLECTION_RUN.value
    )
    if tenant_id is None:
        query = query.where(models.PlatformEvent.tenant_id.is_(None))
    elif tenant_id is not ...:
        query = query.where(models.PlatformEvent.tenant_id == tenant_id)
    if run_ids is not None:
        query = query.where(models.PlatformEvent.target_id.in_([str(r) for r in run_ids]))
    if region is not None:
        query = query.where(models.PlatformEvent.details["region"].astext == region)
    return db.scalars(query.order_by(models.PlatformEvent.occurred_at)).all()


def _is_run_event(event, run, outcome, *, reason=None) -> None:
    """회차 행의 공통 모양 — 시스템이 요청 밖에서 남긴 한 행이고 대상은 그 회차다."""
    assert (event.action, event.outcome) == (PlatformEventAction.COLLECTION_RUN.value, outcome)
    assert event.category is PlatformEventCategory.COLLECTION
    assert event.actor_kind is PlatformActorKind.SYSTEM
    assert (event.actor_user_id, event.actor_account_type, event.actor_platform_role) == (None, None, None)
    assert (event.target_type, event.target_id) == (
        PlatformEventTargetType.COLLECTION_RUN, str(run.collection_run_id),
    )
    assert event.tenant_id == run.tenant_id
    expected = {"region": run.region, "status": run.status.value}
    if reason is not None:
        expected["reason_code"] = reason
    assert event.details == expected
    # 수집 루프는 요청이 아니다 — 세션 · 요청 식별자 · 접속지 · 브라우저 정보가 없다
    assert (event.session_id, event.request_id, event.source_ip, event.user_agent) == (None, None, None, None)


def _inventory(region, **more) -> AssetInventory:
    return AssetInventory(
        account_id=harness.LEGACY_ACCT, region=region, mode="aws",
        collected_at=datetime.now(timezone.utc), lookback_days=14, period_seconds=3600, **more,
    )


def _run(db, run_id) -> models.CollectionRun:
    db.expire_all()
    return db.get(models.CollectionRun, run_id)


def _runs(db, *, region, tenant_id=None) -> list[models.CollectionRun]:
    db.expire_all()
    query = select(models.CollectionRun).where(models.CollectionRun.region == region)
    query = query.where(
        models.CollectionRun.tenant_id.is_(None) if tenant_id is None
        else models.CollectionRun.tenant_id == tenant_id
    )
    return list(db.scalars(query.order_by(models.CollectionRun.started_at)))


def _finish(db, status: CollectionRunStatus, *, region, tenant_id=None) -> str:
    """그 (소유, 리전)에 그 상태로 끝나는 회차 하나를 수집기의 실제 길로 만든다 → 그 회차의 ID.

    객체가 아니라 ID 를 돌려준다 — 실패 기록(_record_failed_region)은 운영처럼 자기 세션을 닫는다."""
    if status is RUN_FAILED:
        C._record_failed_region(
            region, CFG, ClientError({"Error": {"Code": "AccessDenied"}}, "DescribeInstances"),
            lambda: db, tenant_id,
        )
        run = _runs(db, region=region, tenant_id=tenant_id)[-1]
    else:
        failures = {"auto_scaling_groups": "AccessDenied"} if status is PARTIAL else {}
        result = C.persist_inventory(_inventory(region, collector_failures=failures), db, tenant_id=tenant_id)
        run = _run(db, result["collection_run_id"])
    assert run.status is status
    return str(run.collection_run_id)


def _statuses(db, *, region, tenant_id=None) -> list[str]:
    """그 (소유, 리전)에 남은 행의 회차 상태 — 시각순."""
    events = _events(db, tenant_id=tenant_id, region=region)
    return [event.details["status"] for event in events]


# ----- 첫 회차 · 행의 모양 ----------------------------------------------------------------


def test_first_run_of_a_region_is_one_system_row_about_that_run(db, world):  # noqa: F811
    tenant = harness._tenant(db, world, harness.ACCT_A)
    # 자산이 여럿이어도 행은 회차 하나에 하나다
    world.by_tenant[tenant].instances = [harness._instance("i-1"), harness._instance("i-2")]

    C.collect_and_store_tenants()

    [run] = harness._runs(db, tenant)
    assert run.status is SUCCESS
    [event] = _events(db, tenant_id=tenant)
    _is_run_event(event, run, S)


def test_each_region_of_a_round_starts_with_its_own_row(db, world):  # noqa: F811
    tenant = harness._tenant(db, world, harness.ACCT_A, regions=(SEOUL, TOKYO))

    C.collect_and_store_tenants()

    runs = {run.region: run for run in harness._runs(db, tenant)}
    events = {event.details["region"]: event for event in _events(db, tenant_id=tenant)}
    assert set(runs) == set(events) == {SEOUL, TOKYO}
    for region, run in runs.items():
        _is_run_event(events[region], run, S)


# ----- 상태가 달라질 때만 -----------------------------------------------------------------


def test_rounds_that_keep_succeeding_leave_only_the_first_row(db, world):  # noqa: F811
    tenant = harness._tenant(db, world, harness.ACCT_A)

    for _ in range(3):
        C.collect_and_store_tenants()

    runs = harness._runs(db, tenant)
    assert [run.status for run in runs] == [SUCCESS] * 3
    [event] = _events(db, tenant_id=tenant)
    _is_run_event(event, runs[0], S)


@pytest.mark.parametrize("status", [SUCCESS, PARTIAL, RUN_FAILED], ids=lambda status: status.value)
def test_same_status_in_a_row_adds_no_row(db, status):
    region = _region()
    first = _finish(db, status, region=region)
    _finish(db, status, region=region)
    _finish(db, status, region=region)

    assert len(_runs(db, region=region)) == 3
    [event] = _events(db, tenant_id=None, region=region)
    assert event.target_id == first


def test_each_change_of_status_is_a_row_about_the_run_that_changed_it(db):
    region = _region()
    sequence = [SUCCESS, SUCCESS, RUN_FAILED, RUN_FAILED, SUCCESS, PARTIAL, PARTIAL, SUCCESS]
    runs = [_finish(db, status, region=region) for status in sequence]

    events = _events(db, tenant_id=None, region=region)
    # 수집이 「이때부터 이 상태」다 — 성공 → 실패 → 성공 → 부분 → 성공
    assert [event.details["status"] for event in events] == [
        "SUCCESS", "FAILED", "SUCCESS", "PARTIAL", "SUCCESS",
    ]
    assert [event.target_id for event in events] == [runs[index] for index in (0, 2, 4, 5, 7)]
    # 부분 수집은 끝까지 돈 회차라 결과가 성공이다 — 성공 ↔ 부분의 전환은 상태 칸으로만 갈린다
    assert [event.outcome for event in events] == [S, FAILED, S, S, S]


def test_runs_of_another_region_or_tenant_are_not_the_previous_run(db, world):  # noqa: F811
    here, elsewhere = _region(), _region()
    mine = harness._tenant(db, world, harness.ACCT_A)
    other = harness._tenant(db, world, harness.ACCT_B, offset=1)

    _finish(db, SUCCESS, region=here, tenant_id=mine)
    # 같은 고객사의 다른 리전 · 다른 고객사의 같은 리전 · 소속 없는 같은 리전 — 저마다 자기 첫 회차다
    _finish(db, RUN_FAILED, region=elsewhere, tenant_id=mine)
    _finish(db, RUN_FAILED, region=here, tenant_id=other)
    _finish(db, RUN_FAILED, region=here)
    # 남의 실패가 끼어 있어도 이 (고객사, 리전)의 직전 회차는 성공이다 — 다시 성공하면 남지 않는다
    _finish(db, SUCCESS, region=here, tenant_id=mine)

    assert _statuses(db, region=here, tenant_id=mine) == ["SUCCESS"]
    assert _statuses(db, region=elsewhere, tenant_id=mine) == ["FAILED"]
    assert _statuses(db, region=here, tenant_id=other) == ["FAILED"]
    assert _statuses(db, region=here) == ["FAILED"]


def test_unfinished_run_is_not_the_previous_run(db):
    region = _region()
    # 도중에 죽어 IN_PROGRESS 로 남은 회차가 첫 회차 앞에도, 두 회차 사이에도 있다
    for _ in range(2):
        assets_repo.start_collection_run(
            db, account_id=harness.LEGACY_ACCT, region=region, mode="aws", lookback_days=14, period_seconds=3600
        )
        _finish(db, SUCCESS, region=region)
    stale = assets_repo.start_collection_run(
        db, account_id=harness.LEGACY_ACCT, region=region, mode="aws", lookback_days=14, period_seconds=3600
    )
    _finish(db, RUN_FAILED, region=region)

    assert _run(db, stale.collection_run_id).status is CollectionRunStatus.IN_PROGRESS
    # 끝난 회차끼리만 견준다 — 성공(첫 회차) · 성공(그대로) · 실패(바뀜)
    assert _statuses(db, region=region) == ["SUCCESS", "FAILED"]


# ----- 회차 상태와 결과 -------------------------------------------------------------------


def test_partial_run_is_a_succeeded_row_that_carries_the_status_only(db):
    region = _region()
    inventory = _inventory(
        region,
        auto_scaling_groups=[
            AutoScalingGroupAsset(
                arn=f"arn:aws:autoscaling:{region}:{harness.LEGACY_ACCT}:autoScalingGroup:u:autoScalingGroupName/g",
                name="g", region=region, min_size=0, max_size=1, desired_capacity=0,
                health_check_type="EC2", instance_ids=[], launch_template_id=None,
            )
        ],
        collector_failures={"auto_scaling_groups": "AccessDenied"},
    )

    result = C.persist_inventory(inventory, db)

    run = _run(db, result["collection_run_id"])
    assert run.status is PARTIAL and run.error_summary
    [event] = _events(db, run_ids=[run.collection_run_id])
    # 못 본 조회가 있어도 끝까지 돈 회차다 — 성공이고, 무엇을 못 봤는지는 회차 행에만 있다
    _is_run_event(event, run, S)
    assert "AccessDenied" not in str(event.details)


def test_legacy_run_without_a_tenant_is_a_tenantless_row(db):
    result = C.persist_inventory(_inventory(_region()), db)

    run = _run(db, result["collection_run_id"])
    assert run.tenant_id is None
    [event] = _events(db, tenant_id=None, run_ids=[run.collection_run_id])
    _is_run_event(event, run, S)


def test_failed_region_is_a_failed_row_with_the_error_code_and_the_next_tenant_goes_on(db, world):  # noqa: F811
    failing = harness._tenant(db, world, harness.ACCT_A)
    healthy = harness._tenant(db, world, harness.ACCT_B, offset=1)
    world.by_tenant[failing].fail_with = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not shown"}}, "DescribeInstances"
    )

    C.collect_and_store_tenants()

    [failed_run] = harness._runs(db, failing)
    assert failed_run.status is RUN_FAILED
    [failed_event] = _events(db, tenant_id=failing)
    _is_run_event(failed_event, failed_run, FAILED, reason="AccessDenied")
    assert "not shown" not in str(failed_event.details)  # 제공자 메시지는 싣지 않는다
    [healthy_run] = harness._runs(db, healthy)
    [healthy_event] = _events(db, tenant_id=healthy)
    _is_run_event(healthy_event, healthy_run, S)


def test_failure_whose_code_does_not_fit_the_reason_format_falls_back_to_the_class_name(db):
    class Odd(Exception):
        reason_code = "has spaces and 한글"

    region = _region()
    C._record_failed_region(region, CFG, Odd(), lambda: db)

    [event] = _events(db, tenant_id=None, region=region)
    assert (event.outcome, event.details["reason_code"]) == (FAILED, "Odd")


# ----- 적재 쪽은 회차를 끝내는 쓰기와 같은 트랜잭션 ---------------------------------------------


def test_row_is_rolled_back_with_the_run(db):
    savepoint = db.begin_nested()
    result = C.persist_inventory(_inventory(_region()), db)
    run_id = result["collection_run_id"]
    assert len(_events(db, run_ids=[run_id])) == 1

    savepoint.rollback()

    assert _run(db, run_id) is None and _events(db, run_ids=[run_id]) == []


def test_region_ends_as_a_failed_run_when_the_row_cannot_be_written(db, world, monkeypatch):  # noqa: F811
    tenant = harness._tenant(db, world, harness.ACCT_A)
    world.by_tenant[tenant].instances = [harness._instance("i-1")]
    original = platform_events_repo.record_platform_event
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # 적재 트랜잭션의 쓰기만 실패한다
            raise OperationalError("event write", {}, Exception("not exposed"))
        return original(*args, **kwargs)

    monkeypatch.setattr(platform_events_repo, "record_platform_event", fail_once)

    [summary] = C.collect_and_store_tenants()

    # 행을 못 쓴 적재는 통째로 되돌려지고, 그 리전은 실패한 회차로 끝난다
    assert summary["status"] == "FAILED"
    [run] = harness._runs(db, tenant)
    assert run.status is RUN_FAILED
    assert harness._assets(db, tenant) == {}
    [event] = _events(db, tenant_id=tenant)
    _is_run_event(event, run, FAILED, reason="OperationalError")


# ----- 실패 기록 쪽은 활동 로그를 못 써도 회차를 남긴다 -------------------------------------------


@pytest.mark.parametrize("broken", ["write", "lookup", "database"])
def test_failed_run_is_kept_when_the_activity_log_keeps_failing(db, world, monkeypatch, broken):  # noqa: F811
    """활동 로그가 일시 오류가 아니라 계속 실패해도, 수집이 실패했다는 회차 행은 사라지지 않는다(PR 리뷰).

    적재가 되돌려진 뒤 실패 기록까지 같은 이유로 되돌려지면 collection_runs 에 흔적이 없어, 화면에는 마지막으로
    성공한 옛 회차가 계속 보인다."""
    tenant = harness._tenant(db, world, harness.ACCT_A)
    world.by_tenant[tenant].instances = [harness._instance("i-1")]

    def fail(*args, **kwargs):
        raise OperationalError("activity log", {}, Exception("not exposed"))

    def violate(_mapper, _connection, target):
        # DB 가 실제로 거절하게 만든다(reason_shape CHECK) — 그 순간 트랜잭션이 깨지므로, savepoint 없이 오류만
        # 삼켜서는 뒤따르는 commit 이 실패해 회차 행을 잃는다
        target.details = {} if target.outcome is FAILED else {"reason_code": "X"}

    if broken == "write":
        monkeypatch.setattr(platform_events_repo, "record_platform_event", fail)
    elif broken == "lookup":  # 직전 회차를 읽는 조회가 실패해도 같다 — savepoint 안의 일이다
        monkeypatch.setattr(assets_repo, "previous_finished_run_status", fail)
    else:
        sa_event.listen(models.PlatformEvent, "before_insert", violate)
    try:
        [summary] = C.collect_and_store_tenants()
    finally:
        if broken == "database":
            sa_event.remove(models.PlatformEvent, "before_insert", violate)

    assert summary["status"] == "FAILED"
    assert summary["error"] == ("IntegrityError" if broken == "database" else "OperationalError")
    [run] = harness._runs(db, tenant)
    assert run.status is RUN_FAILED and run.finished_at is not None
    assert harness._assets(db, tenant) == {}
    assert _events(db, tenant_id=tenant) == []


def test_value_outside_the_contract_does_not_stop_the_collection(db, world, monkeypatch):  # noqa: F811
    tenant = harness._tenant(db, world, harness.ACCT_A)
    world.by_tenant[tenant].instances = [harness._instance("i-1")]

    def refuse(*args, **kwargs):
        raise ValueError("outside the event contract")

    monkeypatch.setattr(platform_events_repo, "record_platform_event", refuse)

    [summary] = C.collect_and_store_tenants()

    assert summary.get("status") != "FAILED"
    [run] = harness._runs(db, tenant)
    assert run.status is SUCCESS
    assert set(harness._assets(db, tenant)) == {harness._arn(harness.ACCT_A, "i-1")}
    assert _events(db, tenant_id=tenant) == []


# ----- 이 호출이 끝낸 회차만 ---------------------------------------------------------------


def test_run_opened_by_the_caller_is_not_recorded_by_the_load(db):
    region = _region()
    run = assets_repo.start_collection_run(
        db, account_id=harness.LEGACY_ACCT, region=region, mode="aws", lookback_days=14, period_seconds=3600
    )

    C.persist_inventory(_inventory(region), db, collection_run_id=run.collection_run_id)

    # 회차를 연 쪽이 끝낸다 — 적재는 그 회차를 끝내지 않았다
    assert _run(db, run.collection_run_id).status is CollectionRunStatus.IN_PROGRESS
    assert _events(db, run_ids=[run.collection_run_id]) == []


def test_run_that_was_not_finished_by_this_call_is_not_recorded(db, monkeypatch):
    monkeypatch.setattr(assets_repo, "finish_collection_run", lambda *args, **kwargs: False)

    result = C.persist_inventory(_inventory(_region()), db)

    assert _events(db, run_ids=[result["collection_run_id"]]) == []


@pytest.mark.parametrize("status", [SUCCESS, PARTIAL], ids=lambda status: status.value)
def test_only_a_failed_run_is_a_failed_row(db, status):
    region = _region()
    run = assets_repo.start_collection_run(
        db, account_id=harness.LEGACY_ACCT, region=region, mode="aws", lookback_days=14, period_seconds=3600
    )
    C._record_run_event(
        db, collection_run_id=run.collection_run_id, tenant_id=None, region=region, status=status
    )
    [event] = _events(db, run_ids=[run.collection_run_id])
    assert (event.outcome, event.details) == (S, {"region": region, "status": status.value})
