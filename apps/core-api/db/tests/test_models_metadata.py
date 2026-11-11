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
    # 옛 재개 규칙이 남긴 구독 보존(읽기 전용) · 모의 Marketplace 계약 · 이벤트 (Post-MVP T-16 · #528)
    "tenant_subscription_history",
    "mock_marketplace_agreements",
    "mock_marketplace_events",
    # 실제 AWS Marketplace 라이선스 사본(ADR-0012 2차 개정)
    "marketplace_licenses",
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
    # 로그인 기반·사용자 관리 이력 (#492) · 고객사 그룹 · 직원의 담당 그룹 배정 (#572 2번)
    "app_users",
    "tenant_groups",
    "app_user_group_assignments",
    "app_user_feature_permissions",
    "auth_sessions",
    "user_admin_events",
    # 조치 승인 · 종료 판단 감사 기록 (T-2 · #544)
    "audit_events",
    # 고객 설정 · 플랫폼 설정 자원 정리 대기 · 지워진 고객의 파일 바인딩 기동 증거 (T-7)
    "tenant_settings",
    "tenant_settings_cleanups",
    "notification_legacy_routes",
    # #552 — 도입 신청 · 고객 문의 · 내부 수신함 (외부 발송 outbox와 별개)
    "signup_requests",
    "inquiries",
    "inquiry_events",
    "notifications",
    "notification_reads",
    # #565 — 도입 신청 최종 결정 기록
    "signup_request_decisions",
    # TOTP 분실 복구 작업 · 신원 변경 울타리 (#515)
    "totp_recovery_operations",
    "totp_provider_mutations",
    # 고객사별 API 호출 수의 UTC 1시간 버킷 (#572 6번)
    "tenant_api_calls_hourly",
    # #589 — 플랫폼 활동 로그(append-only 통합 기록)
    "platform_events",
    # GuardDuty 탐지 읽기 커서 — 고객사 × 리전 (#588 C)
    "guardduty_finding_cursors",
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
        # 구독 칸의 모양(T-16 · #528) — 계약 TenantSubscription 의 불변식 거울
        "tenants": {
            "ck_tenants_amount_with_currency",
            "ck_tenants_amount_range",
            "ck_tenants_currency_format",
            "ck_tenants_billing_anchor_day_range",
            "ck_tenants_billing_anchor_with_period",
            "ck_tenants_cancel_with_period_end",
            # 투영의 원천(모의 · 실제 Marketplace)
            "ck_tenants_source_iff_subscription",
            "ck_tenants_entitlement_end_with_contract",
            "ck_tenants_mock_projection_shape",
            "ck_tenants_marketplace_projection_shape",
            "ck_tenants_app_free_is_free",
        },
        "mock_marketplace_agreements": {
            "ck_mock_marketplace_agreements_source_is_mock",
            "ck_mock_marketplace_agreements_activation_shape",
            "ck_mock_marketplace_agreements_ended_shape",
            "ck_mock_marketplace_agreements_expired_shape",
            "ck_mock_marketplace_agreements_renewal_disabled_shape",
        },
        "mock_marketplace_events": {"ck_mock_marketplace_events_source_is_mock"},
        "marketplace_licenses": {
            "ck_marketplace_licenses_account_format",
            "ck_marketplace_licenses_plan_is_paid",
            "ck_marketplace_licenses_active_shape",
        },
        "tenant_api_calls_hourly": {
            "ck_tenant_api_calls_hourly_calls_nonnegative",
            "ck_tenant_api_calls_hourly_hour_on_the_hour",
        },
        # 플랫폼 활동 로그의 행 모양(#589) — 계약 PlatformEventRecord 의 불변식 거울
        "platform_events": {
            "ck_platform_events_actor_shape",
            "ck_platform_events_actor_account_shape",
            "ck_platform_events_actor_platform_role_shape",
            "ck_platform_events_target_shape",
            "ck_platform_events_details_object",
            "ck_platform_events_reason_shape",
        },
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


def test_group_assignment_is_composite_key_with_cascading_foreign_keys():
    """배정은 (user_id, group_id) 하나씩이고, 사용자 · 그룹이 지워지면 함께 지워진다(남는 배정 없음)."""
    table = Base.metadata.tables["app_user_group_assignments"]
    assert [c.name for c in table.primary_key.columns] == ["user_id", "group_id"]
    assert {
        (fk.column.table.name, fk.column.name, fk.ondelete) for fk in table.foreign_keys
    } == {("app_users", "user_id", "CASCADE"), ("tenant_groups", "group_id", "CASCADE")}
    assert set(table.columns.keys()) == {"user_id", "group_id"}


def test_tenant_belongs_to_at_most_one_group_that_ungroups_on_delete():
    """고객사는 group_id 칸 하나(최대 한 그룹 · NULL = 담당 그룹 없음)이고, 그룹을 지우면 NULL 로 돌아간다."""
    column = Base.metadata.tables["tenants"].c.group_id
    assert column.nullable is True
    assert {(fk.column.table.name, fk.column.name, fk.ondelete) for fk in column.foreign_keys} == {
        ("tenant_groups", "group_id", "SET NULL")
    }
    groups = Base.metadata.tables["tenant_groups"]
    assert set(groups.columns.keys()) == {"group_id", "name", "created_at", "updated_at"}
    assert "uq_tenant_groups_name" in _constraint_names("tenant_groups")


def test_tenant_api_calls_hourly_is_a_per_hour_bucket_removed_with_its_tenant():
    """칸은 (tenant_id, hour) 하나씩이고, 고객사가 지워지면 통계도 함께 지워진다(#572 6번)."""
    table = Base.metadata.tables["tenant_api_calls_hourly"]
    assert [c.name for c in table.primary_key.columns] == ["tenant_id", "hour"]
    assert {
        (fk.column.table.name, fk.column.name, fk.ondelete) for fk in table.foreign_keys
    } == {("tenants", "tenant_id", "CASCADE")}
    assert set(table.columns.keys()) == {"tenant_id", "hour", "calls"}
    assert table.c.hour.type.timezone is True
    assert "ix_tenant_api_calls_hourly_hour" in {ix.name for ix in table.indexes}


def test_feature_permission_is_composite_key_with_cascading_user_foreign_key():
    """기능 권한은 (user_id, permission) 하나씩이고, 사용자가 지워지면 함께 지워진다(#572 5번)."""
    table = Base.metadata.tables["app_user_feature_permissions"]
    assert [c.name for c in table.primary_key.columns] == ["user_id", "permission"]
    assert {
        (fk.column.table.name, fk.column.name, fk.ondelete) for fk in table.foreign_keys
    } == {("app_users", "user_id", "CASCADE")}
    assert set(table.columns.keys()) == {"user_id", "permission"}


def test_one_open_mock_agreement_per_tenant_is_a_partial_unique_index():
    indexes = {ix.name: ix for ix in Base.metadata.tables["mock_marketplace_agreements"].indexes}
    open_agreement = indexes["uq_mock_marketplace_agreements_open_tenant"]
    assert open_agreement.unique
    assert [c.name for c in open_agreement.columns] == ["tenant_id"]
    assert open_agreement.dialect_options["postgresql"]["where"] is not None


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
