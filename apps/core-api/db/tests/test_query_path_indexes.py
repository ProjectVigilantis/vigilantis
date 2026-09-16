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


def test_started_at_only_index_is_gone(db):
    """옛 started_at 단독 인덱스는 지워져 있어야 한다 — 남아 있으면 플래너가 리전 조건을
    필터로 거르는 경쟁 경로가 된다(PR #344 리뷰). 모델 선언과 DB 양쪽에서 확인한다."""
    assert all(
        idx.name != "ix_collection_runs_started_at"
        for idx in models.Base.metadata.tables["collection_runs"].indexes
    )
    assert db.execute(
        text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_collection_runs_started_at'")
    ).scalar_one() == 0


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
    # 같은 시각 두 건 — 회차만 다르다. 동시각이면 id 역순을 고른다 — id 는 UUID v4 라
    # "최신" 이라는 뜻은 없고, 목록·단건이 같은 한 건을 고르게 하는 결정적 규칙일 뿐이다.
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


def _days_of_runs(db, regions: dict[str, str], days: int):
    """``days`` 일치(리전당 days×288행, 5분 간격)를 generate_series 로 넣는다.
    ``regions`` 는 리전 → 과거로 미는 간격."""
    values = ", ".join(f"('{r}', INTERVAL '{back}')" for r, back in regions.items())
    db.execute(
        text(
            f"""
            INSERT INTO collection_runs (collection_run_id, status, account_id, region, mode,
                                         lookback_days, period_seconds, started_at)
            SELECT gen_random_uuid(), CAST(:st AS collection_run_status), '1', v.r, 'aws', 14, 3600,
                   CAST(:now AS timestamptz) - v.back - i * INTERVAL '5 min'
            FROM generate_series(0, :cap) i, (VALUES {values}) v(r, back)
            ORDER BY i DESC
            """
        ),
        {"st": CollectionRunStatus.SUCCESS.value, "now": NOW, "cap": days * 288 - 1},
    )
    db.execute(text("ANALYZE collection_runs"))


def _assert_region_is_an_index_condition(db, regions):
    plan = _plan(db, assets_repo.latest_run_per_region_stmt(regions))
    joined = "\n".join(plan)
    assert "ix_collection_runs_region_started_at" in joined, joined
    assert "Rows Removed by Filter" not in joined, joined  # 0 이면 EXPLAIN 이 이 줄을 아예 안 찍는다
    # EXPLAIN 의 rows 는 loop 평균(반올림)이라 리전 수 × 1 이 상한 — 수천이 아니다
    assert _rows_touched(plan, "collection_runs") <= len(regions), joined


def test_unseen_region_filters_by_index_at_seven_day_scale(db):
    """신선한 리전 둘에 이력 없는 리전을 함께 물어도 리전 조건이 **인덱스 조건**으로 걸린다
    (필터로 버리는 행 0). 작은 규모의 positive 보장이다 — 80행에서도 7일치(리전당 2,016행)
    에서도 플래너는 복합 인덱스를 고른다. 재현 조건은 PR #344 리뷰 그대로다(리전 둘 다 신선,
    플래너 설정 기본값). 묵은 리전은 바로 아래
    test_stale_region_filters_by_index_and_returns_its_own_latest 가 따로 본다.

    ⚠️ 이 테스트는 옛 started_at 단독 인덱스를 되살려도 통과한다 — tie-break 키가 들어온
    뒤로는 이 규모에서 플래너가 복합 인덱스를 고르기 때문이다(저자 실측 049a7be PASSED ·
    PR #344 리뷰 재현 7·30일치 복합 / 60일치부터 옛 인덱스 — 갈리는 규모는 환경·통계에
    따라 다르다). 재도입을 막는 확정적 방어는 구조 가드 test_started_at_only_index_is_gone
    이며, 180일치 회귀는 실행 순서에 의존한다(그 테스트 설명 참조)."""
    _days_of_runs(db, {"fresh-a": "0", "fresh-b": "0"}, 7)
    _assert_region_is_an_index_condition(db, ["fresh-a", "fresh-b", "never-collected"])


def test_stale_region_filters_by_index_and_returns_its_own_latest(db):
    """31일 묵은 리전을 물어도 다른 리전 행을 읽고 버리지 않고, 그 리전의 최신값을 돌려준다."""
    _days_of_runs(db, {"fresh-a": "0", "stale": "31 days"}, 7)
    regions = ["fresh-a", "stale", "never-collected"]
    _assert_region_is_an_index_condition(db, regions)

    got = {r.region: r for r in db.execute(assets_repo.latest_run_per_region_stmt(regions)).scalars()}
    assert set(got) == {"fresh-a", "stale"}
    assert got["fresh-a"].started_at == NOW
    assert got["stale"].started_at == NOW - timedelta(days=31)


def test_unseen_region_filters_by_index_at_180_day_scale(db):
    """옛 인덱스를 지운 상태에서, 규모가 커져도 리전 조건이 **인덱스 조건**으로 걸린다
    (버리는 행 0). 복합 인덱스가 한 층 깊어지는 60일치부터 옛 started_at 단독 인덱스가
    경쟁에서 이기고 365일치에서 다시 뒤집히는 창(60–180일)의 상단인 180일치(리전당 51,840행)
    로 본다 — 옛 인덱스를 재도입하면 이 규모에서 플래너가 그쪽을 골라 이력 없는 리전당
    수만 행(180일치 103,683행)을 버린다(단독 실행 실측 · PR #344 리뷰: 김세혁이 제안한 회귀).

    ⚠️ 이 plan-choice 검증은 **실행 순서에 의존한다** — savepoint 픽스처에서 앞선 테스트의
    ANALYZE 가 pg_class 통계를 in-place 로 남겨(롤백돼도 persist) 플래너 추정이 흔들린다.
    그래서 옛 인덱스 재도입을 **확정적으로** 막는 것은 이 테스트가 아니라 구조 가드
    test_started_at_only_index_is_gone 다. 이 테스트는 제거 상태에서 규모가 커져도 정렬 없이
    인덱스로 끝난다는 positive 보장을 맡는다(제거 상태에선 경쟁 후보가 없어 순서와 무관)."""
    _days_of_runs(db, {"fresh-a": "0", "fresh-b": "0"}, 180)
    _assert_region_is_an_index_condition(db, ["fresh-a", "fresh-b", "never-collected"])
