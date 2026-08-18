"""baseline — ORM 13종 신규 생성 (Issue #60)

빈 PostgreSQL 대상 baseline이다. 이전할 기존 공유 DB가 없으므로 구 assets
구조에서의 데이터 이전은 포함하지 않는다.

Revision ID: e4947bbcd48a
Revises:
Create Date: 2026-08-18 15:10:18.436206
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# PG ENUM 타입 사전 — 값 원천은 packages/schemas의 Enum(모델 _enum()과 동일 값).
# 타입 생성·삭제는 아래 블록이 단일 경로로 수행하고, 컬럼 정의는 create_type=False로
# 재생성을 막는다 (runbook_id·risk_level처럼 여러 컬럼이 한 타입을 공유).
_ENUMS: dict[str, tuple[str, ...]] = {
    'collection_run_status': ('IN_PROGRESS', 'SUCCESS', 'PARTIAL', 'FAILED'),
    'threat_event_type': ('OPEN_IP', 'SSH_BRUTE_FORCE'),
    'asset_type': ('EC2', 'SG', 'NACL', 'EBS', 'AUTO_SCALING_GROUP', 'LAUNCH_TEMPLATE', 'ALB_TARGET_GROUP'),
    'incident_category': ('FINOPS', 'SECOPS'),
    'incident_status': ('ANALYZING', 'AWAITING_APPROVAL', 'ACTION_IN_PROGRESS', 'RESOLVED', 'FAILED'),
    'risk_level': ('HIGH', 'MEDIUM', 'LOW'),
    'response_mode': ('PRE_MITIGATION_0_5S', 'AGENT_WAIT', 'TIMEOUT_ISOLATION_1M'),
    'agent_invocation_status': ('PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'NO_PROPOSAL', 'FAILED'),
    'relation_type': ('SECURED_BY', 'ATTACHED_TO', 'MEMBER_OF', 'USES', 'REGISTERED_IN', 'PROTECTED_BY'),
    'evidence_type': ('METRIC', 'RULE', 'THREAT', 'EXECUTION'),
    'runbook_id': ('RUNBOOK_EC2_ISOLATE', 'RUNBOOK_NACL_ADD_DENY', 'RUNBOOK_NACL_RESTORE', 'RUNBOOK_SG_DELETE_ISOLATED', 'RUNBOOK_EC2_RIGHTSIZING', 'RUNBOOK_EC2_ENABLE_AUTOSCALING', 'RUNBOOK_EBS_DELETE_UNATTACHED', 'RUNBOOK_EC2_UNISOLATE', 'RUNBOOK_SG_RECREATE', 'RUNBOOK_EC2_REVERT_SIZE'),
    'candidate_status': ('PENDING_VALIDATION', 'EXECUTABLE', 'REJECTED', 'CLAIMED', 'INVALIDATED'),
    'execution_status': ('IN_PROGRESS', 'SUCCESS', 'FAILED', 'ROLLBACK_INITIATED', 'ROLLED_BACK', 'ROLLBACK_FAILED'),
    'trigger_source': ('USER_APPROVAL', 'PRE_MITIGATION_0_5S', 'TIMEOUT_ISOLATION_1M', 'AUTO_ON_FAILURE'),
    'execution_step_status': ('IN_PROGRESS', 'SUCCESS', 'FAILED'),
    'execution_effect': ('NOT_APPLIED', 'APPLIED', 'PARTIAL', 'UNKNOWN'),
    'guardrail_validation_context': ('AI_CANDIDATE', 'AUTO_ISOLATION', 'ROLLBACK_EXECUTION'),
    'guardrail_decision': ('PASS', 'FAIL'),
    'guardrail_step': ('SCHEMA_CHECK', 'ACTION_WHITELIST', 'ARN_MATCH', 'AWS_DRY_RUN'),
}


def _pgenum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


revision = 'e4947bbcd48a'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.create_table('collection_runs',
    sa.Column('collection_run_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('status', _pgenum('collection_run_status'), nullable=False),
    sa.Column('account_id', sa.String(length=16), nullable=False),
    sa.Column('region', sa.String(length=32), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('lookback_days', sa.Integer(), nullable=False),
    sa.Column('period_seconds', sa.Integer(), nullable=False),
    sa.Column('error_summary', sa.String(length=1024), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('collection_run_id', name=op.f('pk_collection_runs'))
    )
    op.create_index('ix_collection_runs_started_at', 'collection_runs', ['started_at'], unique=False)
    op.create_table('threat_events',
    sa.Column('threat_event_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('source_event_id', sa.String(length=256), nullable=False),
    sa.Column('event_type', _pgenum('threat_event_type'), nullable=False),
    sa.Column('target_arn', sa.String(length=512), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('deduplication_key', sa.String(length=512), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('threat_event_id', name=op.f('pk_threat_events')),
    sa.UniqueConstraint('deduplication_key', name=op.f('uq_threat_events_deduplication_key'))
    )
    op.create_index('ix_threat_events_source_event_id', 'threat_events', ['source_event_id'], unique=False)
    op.create_index('ix_threat_events_target_arn', 'threat_events', ['target_arn'], unique=False)
    op.create_table('assets',
    sa.Column('asset_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('arn', sa.String(length=512), nullable=False),
    sa.Column('asset_type', _pgenum('asset_type'), nullable=False),
    sa.Column('resource_id', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=True),
    sa.Column('account_id', sa.String(length=16), nullable=False),
    sa.Column('region', sa.String(length=32), nullable=False),
    sa.Column('state', sa.String(length=32), nullable=True),
    sa.Column('spec', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('last_collection_run_id', sa.Uuid(as_uuid=False), nullable=True),
    sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['last_collection_run_id'], ['collection_runs.collection_run_id'], name=op.f('fk_assets_last_collection_run_id_collection_runs')),
    sa.PrimaryKeyConstraint('asset_id', name=op.f('pk_assets')),
    sa.UniqueConstraint('arn', name=op.f('uq_assets_arn'))
    )
    op.create_index('ix_assets_asset_type', 'assets', ['asset_type'], unique=False)
    op.create_index('ix_assets_region', 'assets', ['region'], unique=False)
    op.create_index('ix_assets_resource_id', 'assets', ['resource_id'], unique=False)
    op.create_table('incidents',
    sa.Column('incident_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('title', sa.String(length=256), nullable=True),
    sa.Column('subject_arn', sa.String(length=512), nullable=False),
    sa.Column('category', _pgenum('incident_category'), nullable=False),
    sa.Column('status', _pgenum('incident_status'), nullable=False),
    sa.Column('threat_event_id', sa.Uuid(as_uuid=False), nullable=True),
    sa.Column('initial_risk_level', _pgenum('risk_level'), nullable=True),
    sa.Column('reviewed_risk_level', _pgenum('risk_level'), nullable=True),
    sa.Column('response_mode', _pgenum('response_mode'), nullable=True),
    sa.Column('initial_risk_reason_codes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('summary_lines', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('agent_invocation_status', _pgenum('agent_invocation_status'), nullable=False),
    sa.Column('agent_invocation_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('agent_wait_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('response_deadline_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(agent_wait_started_at IS NULL AND response_deadline_at IS NULL) OR (agent_wait_started_at IS NOT NULL AND response_deadline_at IS NOT NULL AND response_deadline_at = agent_wait_started_at + INTERVAL '60 seconds')", name=op.f('ck_incidents_wait_deadline_60s')),
    sa.CheckConstraint("CASE jsonb_typeof(initial_risk_reason_codes) WHEN 'array' THEN (category = 'SECOPS' AND initial_risk_level IS NOT NULL AND jsonb_array_length(initial_risk_reason_codes) >= 1) OR (category = 'FINOPS' AND initial_risk_level IS NULL AND reviewed_risk_level IS NULL AND response_mode IS NULL AND jsonb_array_length(initial_risk_reason_codes) = 0) ELSE false END", name=op.f('ck_incidents_category_risk_shape')),
    sa.CheckConstraint("jsonb_typeof(initial_risk_reason_codes) = 'array'", name=op.f('ck_incidents_reason_codes_is_array')),
    sa.CheckConstraint("CASE jsonb_typeof(summary_lines) WHEN 'array' THEN jsonb_array_length(summary_lines) IN (0, 3) ELSE false END", name=op.f('ck_incidents_summary_lines_len')),
    sa.ForeignKeyConstraint(['threat_event_id'], ['threat_events.threat_event_id'], name=op.f('fk_incidents_threat_event_id_threat_events')),
    sa.PrimaryKeyConstraint('incident_id', name=op.f('pk_incidents'))
    )
    op.create_index('ix_incidents_agent_invocation_status', 'incidents', ['agent_invocation_status'], unique=False)
    op.create_index('ix_incidents_category', 'incidents', ['category'], unique=False)
    op.create_index('ix_incidents_created_at', 'incidents', ['created_at'], unique=False)
    op.create_index('ix_incidents_status', 'incidents', ['status'], unique=False)
    op.create_index('ix_incidents_subject_arn', 'incidents', ['subject_arn'], unique=False)
    op.create_table('asset_relationships',
    sa.Column('relationship_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('source_asset_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('relation_type', _pgenum('relation_type'), nullable=False),
    sa.Column('target_arn', sa.String(length=512), nullable=False),
    sa.Column('collection_run_id', sa.Uuid(as_uuid=False), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['collection_run_id'], ['collection_runs.collection_run_id'], name=op.f('fk_asset_relationships_collection_run_id_collection_runs')),
    sa.ForeignKeyConstraint(['source_asset_id'], ['assets.asset_id'], name=op.f('fk_asset_relationships_source_asset_id_assets')),
    sa.PrimaryKeyConstraint('relationship_id', name=op.f('pk_asset_relationships')),
    sa.UniqueConstraint('source_asset_id', 'relation_type', 'target_arn', name=op.f('uq_asset_relationships_source_asset_id_relation_type_target_arn'))
    )
    op.create_index('ix_asset_relationships_target_arn', 'asset_relationships', ['target_arn'], unique=False)
    op.create_table('evidence_items',
    sa.Column('evidence_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('incident_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('evidence_type', _pgenum('evidence_type'), nullable=False),
    sa.Column('source_type', sa.String(length=64), nullable=False),
    sa.Column('source_id', sa.String(length=256), nullable=False),
    sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.incident_id'], name=op.f('fk_evidence_items_incident_id_incidents')),
    sa.PrimaryKeyConstraint('evidence_id', name=op.f('pk_evidence_items'))
    )
    op.create_index('ix_evidence_items_incident_id', 'evidence_items', ['incident_id'], unique=False)
    op.create_table('metric_summaries',
    sa.Column('metric_summary_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('asset_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('collection_run_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('cpu_datapoints', sa.Integer(), nullable=False),
    sa.Column('cpu_avg', sa.Float(), nullable=True),
    sa.Column('cpu_max', sa.Float(), nullable=True),
    sa.Column('net_in_avg', sa.Float(), nullable=True),
    sa.Column('net_out_avg', sa.Float(), nullable=True),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('collected_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('window_end >= window_start', name=op.f('ck_metric_summaries_window_ordered')),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.asset_id'], name=op.f('fk_metric_summaries_asset_id_assets')),
    sa.ForeignKeyConstraint(['collection_run_id'], ['collection_runs.collection_run_id'], name=op.f('fk_metric_summaries_collection_run_id_collection_runs')),
    sa.PrimaryKeyConstraint('metric_summary_id', name=op.f('pk_metric_summaries')),
    sa.UniqueConstraint('asset_id', 'collection_run_id', name=op.f('uq_metric_summaries_asset_id_collection_run_id'))
    )
    op.create_table('rule_evaluations',
    sa.Column('rule_evaluation_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('asset_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('collection_run_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('evaluation_status', sa.String(length=32), nullable=False),
    sa.Column('verdict', sa.String(length=32), nullable=True),
    sa.Column('health_score', sa.Integer(), nullable=True),
    sa.Column('skip_reason_code', sa.String(length=64), nullable=True),
    sa.Column('reason', sa.String(length=1024), nullable=True),
    sa.Column('evaluated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('health_score IS NULL OR (health_score >= 0 AND health_score <= 100)', name=op.f('ck_rule_evaluations_health_score_range')),
    sa.ForeignKeyConstraint(['asset_id'], ['assets.asset_id'], name=op.f('fk_rule_evaluations_asset_id_assets')),
    sa.ForeignKeyConstraint(['collection_run_id'], ['collection_runs.collection_run_id'], name=op.f('fk_rule_evaluations_collection_run_id_collection_runs')),
    sa.PrimaryKeyConstraint('rule_evaluation_id', name=op.f('pk_rule_evaluations')),
    sa.UniqueConstraint('asset_id', 'collection_run_id', name=op.f('uq_rule_evaluations_asset_id_collection_run_id'))
    )
    op.create_index('ix_rule_evaluations_verdict', 'rule_evaluations', ['verdict'], unique=False)
    op.create_table('runbook_candidates',
    sa.Column('candidate_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('incident_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('runbook_id', _pgenum('runbook_id'), nullable=False),
    sa.Column('target_arn', sa.String(length=512), nullable=False),
    sa.Column('display_parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('evidence_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', _pgenum('candidate_status'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.incident_id'], name=op.f('fk_runbook_candidates_incident_id_incidents')),
    sa.PrimaryKeyConstraint('candidate_id', name=op.f('pk_runbook_candidates'))
    )
    op.create_index('ix_runbook_candidates_incident_id', 'runbook_candidates', ['incident_id'], unique=False)
    op.create_index('uq_runbook_candidates_active', 'runbook_candidates', ['incident_id', 'runbook_id'], unique=True, postgresql_where=sa.text("status IN ('PENDING_VALIDATION', 'EXECUTABLE', 'CLAIMED')"))
    op.create_table('action_executions',
    sa.Column('execution_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('incident_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('candidate_id', sa.Uuid(as_uuid=False), nullable=True),
    sa.Column('runbook_id', _pgenum('runbook_id'), nullable=False),
    sa.Column('target_arn', sa.String(length=512), nullable=False),
    sa.Column('status', _pgenum('execution_status'), nullable=False),
    sa.Column('trigger_source', _pgenum('trigger_source'), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('deduplication_key', sa.String(length=512), nullable=True),
    sa.Column('parent_execution_id', sa.Uuid(as_uuid=False), nullable=True),
    sa.Column('backup_record_id', sa.Uuid(as_uuid=False), nullable=True),
    sa.Column('validated_command', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error_summary', sa.String(length=1024), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("parent_execution_id IS NULL OR status IN ('IN_PROGRESS', 'SUCCESS', 'FAILED')", name=op.f('ck_action_executions_rollback_child_status')),
    sa.ForeignKeyConstraint(['candidate_id'], ['runbook_candidates.candidate_id'], name=op.f('fk_action_executions_candidate_id_runbook_candidates')),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.incident_id'], name=op.f('fk_action_executions_incident_id_incidents')),
    sa.ForeignKeyConstraint(['parent_execution_id'], ['action_executions.execution_id'], name=op.f('fk_action_executions_parent_execution_id_action_executions')),
    sa.PrimaryKeyConstraint('execution_id', name=op.f('pk_action_executions')),
    sa.UniqueConstraint('deduplication_key', name=op.f('uq_action_executions_deduplication_key')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_action_executions_idempotency_key'))
    )
    op.create_index('ix_action_executions_incident_id', 'action_executions', ['incident_id'], unique=False)
    op.create_index('ix_action_executions_non_terminal', 'action_executions', ['status'], unique=False, postgresql_where=sa.text("status IN ('IN_PROGRESS', 'ROLLBACK_INITIATED')"))
    op.create_table('backup_records',
    sa.Column('backup_record_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('execution_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('target_arn', sa.String(length=512), nullable=False),
    sa.Column('backup_type', sa.String(length=64), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['execution_id'], ['action_executions.execution_id'], name=op.f('fk_backup_records_execution_id_action_executions')),
    sa.PrimaryKeyConstraint('backup_record_id', name=op.f('pk_backup_records'))
    )
    op.create_index('ix_backup_records_target_arn', 'backup_records', ['target_arn'], unique=False)
    # 순환 FK(action_executions ↔ backup_records)는 두 테이블 생성 뒤에만 걸 수 있다.
    # create_table 안의 use_alter FK는 alembic이 사후 ALTER를 만들지 않아 조용히
    # 누락된다 — 명시적 create_foreign_key로 건다.
    op.create_foreign_key(
        'fk_action_executions_backup_record_id_backup_records',
        'action_executions', 'backup_records',
        ['backup_record_id'], ['backup_record_id'],
    )
    op.create_table('execution_steps',
    sa.Column('execution_step_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('execution_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('affected_arn', sa.String(length=512), nullable=False),
    sa.Column('step_type', sa.String(length=64), nullable=False),
    sa.Column('aws_operation', sa.String(length=128), nullable=False),
    sa.Column('status', _pgenum('execution_step_status'), nullable=False),
    sa.Column('effect', _pgenum('execution_effect'), nullable=True),
    sa.Column('aws_request_id', sa.String(length=128), nullable=True),
    sa.Column('result_summary', sa.String(length=1024), nullable=True),
    sa.Column('error_summary', sa.String(length=1024), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("(status = 'IN_PROGRESS' AND effect IS NULL) OR (status = 'SUCCESS' AND effect IS NOT NULL AND effect IN ('APPLIED', 'NOT_APPLIED')) OR (status = 'FAILED' AND effect IS NOT NULL AND effect IN ('NOT_APPLIED', 'PARTIAL', 'UNKNOWN'))", name=op.f('ck_execution_steps_effect_matches_status')),
    sa.CheckConstraint('sequence >= 1', name=op.f('ck_execution_steps_sequence_positive')),
    sa.ForeignKeyConstraint(['execution_id'], ['action_executions.execution_id'], name=op.f('fk_execution_steps_execution_id_action_executions')),
    sa.PrimaryKeyConstraint('execution_step_id', name=op.f('pk_execution_steps')),
    sa.UniqueConstraint('execution_id', 'sequence', name=op.f('uq_execution_steps_execution_id_sequence'))
    )
    op.create_table('guardrail_evaluations',
    sa.Column('guardrail_evaluation_id', sa.Uuid(as_uuid=False), nullable=False),
    sa.Column('validation_context', _pgenum('guardrail_validation_context'), nullable=False),
    sa.Column('candidate_id', sa.Uuid(as_uuid=False), nullable=True),
    sa.Column('execution_id', sa.Uuid(as_uuid=False), nullable=True),
    sa.Column('result', _pgenum('guardrail_decision'), nullable=False),
    sa.Column('failed_step', _pgenum('guardrail_step'), nullable=True),
    sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('validated_command', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('validated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('(candidate_id IS NULL) != (execution_id IS NULL)', name=op.f('ck_guardrail_evaluations_exactly_one_reference')),
    sa.ForeignKeyConstraint(['candidate_id'], ['runbook_candidates.candidate_id'], name=op.f('fk_guardrail_evaluations_candidate_id_runbook_candidates')),
    sa.ForeignKeyConstraint(['execution_id'], ['action_executions.execution_id'], name=op.f('fk_guardrail_evaluations_execution_id_action_executions')),
    sa.PrimaryKeyConstraint('guardrail_evaluation_id', name=op.f('pk_guardrail_evaluations'))
    )
    op.create_index('ix_guardrail_evaluations_candidate_id', 'guardrail_evaluations', ['candidate_id'], unique=False)
    op.create_index('ix_guardrail_evaluations_execution_id', 'guardrail_evaluations', ['execution_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_guardrail_evaluations_execution_id', table_name='guardrail_evaluations')
    op.drop_index('ix_guardrail_evaluations_candidate_id', table_name='guardrail_evaluations')
    op.drop_table('guardrail_evaluations')
    op.drop_table('execution_steps')
    op.drop_constraint(
        'fk_action_executions_backup_record_id_backup_records',
        'action_executions', type_='foreignkey',
    )
    op.drop_index('ix_backup_records_target_arn', table_name='backup_records')
    op.drop_table('backup_records')
    op.drop_index('ix_action_executions_non_terminal', table_name='action_executions', postgresql_where=sa.text("status IN ('IN_PROGRESS', 'ROLLBACK_INITIATED')"))
    op.drop_index('ix_action_executions_incident_id', table_name='action_executions')
    op.drop_table('action_executions')
    op.drop_index('uq_runbook_candidates_active', table_name='runbook_candidates', postgresql_where=sa.text("status IN ('PENDING_VALIDATION', 'EXECUTABLE', 'CLAIMED')"))
    op.drop_index('ix_runbook_candidates_incident_id', table_name='runbook_candidates')
    op.drop_table('runbook_candidates')
    op.drop_index('ix_rule_evaluations_verdict', table_name='rule_evaluations')
    op.drop_table('rule_evaluations')
    op.drop_table('metric_summaries')
    op.drop_index('ix_evidence_items_incident_id', table_name='evidence_items')
    op.drop_table('evidence_items')
    op.drop_index('ix_asset_relationships_target_arn', table_name='asset_relationships')
    op.drop_table('asset_relationships')
    op.drop_index('ix_incidents_subject_arn', table_name='incidents')
    op.drop_index('ix_incidents_status', table_name='incidents')
    op.drop_index('ix_incidents_created_at', table_name='incidents')
    op.drop_index('ix_incidents_category', table_name='incidents')
    op.drop_index('ix_incidents_agent_invocation_status', table_name='incidents')
    op.drop_table('incidents')
    op.drop_index('ix_assets_resource_id', table_name='assets')
    op.drop_index('ix_assets_region', table_name='assets')
    op.drop_index('ix_assets_asset_type', table_name='assets')
    op.drop_table('assets')
    op.drop_index('ix_threat_events_target_arn', table_name='threat_events')
    op.drop_index('ix_threat_events_source_event_id', table_name='threat_events')
    op.drop_table('threat_events')
    op.drop_index('ix_collection_runs_started_at', table_name='collection_runs')
    op.drop_table('collection_runs')
    bind = op.get_bind()
    for name in _ENUMS:
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
