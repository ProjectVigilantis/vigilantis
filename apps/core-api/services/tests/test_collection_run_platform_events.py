# ==============================================================================
# [파일 설명]  담당: 김승철 (DATA)
# 수집 회차의 플랫폼 활동 로그(#589 B · COLLECTION_RUN) — 실제 PostgreSQL 행으로 본다.
#
# 지키는 것
#   - 끝난 회차마다 한 행이다(자산 하나하나는 남기지 않는다). 행위자는 SYSTEM, 대상은 그 회차, 소속은 회차의
#     고객사이고, details 는 리전과 회차 상태다. 요청 밖이라 요청 문맥 칸은 비어 있다.
#   - 일부 조회를 못 본 회차(PARTIAL)는 끝까지 돈 회차라 성공으로 남는다. 실패 사유 지도(error_summary)는
#     옮기지 않는다.
#   - 리전 전체를 못 수집한 회차(FAILED)만 실패이고 사유는 그 예외의 코드다. 다른 고객사의 회차는 그대로 남는다.
#   - 회차를 끝내는 쓰기와 같은 트랜잭션이다 — 되돌려지면 행도 없다. 행을 쓰다 DB 오류가 나면 그 리전은
#     실패한 회차로 끝난다.
#   - 기록 계약에 맞지 않는 값은 수집을 막지 않는다.
#   - 남이 연 회차(collection_run_id 를 받은 적재)는 이 호출이 끝내지 않으므로 남기지 않는다.
#
# 수집 · 적재 자체는 test_tenant_collection.py · test_persistence_pipeline.py 가 지킨다.
# ==============================================================================

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
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


# ----- 도우미 ---------------------------------------------------------------------------


def _events(db, *, tenant_id=..., run_ids=None):
    """그 고객사(또는 그 회차들)의 수집 회차 행 — 시각순."""
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


def _inventory(region=SEOUL, **more) -> AssetInventory:
    return AssetInventory(
        account_id=harness.LEGACY_ACCT, region=region, mode="aws",
        collected_at=datetime.now(timezone.utc), lookback_days=14, period_seconds=3600, **more,
    )


def _run(db, run_id) -> models.CollectionRun:
    db.expire_all()
    return db.get(models.CollectionRun, run_id)


# ----- 끝난 회차마다 한 행 ----------------------------------------------------------------


def test_finished_run_is_one_system_row_about_that_run(db, world):  # noqa: F811
    tenant = harness._tenant(db, world, harness.ACCT_A)
    # 자산이 여럿이어도 행은 회차 하나에 하나다
    world.by_tenant[tenant].instances = [harness._instance("i-1"), harness._instance("i-2")]

    C.collect_and_store_tenants()

    [run] = harness._runs(db, tenant)
    assert run.status is CollectionRunStatus.SUCCESS
    [event] = _events(db, tenant_id=tenant)
    _is_run_event(event, run, S)


def test_each_region_of_a_round_is_its_own_row(db, world):  # noqa: F811
    tenant = harness._tenant(db, world, harness.ACCT_A, regions=(SEOUL, TOKYO))

    C.collect_and_store_tenants()

    runs = {run.region: run for run in harness._runs(db, tenant)}
    events = {event.details["region"]: event for event in _events(db, tenant_id=tenant)}
    assert set(runs) == set(events) == {SEOUL, TOKYO}
    for region, run in runs.items():
        _is_run_event(events[region], run, S)


def test_every_round_adds_a_row_per_run(db, world):  # noqa: F811
    tenant = harness._tenant(db, world, harness.ACCT_A)

    C.collect_and_store_tenants()
    C.collect_and_store_tenants()

    runs = harness._runs(db, tenant)
    assert len(runs) == 2
    assert sorted(event.target_id for event in _events(db, tenant_id=tenant)) == sorted(
        str(run.collection_run_id) for run in runs
    )


# ----- 회차 상태와 결과 -------------------------------------------------------------------


def test_partial_run_is_a_succeeded_row_that_carries_the_status_only(db):
    inventory = _inventory(
        auto_scaling_groups=[
            AutoScalingGroupAsset(
                arn=f"arn:aws:autoscaling:{SEOUL}:{harness.LEGACY_ACCT}:autoScalingGroup:u:autoScalingGroupName/g",
                name="g", region=SEOUL, min_size=0, max_size=1, desired_capacity=0,
                health_check_type="EC2", instance_ids=[], launch_template_id=None,
            )
        ],
        collector_failures={"auto_scaling_groups": "AccessDenied"},
    )

    result = C.persist_inventory(inventory, db)

    run = _run(db, result["collection_run_id"])
    assert run.status is CollectionRunStatus.PARTIAL and run.error_summary
    [event] = _events(db, run_ids=[run.collection_run_id])
    # 못 본 조회가 있어도 끝까지 돈 회차다 — 성공이고, 무엇을 못 봤는지는 회차 행에만 있다
    _is_run_event(event, run, S)
    assert "AccessDenied" not in str(event.details)


