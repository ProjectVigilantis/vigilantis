"""MVP 서비스 연결·전환 3종 + 격리 scale-in 보호 결과 칸

1. service_transitions — #446 실행의 서비스 시도. 불변 의도와 원래 경로 스냅숏. 시도는 실행마다
   한 줄로 잇는다: root는 실행당 하나(부분 유니크), 대체는 같은 실행의 시도 하나만(복합 자기 FK ·
   supersedes 유니크 · 자기 대체 금지). leaf는 아무도 대체하지 않은 시도로 유도한다.
2. service_transition_operations — AWS 변경 일지. #446 실행 단위(순번도 실행 단위)이고 시도 소유
   서비스 자원이면 그 시도를 함께 가리킨다(같은 실행의 시도만 — 복합 FK). 호출 전에 PENDING을
   commit하고 결과로 닫는다. ActionExecution·ExecutionStep의 상태·단계 의미와 섞지 않는다.
3. service_transition_observations — typed 관측. 실행 단위, 시도 관측이면 그 시도를 가리킨다.
4. action_executions.scale_in_protection_result — EC2_ISOLATE(PROTECT)·EC2_UNISOLATE(RESTORE)의
   ASG scale-in 보호 결과. 실행 상태·원복 근거 백업과 별개다. 기존 행은 NULL(결과 없음).

Revision ID: c8e2f5a1d704
Revises: b6c1e4a9d853
Create Date: 2026-10-13 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c8e2f5a1d704"
down_revision = "b6c1e4a9d853"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=False)
_JSONB = postgresql.JSONB(astext_type=sa.Text())
_ATTEMPT = ["service_transitions.transition_id", "service_transitions.execution_id"]


def upgrade() -> None:
    op.add_column(
        "action_executions",
        sa.Column("scale_in_protection_result", _JSONB, nullable=True),
    )
    op.create_table(
        "service_transitions",
        sa.Column("transition_id", _UUID, nullable=False),
        sa.Column("execution_id", _UUID, nullable=False),
        sa.Column("supersedes_transition_id", _UUID, nullable=True),
        sa.Column("plan", _JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(plan) = 'object'", name=op.f("ck_service_transitions_plan_is_object")
        ),
        sa.CheckConstraint(
            "supersedes_transition_id IS NULL OR supersedes_transition_id <> transition_id",
            name=op.f("ck_service_transitions_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["action_executions.execution_id"],
            name=op.f("fk_service_transitions_execution_id_action_executions"),
        ),
        sa.PrimaryKeyConstraint("transition_id", name=op.f("pk_service_transitions")),
        sa.UniqueConstraint(
            "transition_id", "execution_id", name=op.f("uq_service_transitions_id_execution")
        ),
        sa.UniqueConstraint(
            "supersedes_transition_id", name=op.f("uq_service_transitions_supersedes")
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_transition_id", "execution_id"],
            _ATTEMPT,
            name=op.f("fk_service_transitions_supersedes_same_execution"),
        ),
    )
    op.create_index(
        "uq_service_transitions_execution_root",
        "service_transitions",
        ["execution_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_transition_id IS NULL"),
    )
    op.create_table(
        "service_transition_operations",
        sa.Column("operation_id", _UUID, nullable=False),
        sa.Column("execution_id", _UUID, nullable=False),
        sa.Column("transition_id", _UUID, nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("command", sa.String(length=32), nullable=False),
        sa.Column("aws_operation", sa.String(length=128), nullable=False),
        sa.Column("resource_ref", sa.String(length=512), nullable=False),
        sa.Column("request", _JSONB, nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response", _JSONB, nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_summary", sa.String(length=1024), nullable=True),
        sa.Column("reconciled_state", sa.String(length=32), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "sequence >= 1", name=op.f("ck_service_transition_operations_sequence_positive")
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'APPLIED', 'NOT_APPLIED', 'UNCERTAIN')",
            name=op.f("ck_service_transition_operations_state_known"),
        ),
        sa.CheckConstraint(
            "(state = 'PENDING') = (finished_at IS NULL)",
            name=op.f("ck_service_transition_operations_pending_unfinished"),
        ),
        sa.CheckConstraint(
            "(reconciled_state IS NULL AND reconciled_at IS NULL)"
            " OR (reconciled_state IN ('EFFECT_OBSERVED', 'EFFECT_NOT_OBSERVED')"
            " AND reconciled_at IS NOT NULL AND state IN ('PENDING', 'UNCERTAIN'))",
            name=op.f("ck_service_transition_operations_reconciled_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["action_executions.execution_id"],
            name=op.f("fk_service_transition_operations_execution_id_action_executions"),
        ),
        sa.ForeignKeyConstraint(
            ["transition_id", "execution_id"],
            _ATTEMPT,
            name=op.f("fk_service_transition_operations_attempt"),
        ),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("pk_service_transition_operations")),
        sa.UniqueConstraint(
            "execution_id",
            "sequence",
            name=op.f("uq_service_transition_operations_execution_sequence"),
        ),
    )
    op.create_table(
        "service_transition_observations",
        sa.Column("observation_id", _UUID, nullable=False),
        sa.Column("execution_id", _UUID, nullable=False),
        sa.Column("transition_id", _UUID, nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", _JSONB, nullable=False),
        sa.CheckConstraint(
            "kind IN ('PLAN', 'VERIFY', 'CUTOVER', 'RETURN', 'RECONCILE', 'CLEANUP')",
            name=op.f("ck_service_transition_observations_kind_known"),
        ),
        sa.CheckConstraint(
            "verdict IN ('PASSED', 'FAILED', 'UNKNOWN')",
            name=op.f("ck_service_transition_observations_verdict_known"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["action_executions.execution_id"],
            name=op.f("fk_service_transition_observations_execution"),
        ),
        sa.ForeignKeyConstraint(
            ["transition_id", "execution_id"],
            _ATTEMPT,
            name=op.f("fk_service_transition_observations_attempt"),
        ),
        sa.PrimaryKeyConstraint(
            "observation_id", name=op.f("pk_service_transition_observations")
        ),
    )
    op.create_index(
        "ix_service_transition_observations_execution_observed",
        "service_transition_observations",
        ["execution_id", sa.text("observed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_service_transition_observations_execution_observed",
        table_name="service_transition_observations",
    )
    op.drop_table("service_transition_observations")
    op.drop_table("service_transition_operations")
    op.drop_index(
        "uq_service_transitions_execution_root",
        table_name="service_transitions",
        postgresql_where=sa.text("supersedes_transition_id IS NULL"),
    )
    op.drop_table("service_transitions")
    op.drop_column("action_executions", "scale_in_protection_result")
