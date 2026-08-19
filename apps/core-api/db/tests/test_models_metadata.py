"""ORM 메타데이터 계약 테스트 — DB 없이 항상 실행된다. (Issue #60)

13종 등록·제약 존재·Enum 타입 단일화처럼 모델 정의 자체가 지켜야 하는 형태를
검증한다. 실제 거절 동작(제약 위반)은 test_repositories.py(통합)가 확인한다.
"""

import sys
from pathlib import Path

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from db.models import Base  # noqa: E402

EXPECTED_TABLES = {
    # 자산·인시던트 계열 9종
    "collection_runs",
    "assets",
    "asset_relationships",
    "metric_summaries",
    "rule_evaluations",
    "threat_events",
    "incidents",
    "evidence_items",
    "runbook_candidates",
    # 조치·복구 계열 4종
    "guardrail_evaluations",
    "action_executions",
    "execution_steps",
    "backup_records",
}


def test_thirteen_tables_registered():
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def _constraint_names(table_name: str) -> set[str]:
    return {c.name for c in Base.metadata.tables[table_name].constraints}


def test_row_invariant_constraints_registered():
    """행 불변식 CheckConstraint가 모델에서 빠지면 CI(DB 없음)에서 이 테스트만이 잡는다.
    실제 거절 동작은 test_repositories.py(통합)가 확인한다."""
    expected = {
        "incidents": {
            "ck_incidents_reason_codes_is_array",
            "ck_incidents_category_risk_shape",
            "ck_incidents_summary_lines_len",
            "ck_incidents_wait_deadline_60s",
        },
        "action_executions": {"ck_action_executions_rollback_child_status"},
        "execution_steps": {
            "ck_execution_steps_effect_matches_status",
            "ck_execution_steps_sequence_positive",
        },
        "guardrail_evaluations": {"ck_guardrail_evaluations_exactly_one_reference"},
        "metric_summaries": {"ck_metric_summaries_window_ordered"},
        "rule_evaluations": {"ck_rule_evaluations_health_score_range"},
    }
    for table, names in expected.items():
        assert names <= _constraint_names(table), table


def test_candidate_active_partial_unique_exists():
    indexes = {ix.name: ix for ix in Base.metadata.tables["runbook_candidates"].indexes}
    active = indexes["uq_runbook_candidates_active"]
    assert active.unique
    assert active.dialect_options["postgresql"]["where"] is not None


def test_shared_enum_types_are_single_instances():
    """risk_level·runbook_id처럼 여러 컬럼이 공유하는 PG ENUM은 타입 객체가 1개여야
    CREATE TYPE이 중복 발행되지 않는다."""
    incidents = Base.metadata.tables["incidents"]
    assert incidents.c.initial_risk_level.type is incidents.c.reviewed_risk_level.type

    candidates = Base.metadata.tables["runbook_candidates"]
    executions = Base.metadata.tables["action_executions"]
    assert candidates.c.runbook_id.type is executions.c.runbook_id.type
