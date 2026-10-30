"""ORM 메타데이터 계약 테스트 — DB 없이 항상 실행된다. (Issue #60)

테이블 등록·제약 존재·Enum 타입 단일화처럼 모델 정의 자체가 지켜야 하는 형태를
검증한다. 실제 거절 동작(제약 위반)은 test_repositories.py(통합)가 확인한다.
"""

import sys
from pathlib import Path

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from db.models import Base

EXPECTED_TABLES = {
    # 테넌트 — 고객 계정 (Post-MVP T-4 · #485)
    "tenants",
    # 자산·인시던트 계열 10종
    "collection_runs",
    "asset_inventory_counts",  # 회차별 유형별 자산 수 — 시계열 축 5 (2026-09-29)
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
    # MVP 서비스 연결·전환 3종 (schemas.service_transition)
    "service_transitions",
    "service_transition_operations",
    "service_transition_observations",
    "notification_events",
    # #493 PR2 채널별 발송 상태·현재 수신 바인딩·호출 관문
    "notification_deliveries",
    "notification_routes",
    "notification_lanes",
    # 로그인 기반·사용자 관리 이력·담당 고객사 배정 (#492)
    "app_users",
    "app_user_tenant_assignments",
    "auth_sessions",
    "user_admin_events",
    # 조치 승인 · 종료 판단 감사 기록 (T-2 · #544)
    "audit_events",
    # #552 — 도입 신청 · 고객 문의 · 내부 수신함 (외부 발송 outbox와 별개)
    "signup_requests",
    "inquiries",
    "inquiry_events",
    "notifications",
    "notification_reads",
    # TOTP 분실 복구 작업 · 신원 변경 울타리 (#515)
    "totp_recovery_operations",
    "totp_provider_mutations",
}


def test_tables_registered():
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def _constraint_names(table_name: str) -> set[str]:
    return {c.name for c in Base.metadata.tables[table_name].constraints}


def test_row_invariant_constraints_registered():
    """행 불변식 CheckConstraint가 모델에서 빠지면 CI(DB 없음)에서 이 테스트만이 잡는다.
    실제 거절 동작은 test_repositories.py(통합)가 확인한다."""
    expected = {
        "signup_requests": {"ck_signup_requests_rejection_reason_by_status"},
        "notifications": {"ck_notifications_notification_target_shape"},
        "incidents": {
            "ck_incidents_reason_codes_is_array",
            "ck_incidents_category_risk_shape",
            "ck_incidents_summary_lines_len",
            "ck_incidents_wait_deadline_60s",
        },
        "action_executions": {
            "ck_action_executions_rollback_child_status",
            # 판정 불가 보류 기록의 모양과 UNVERIFIED의 기록 필수 (Issue #249)
            "ck_action_executions_verification_hold_shape",
            "ck_action_executions_unverified_has_hold",
        },
        "execution_steps": {
            "ck_execution_steps_effect_matches_status",
            "ck_execution_steps_sequence_positive",
        },
        "guardrail_evaluations": {"ck_guardrail_evaluations_exactly_one_reference"},
        "metric_summaries": {"ck_metric_summaries_window_ordered"},
        "rule_evaluations": {"ck_rule_evaluations_health_score_range"},
        # 계정 종류 ↔ 소속 · 플랫폼 등급(#492). PLATFORM role=ADMIN 은 호환 제약이다
        "app_users": {
            "ck_app_users_tenant_binding_by_account_type",
            "ck_app_users_platform_role_is_admin",
            "ck_app_users_platform_role_by_account_type",
        },
        # TOTP 분실 복구 작업 — 신원 확인 진술 필수 · 확정 모양 (#515)
        "totp_recovery_operations": {
            "ck_totp_recovery_operations_identity_verified",
            "ck_totp_recovery_operations_generation_positive",
            "ck_totp_recovery_operations_revoked_count_nonnegative",
            "ck_totp_recovery_operations_finalized_shape",
        },
        # 신원 변경 울타리 — 종류별 작업 · 풀림 모양 · 정리 행위자 (#515)
        "totp_provider_mutations": {
            "ck_totp_provider_mutations_operation_by_kind",
            "ck_totp_provider_mutations_resolution_shape",
            "ck_totp_provider_mutations_reconciler_shape",
        },
    }
    for table, names in expected.items():
        assert names <= _constraint_names(table), table


def test_tenant_assignment_is_composite_key_with_cascading_foreign_keys():
    """배정은 (user_id, tenant_id) 하나씩이고, 사용자 · 고객사가 지워지면 함께 지워진다(남는 배정 없음)."""
    table = Base.metadata.tables["app_user_tenant_assignments"]
    assert [c.name for c in table.primary_key.columns] == ["user_id", "tenant_id"]
    assert {
        (fk.column.table.name, fk.column.name, fk.ondelete) for fk in table.foreign_keys
    } == {("app_users", "user_id", "CASCADE"), ("tenants", "tenant_id", "CASCADE")}
    assert set(table.columns.keys()) == {"user_id", "tenant_id"}


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
