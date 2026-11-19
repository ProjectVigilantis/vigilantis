"""incident report drafts — 인시던트당 최신 보고서 생성본 보관 (#623)

Revision ID: a623c4d5e601
Revises: f598b2c3d402
Create Date: 2026-11-19 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a623c4d5e601"
down_revision = "f598b2c3d402"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_report_drafts",
        sa.Column("incident_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("markdown", sa.LargeBinary(), nullable=False),
        sa.Column("json_body", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "expires_at = generated_at + interval '7 days'",
            name=op.f("ck_incident_report_drafts_expires_after_seven_days"),
        ),
        sa.CheckConstraint(
            "octet_length(markdown) <= 2097152",
            name=op.f("ck_incident_report_drafts_markdown_size_cap"),
        ),
        sa.CheckConstraint(
            "octet_length(json_body) <= 2097152",
            name=op.f("ck_incident_report_drafts_json_body_size_cap"),
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.incident_id"],
            name=op.f("fk_incident_report_drafts_incident_id_incidents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name=op.f("fk_incident_report_drafts_tenant_id_tenants"),
        ),
        sa.PrimaryKeyConstraint("incident_id", name=op.f("pk_incident_report_drafts")),
    )
    op.create_index(
        op.f("ix_incident_report_drafts_tenant_id"), "incident_report_drafts", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_incident_report_drafts_expires_at"), "incident_report_drafts", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_table("incident_report_drafts")
