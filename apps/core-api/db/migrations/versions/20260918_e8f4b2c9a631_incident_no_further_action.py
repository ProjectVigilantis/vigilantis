"""SecOps 추가 조치 없는 종료와 선택 사유 (#381)."""

import sqlalchemy as sa
from alembic import op

revision = "e8f4b2c9a631"
down_revision = "c7a9e1d83f24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PG16: 값을 추가한 트랜잭션에서는 새 값을 사용하지 않는다.
    op.execute("ALTER TYPE resolution_judgement ADD VALUE 'NO_FURTHER_ACTION'")
    op.add_column("incidents", sa.Column("resolution_note", sa.String(1000), nullable=True))
    op.create_check_constraint(
        "resolution_note_with_judgement", "incidents",
        "resolution_note IS NULL OR"
        " (resolution IS NOT NULL AND length(btrim(resolution_note)) > 0)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM incidents"
        " WHERE resolution::text = 'NO_FURTHER_ACTION' OR resolution_note IS NOT NULL)"
    )).scalar_one():
        raise RuntimeError("종료 판단·사유가 남아 있어 손실 없는 downgrade가 불가능합니다")
    op.drop_constraint("resolution_note_with_judgement", "incidents", type_="check")
    op.drop_column("incidents", "resolution_note")
    op.execute("ALTER TYPE resolution_judgement RENAME TO resolution_judgement_old")
    op.execute("CREATE TYPE resolution_judgement AS ENUM ('JUSTIFIED')")
    op.execute(
        "ALTER TABLE incidents ALTER COLUMN resolution TYPE resolution_judgement"
        " USING resolution::text::resolution_judgement"
    )
    op.execute("DROP TYPE resolution_judgement_old")
