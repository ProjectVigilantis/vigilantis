"""조회 경로 인덱스 3개 (Issue #343) — 5분 스캔으로 쌓이는 표에서 "최신 1건" 을 정렬 없이 찾는다

수집이 5분마다 리전 수만큼 CollectionRun 을, 자산 수만큼 RuleEvaluation·MetricSummary
를 남긴다(하루 288회 × 2리전 × 16자산 = 판정 9,216행/일). 그런데 GET /assets 가 부르는
두 조회는 이 표를 **전부 정렬** 하고 첫 행만 썼다.

- latest_collection_run_per_region — DISTINCT ON (region) ORDER BY started_at DESC.
  ix_collection_runs_started_at 은 region 이 앞에 없어 정렬을 대신하지 못했다.
  그 옛 인덱스는 **여기서 지운다** — 남겨 두면 플래너가 그쪽을 최신순으로 훑다가 리전
  필터로 수천 행을 버리는 경쟁 경로가 된다(PR #344 리뷰: 이력 없는 리전 17,280행 ·
  31일 묵은 리전 8,640행 폐기). 운영 코드에 started_at 단독으로 정렬·범위 조회하는
  문장은 없다. 새 인덱스의 세 번째 키 collection_run_id 는 동시각 tie-break 다.
- latest_rule_evaluation_by_asset — DISTINCT ON (asset_id) ORDER BY evaluated_at DESC.
  (asset_id, collection_run_id) 유니크 인덱스는 evaluated_at 순서를 모른다.

30일치 부하(run 17,280 · 판정 138,240)에서 두 질의가 24ms · 150ms 였다(DB 안,
EXPLAIN ANALYZE 실행 시간 — Python 왕복·ORM 조립을 더한 리포지토리 함수 기준은
18ms · 80ms). 정렬 키와 같은 순서의 복합 인덱스를 두고 repository 를 LATERAL LIMIT 1
로 바꾸면 0.05ms · 0.08ms(DB 안; 함수 기준 2.4ms · 0.9ms) — 자산·리전 수에만
비례하고 회차가 쌓여도 늘지 않는다.

metric_summaries.window_end 는 스케줄러가 매 회차 부르는 fresh_ec2_metric_summaries
(window_end >= 기준 시각)가 요약 전체를 훑지 않게 한다(DB 안 10ms → 0.1ms, 함수 기준
10.2ms → 1.8ms).

incidents 의 (status, created_at)·(category, created_at) 복합 인덱스는 같은 부하에서
이득이 없어(3,000건 · 정렬 0.3ms) 넣지 않았다 — 목록 API 는 페이지네이션 없이 전 행을
돌려주는 것이 비용의 전부라 인덱스 문제가 아니다.

인덱스만 더한다. 데이터·제약은 바뀌지 않는다.

Revision ID: 95956d08c917
Revises: b5e2d7a4c19f
Create Date: 2026-09-14 15:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '95956d08c917'
down_revision = 'b5e2d7a4c19f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'ix_collection_runs_region_started_at',
        'collection_runs',
        ['region', sa.text('started_at DESC'), sa.text('collection_run_id DESC')],
    )
    op.create_index(
        'ix_rule_evaluations_asset_evaluated_at',
        'rule_evaluations',
        ['asset_id', sa.text('evaluated_at DESC'), sa.text('rule_evaluation_id DESC')],
    )
    op.create_index(
        'ix_metric_summaries_window_end',
        'metric_summaries',
        [sa.text('window_end DESC')],
    )
    # 리전 조건을 필터로 거르는 경쟁 경로를 없앤다 — 이력 없는·오래된 리전에서 전 행을
    # 훑던 원인(PR #344 리뷰). 시작 시각 전역 조회가 생기면 그때 다시 둔다.
    # if_exists: 이 리비전을 옛 정의(옛 인덱스를 남기던 판)로 이미 적용한 DB 에서
    # downgrade → upgrade 왕복이 막히지 않게 한다(리뷰 중 제자리 수정한 리비전이라서).
    op.drop_index('ix_collection_runs_started_at', table_name='collection_runs', if_exists=True)


def downgrade() -> None:
    # 인덱스만 되돌린다. 조회 결과는 같고 느려질 뿐이다.
    op.create_index(
        'ix_collection_runs_started_at', 'collection_runs', ['started_at'], if_not_exists=True
    )
    op.drop_index('ix_metric_summaries_window_end', table_name='metric_summaries')
    op.drop_index('ix_rule_evaluations_asset_evaluated_at', table_name='rule_evaluations')
    op.drop_index('ix_collection_runs_region_started_at', table_name='collection_runs')
