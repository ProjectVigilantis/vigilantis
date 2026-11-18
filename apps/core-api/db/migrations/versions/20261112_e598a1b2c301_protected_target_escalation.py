"""notification_events 에 보호 대상 에스컬레이션 사건 추가 (#598 PR1)

보호 태그 대상은 자동 격리를 막고 사람에게 올린다. 그 사실을 실행 없이 남기는 사건 종류
PROTECTED_TARGET_ESCALATION(phase CREATED, 실행 식별자 · 런북 · 실행 상태 없음)을 event_shape CHECK 에
한 갈래로 더한다. trigger_source 는 올리게 만든 자동 격리 시작 사유(High 즉시 선차단 · 1분 타임아웃 격리)이고
reason_code 는 PROTECTED_TAG 다. 표 · 컬럼 · enum 은 바꾸지 않으며 기존 갈래와 outcome_shape 는 그대로다.

downgrade 는 새 사건 행이 있으면 내리지 않는다 — 옛 CHECK 는 이 사건을 표현하지 못하고, 행을 지우면
미발송 알림 의도(outbox)가 사라진다.

Revision ID: e598a1b2c301
Revises: f588d1a2b3c4
Create Date: 2026-11-12 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e598a1b2c301"
down_revision = "f588d1a2b3c4"
branch_labels = None
depends_on = None

_KIND = "PROTECTED_TARGET_ESCALATION"
_CHECK_NAME = "ck_notification_events_event_shape"
_OLD_SHAPE = (
    "(kind = 'HIGH_INCIDENT_CREATED' AND phase = 'CREATED'"
    " AND execution_id IS NULL AND related_execution_id IS NULL"
    " AND risk_level IS NOT NULL AND risk_level = 'HIGH'"
    " AND trigger_source IS NULL AND execution_status IS NULL AND runbook_id IS NULL)"
    " OR (kind IN ('AUTO_ISOLATION', 'AUTO_ROLLBACK')"
    " AND phase IN ('REQUESTED', 'RESULT') AND execution_id IS NOT NULL"
    " AND trigger_source IS NOT NULL AND execution_status IS NOT NULL"
    " AND runbook_id IS NOT NULL"
    " AND ((kind = 'AUTO_ISOLATION'"
    " AND trigger_source IN ('PRE_MITIGATION_0_5S', 'TIMEOUT_ISOLATION_1M')"
    " AND related_execution_id IS NULL)"
    " OR (kind = 'AUTO_ROLLBACK' AND trigger_source = 'AUTO_ON_FAILURE')))"
)
_NEW_SHAPE = (
    _OLD_SHAPE
    + " OR (kind = 'PROTECTED_TARGET_ESCALATION' AND phase = 'CREATED'"
    " AND execution_id IS NULL AND related_execution_id IS NULL"
    " AND runbook_id IS NULL AND execution_status IS NULL"
    " AND trigger_source IS NOT NULL"
    " AND trigger_source IN ('PRE_MITIGATION_0_5S', 'TIMEOUT_ISOLATION_1M')"
    " AND reason_code IS NOT NULL AND reason_code = 'PROTECTED_TAG')"
)


def _replace_check(shape: str) -> None:
    # op.f — 이름은 이미 규약(ck_<table>_<name>)이 적용된 최종 이름이라 다시 접두하지 않는다
    op.drop_constraint(op.f(_CHECK_NAME), "notification_events", type_="check")
    op.create_check_constraint(op.f(_CHECK_NAME), "notification_events", shape)


def upgrade() -> None:
    _replace_check(_NEW_SHAPE)


def downgrade() -> None:
    used = op.get_bind().execute(
        sa.text("SELECT count(*) FROM notification_events WHERE kind = :kind"),
        {"kind": _KIND},
    ).scalar_one()
    if used:
        raise RuntimeError(
            f"notification_events에 {_KIND} 행이 {used}건 있어 내릴 수 없다 — 이전 CHECK는 "
            "이 사건을 표현하지 못하고, 지우면 미발송 알림 의도가 사라진다"
        )
    _replace_check(_OLD_SHAPE)