def test_legacy_run_without_a_tenant_is_a_tenantless_row(db):
    result = C.persist_inventory(_inventory(), db)

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
    assert failed_run.status is CollectionRunStatus.FAILED
    [failed_event] = _events(db, tenant_id=failing)
    _is_run_event(failed_event, failed_run, FAILED, reason="AccessDenied")
    assert "not shown" not in str(failed_event.details)  # 제공자 메시지는 싣지 않는다
    [healthy_run] = harness._runs(db, healthy)
    [healthy_event] = _events(db, tenant_id=healthy)
    _is_run_event(healthy_event, healthy_run, S)


def test_failure_whose_code_does_not_fit_the_reason_format_falls_back_to_the_class_name(db):
    class Odd(Exception):
        reason_code = "has spaces and 한글"

    C._record_failed_region(
        SEOUL, {"lookback_days": 14, "period_seconds": 3600}, Odd(), lambda: db
    )

    [event] = _events(db, tenant_id=None)
    assert (event.outcome, event.details["reason_code"]) == (FAILED, "Odd")


# ----- 회차를 끝내는 쓰기와 같은 트랜잭션 ----------------------------------------------------


def test_row_is_rolled_back_with_the_run(db):
    savepoint = db.begin_nested()
    result = C.persist_inventory(_inventory(), db)
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

    # 행을 못 쓴 적재는 통째로 되돌려지고, 그 리전은 실패한 회차로 끝난다 — 끝난 회차와 행의 수가 어긋나지 않는다
    assert summary["status"] == "FAILED"
    [run] = harness._runs(db, tenant)
    assert run.status is CollectionRunStatus.FAILED
    assert harness._assets(db, tenant) == {}
    [event] = _events(db, tenant_id=tenant)
    _is_run_event(event, run, FAILED, reason="OperationalError")


def test_value_outside_the_contract_does_not_stop_the_collection(db, world, monkeypatch):  # noqa: F811
    tenant = harness._tenant(db, world, harness.ACCT_A)
    world.by_tenant[tenant].instances = [harness._instance("i-1")]

    def refuse(*args, **kwargs):
        raise ValueError("outside the event contract")

    monkeypatch.setattr(platform_events_repo, "record_platform_event", refuse)

    [summary] = C.collect_and_store_tenants()

    assert summary.get("status") != "FAILED"
    [run] = harness._runs(db, tenant)
    assert run.status is CollectionRunStatus.SUCCESS
    assert set(harness._assets(db, tenant)) == {harness._arn(harness.ACCT_A, "i-1")}
    assert _events(db, tenant_id=tenant) == []


# ----- 이 호출이 끝낸 회차만 ---------------------------------------------------------------


def test_run_opened_by_the_caller_is_not_recorded_by_the_load(db):
    run = assets_repo.start_collection_run(
        db, account_id=harness.LEGACY_ACCT, region=SEOUL, mode="aws", lookback_days=14, period_seconds=3600
    )

    C.persist_inventory(_inventory(), db, collection_run_id=run.collection_run_id)

    # 회차를 연 쪽이 끝낸다 — 적재는 그 회차를 끝내지 않았다
    assert _run(db, run.collection_run_id).status is CollectionRunStatus.IN_PROGRESS
    assert _events(db, run_ids=[run.collection_run_id]) == []


def test_run_that_was_not_finished_by_this_call_is_not_recorded(db, monkeypatch):
    monkeypatch.setattr(assets_repo, "finish_collection_run", lambda *args, **kwargs: False)

    result = C.persist_inventory(_inventory(), db)

    assert _events(db, run_ids=[result["collection_run_id"]]) == []


@pytest.mark.parametrize("status", [CollectionRunStatus.SUCCESS, CollectionRunStatus.PARTIAL])
def test_only_a_failed_run_is_a_failed_row(db, status):
    run = assets_repo.start_collection_run(
        db, account_id=harness.LEGACY_ACCT, region=TOKYO, mode="aws", lookback_days=14, period_seconds=3600
    )
    C._record_run_event(
        db, collection_run_id=run.collection_run_id, tenant_id=None, region=TOKYO, status=status
    )
    [event] = _events(db, run_ids=[run.collection_run_id])
    assert (event.outcome, event.details) == (S, {"region": TOKYO, "status": status.value})
