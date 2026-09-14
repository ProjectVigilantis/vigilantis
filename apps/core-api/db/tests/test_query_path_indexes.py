# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# 조회 경로 인덱스 3개와 "최신 1건" 조회 두 함수의 회귀. (Issue #343)
#
# 지키려는 것은 셋이다.
#   1. 마이그레이션이 만든 인덱스와 models 의 Index 선언이 같다 — 한쪽만 고치면 깨진다.
#   2. latest_collection_run_per_region · latest_rule_evaluation_by_asset 이 LATERAL 로
#      바뀐 뒤에도 답이 같다 — 리전·자산별 최신 1건, 없는 쪽은 빠짐, 동시각은 id 큰 쪽.
#   3. 두 조회가 정렬(Sort) 없이 인덱스 첫 항목으로 끝난다 — 회차가 쌓여도 비용이
#      리전·자산 수에만 비례하는 이유가 이것이라, 정렬이 돌아오면 그 보장이 사라진다.
#      "만진 행" 에는 필터로 버린 행도 센다 — 이력이 없거나 묵은 리전에서 다른 리전 행을
#      읽고 버리는 경로(PR #344 리뷰)가 여기 걸린다.
# ==============================================================================

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db import models  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.collections import CollectionRunStatus  # noqa: E402
from schemas.rules import RuleEvaluationResult  # noqa: E402

NOW = datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)

NEW_INDEXES = {
    "collection_runs": "ix_collection_runs_region_started_at",
    "rule_evaluations": "ix_rule_evaluations_asset_evaluated_at",
    "metric_summaries": "ix_metric_summaries_window_end",
}


# --- 1. 마이그레이션 ↔ models ----------------------------------------------------


def test_new_indexes_exist_and_match_models(db):
    """세 인덱스가 DB 에 있고, 컬럼·정렬 방향이 models.Index 선언과 같다."""
    for table, name in NEW_INDEXES.items():
        indexdef = db.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :n"),
            {"t": table, "n": name},
        ).scalar_one_or_none()
        assert indexdef is not None, f"{name} 이 {table} 에 없다 — 마이그레이션을 봐라"

        declared = next(
            idx for idx in models.Base.metadata.tables[table].indexes if idx.name == name
        )
        # 선언은 "col" 또는 text("col DESC") — DB 정의의 괄호 안과 같아야 한다
        expected = ", ".join(
            str(e.text) if hasattr(e, "text") else e.name for e in declared.expressions
        )
        assert indexdef.endswith(f"({expected})"), (indexdef, expected)


# --- 2. 답이 같다 --------------------------------------------------------------------


def _run(db, region, started_at, status=CollectionRunStatus.SUCCESS):
    run = assets_repo.start_collection_run(
        db, account_id="1", region=region, mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    run.started_at = started_at
    run.status = status
    db.flush()
    return run


def test_latest_run_per_region_picks_newest_and_skips_unknown(db):
    old = _run(db, "ap-northeast-2", NOW, CollectionRunStatus.FAILED)
    new = _run(db, "ap-northeast-2", NOW + timedelta(minutes=5))
    us = _run(db, "us-east-1", NOW + timedelta(minutes=1))
    _run(db, "eu-west-1", NOW + timedelta(minutes=9))  # 관제 밖

    got = assets_repo.latest_collection_run_per_region(
        db, regions=["us-east-1", "ap-northeast-2", "ap-northeast-2", "ap-south-1"]
    )
    assert {r.collection_run_id for r in got} == {new.collection_run_id, us.collection_run_id}
    assert old.collection_run_id not in {r.collection_run_id for r in got}
    assert len(got) == 2  # 중복 리전은 한 번, run 없는 ap-south-1 은 빠진다
    assert assets_repo.latest_collection_run_per_region(db, regions=[]) == []
    # regions=None 은 전 리전
    assert len(assets_repo.latest_collection_run_per_region(db)) == 3


def _asset(db, run, arn):
    return assets_repo.upsert_asset(
        db, arn=arn, asset_type=AssetType.EC2, resource_id=arn.rsplit("/", 1)[-1],
        account_id="1", region="r", spec={}, collection_run_id=run.collection_run_id,
        collected_at=NOW,
    )


def _evaluate(db, run, asset, evaluated_at, verdict="UNUSED"):
    return assets_repo.add_rule_evaluation(
        db,
        RuleEvaluationResult.model_validate(
            {
                "asset_arn": asset.arn,
                "collection_run_id": run.collection_run_id,
                "evaluation_status": "COMPLETED",
                "verdict": verdict,
                "health_score": 50,
                "evaluated_at": evaluated_at,
            }
        ),
    )


def test_latest_evaluation_by_asset_newest_then_larger_id(db):
    run1 = _run(db, "r", NOW)
    run2 = _run(db, "r", NOW + timedelta(minutes=5))
    run3 = _run(db, "r", NOW + timedelta(minutes=10))
    a = _asset(db, run1, "arn:aws:ec2:r:1:instance/i-a")
    b = _asset(db, run1, "arn:aws:ec2:r:1:instance/i-b")
    _asset(db, run1, "arn:aws:ec2:r:1:instance/i-never")  # 판정 없음

    _evaluate(db, run1, a, NOW, verdict="COST_CANDIDATE")
    a_new = _evaluate(db, run2, a, NOW + timedelta(minutes=5))
    # 같은 시각 두 건 — 회차만 다르다. id 가 큰 쪽이 최신이다(종전 DISTINCT ON 과 같은 규칙).
    b1 = _evaluate(db, run2, b, NOW + timedelta(minutes=5))
    b2 = _evaluate(db, run3, b, NOW + timedelta(minutes=5))
    b_win = max(b1, b2, key=lambda e: e.rule_evaluation_id)

    got = assets_repo.latest_rule_evaluation_by_asset(db)
    assert set(got) == {a.asset_id, b.asset_id}
    assert got[a.asset_id].rule_evaluation_id == a_new.rule_evaluation_id
    assert got[b.asset_id].rule_evaluation_id == b_win.rule_evaluation_id
    # 단건 조회와 같은 답이다
    assert (
        assets_repo.latest_rule_evaluation(db, b.asset_id).rule_evaluation_id
        == b_win.rule_evaluation_id
    )


# --- 3. 비용이 회차 수가 아니라 리전·자산 수에 비례한다 ------------------------------


def _plan(db, stmt) -> list[str]:
    from sqlalchemy.dialects import postgresql

    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    return [r[0] for r in db.execute(text("EXPLAIN (ANALYZE, COSTS OFF) " + sql))]


def _rows_touched(plan: list[str], table: str) -> int:
    """``table`` 을 읽는 노드가 실제로 만진 행 수 — 내보낸 행(rows × loops)에 **필터로 버린
    행(Rows Removed by Filter × loops)** 을 더한다. 버린 행을 빼고 세면 인덱스를 최신순으로
    훑다가 리전 조건으로 수천 행을 폐기하는 경로가 통과한다(PR #344 리뷰)."""
    import re

    node = re.compile(rf"\bon {table}\b.*rows=(\d+) loops=(\d+)")
    removed = re.compile(r"Rows Removed by Filter: (\d+)")
    touched: list[int] = []
    loops = 0
    for line in plan:
        m = node.search(line)
        if m:
            loops = int(m.group(2))
            touched.append(int(m.group(1)) * loops)
            continue
        r = removed.search(line)
        if r and touched:
            touched[-1] += int(r.group(1)) * loops
    assert touched, "\n".join(plan)
    return max(touched)


def _prepare_planner(db):
    """통계가 없으면 플래너가 표를 비었다고 보고 아무 경로나 고른다 — 실제 크기를 알린 뒤,
    작은 표에서 순차 탐색을 고르지 않게 한다."""
    db.execute(text("ANALYZE assets, collection_runs, rule_evaluations"))
    db.execute(text("SET LOCAL enable_seqscan = off"))


def test_latest_queries_touch_one_row_per_region_and_asset(db):
    """리전 2개 × 회차 40개를 쌓아도 두 조회는 리전·자산당 1행만 만진다(= 2). 종전
    DISTINCT ON 은 인덱스가 있어도 80행을 전부 걸어야 했다 — 그 형태로 되돌아오면
    여기서 잡힌다. (리전이 하나면 플래너가 DISTINCT ON 도 LIMIT 1 로 바꿔 구분이 안 된다.)"""
    regions = ["r1", "r2"]
    runs = [
        _run(db, region, NOW + timedelta(minutes=5 * i))
        for i in range(40)
        for region in regions
    ]
    assets = [_asset(db, runs[0], f"arn:aws:ec2:r:1:instance/i-plan-{k}") for k in range(2)]
    for run in runs:
        for asset in assets:
            _evaluate(db, run, asset, run.started_at)

    _prepare_planner(db)

    assert _rows_touched(_plan(db, assets_repo.latest_run_per_region_stmt(regions)), "collection_runs") == 2
    assert _rows_touched(_plan(db, assets_repo.latest_rule_evaluation_by_asset_stmt()), "rule_evaluations") == 2


def test_stale_or_empty_region_does_not_scan_other_regions(db):
    """이력이 없는 리전·31일 묵은 리전을 조회해도 다른 리전의 행을 읽고 버리지 않는다.

    PR #344 리뷰가 잡은 경로: 플래너가 started_at 단독 인덱스를 최신순으로 훑으며 리전
    필터로 8,640행을 폐기했다. 정렬 키에 id 를 더해 그 인덱스가 정렬을 대신하지 못하게
    했으므로, 어떤 리전 조합에서도 만진 행은 이력이 있는 리전 수를 넘지 않는다.
    """
    for i in range(40):
        _run(db, "fresh", NOW + timedelta(minutes=5 * i))
        _run(db, "stale", NOW - timedelta(days=31) + timedelta(minutes=5 * i))

    _prepare_planner(db)

    stmt = assets_repo.latest_run_per_region_stmt(["fresh", "stale", "never-collected"])
    plan = _plan(db, stmt)
    assert "ix_collection_runs_started_at" not in "\n".join(plan), "\n".join(plan)
    assert "Rows Removed by Filter" not in "\n".join(plan), "\n".join(plan)
    # EXPLAIN 의 rows 는 loop 평균(반올림)이라 3 리전 × 1 = 3 까지가 상한 — 80 이 아니다
    assert _rows_touched(plan, "collection_runs") <= 3, "\n".join(plan)

    got = {r.region: r for r in db.execute(stmt).scalars()}
    assert set(got) == {"fresh", "stale"}
    assert got["stale"].started_at == NOW - timedelta(days=31) + timedelta(minutes=5 * 39)
