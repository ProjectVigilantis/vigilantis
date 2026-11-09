"""trigger_source에 NACL_DENY_EXPIRED_24H 추가 — 차단 24시간 고정 만료 자동 해제 (T-11 · #597)

NACL_ADD_DENY가 넣은 deny 규칙은 운영자가 해제를 승인하기 전까지 영구히 남았다. 차단 실행
finished_at + 24시간이 지나면 Dispatcher가 시스템 발동으로 NACL_RESTORE를 예약하는데, 그 실행의
시작 사유가 기존 4종(관제자 승인 · 선차단 · 타임아웃 격리 · 실패 시 자동 복구) 어디에도 맞지
않아 값을 하나 더한다. 값 원천은 packages/schemas/runbooks.py TriggerSource.

기존 행은 건드리지 않는다. 감사 기록 CHECK(ck_audit_events_actor_matches_trigger — 사람 승인만
사람이 시작)는 SYSTEM 행위자 + 새 값 조합을 그대로 받는다.

PostgreSQL은 enum 값 삭제를 지원하지 않으므로 downgrade는 타입을 새로 만들어 세 컬럼을 옮긴다.
새 값을 쓴 행이 있으면 내리지 않는다 — 이전 어휘는 "만료로 해제됐다"를 표현하지 못하고, 다른
사유로 접으면 감사 기록이 거짓이 된다.

Revision ID: c597d1e2f3a4
Revises: d572a6c1e906
Create Date: 2026-11-09 11:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c597d1e2f3a4"
down_revision = "d572a6c1e906"
branch_labels = None
depends_on = None

_NEW_VALUE = "NACL_DENY_EXPIRED_24H"
_OLD_VALUES = (
    "USER_APPROVAL",
    "PRE_MITIGATION_0_5S",
    "TIMEOUT_ISOLATION_1M",
    "AUTO_ON_FAILURE",
)
# trigger_source 컬럼을 가진 테이블. 셋 다 같은 PG ENUM 타입을 쓴다(db/models.py _enum).
_COLUMNS = ("action_executions", "notification_events", "audit_events")
# 값 리터럴로 trigger_source 를 비교하는 CHECK — 타입을 바꿔 끼우기 전에 떼고 뒤에 다시 건다.
# 두면 ALTER COLUMN TYPE 이 옛 타입으로 굳은 비교식을 새 타입 컬럼에 걸어 연산자 없음으로 실패한다.
_AUDIT_CHECK = (
    "ck_audit_events_actor_matches_trigger",
    "CASE action WHEN 'ACTION_EXECUTION_STARTED'"
    " THEN (trigger_source = 'USER_APPROVAL') = (actor_kind <> 'SYSTEM')"
    " ELSE actor_kind <> 'SYSTEM' END",
)
_NOTIFICATION_CHECK = (
    "ck_notification_events_event_shape",
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
    " OR (kind = 'AUTO_ROLLBACK' AND trigger_source = 'AUTO_ON_FAILURE')))",
)


def upgrade() -> None:
    # PG 12+는 트랜잭션 안에서 ADD VALUE를 허용한다. 같은 트랜잭션에서 그 값을
    # **사용**하는 것만 막히는데, 여기서는 추가만 하고 쓰지 않는다.
    # Python enum 선언 순서와 맞춰 마지막에 붙인다.
    op.execute(f"ALTER TYPE trigger_source ADD VALUE IF NOT EXISTS '{_NEW_VALUE}'")


def downgrade() -> None:
    bind = op.get_bind()
    for table in _COLUMNS:
        used = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE trigger_source::text = :value"),
            {"value": _NEW_VALUE},
        ).scalar_one()
        if used:
            raise RuntimeError(
                f"{table}에 {_NEW_VALUE} 행이 {used}건 있어 내릴 수 없다 — 이전 어휘는 "
                "만료 해제를 표현하지 못한다"
            )
    old_values = ", ".join(f"'{value}'" for value in _OLD_VALUES)
    # op.f — 이름은 이미 규약(ck_<table>_<name>)이 적용된 최종 이름이라 다시 접두하지 않는다
    op.drop_constraint(op.f(_AUDIT_CHECK[0]), "audit_events", type_="check")
    op.drop_constraint(op.f(_NOTIFICATION_CHECK[0]), "notification_events", type_="check")
    op.execute("ALTER TYPE trigger_source RENAME TO trigger_source_old")
    op.execute(f"CREATE TYPE trigger_source AS ENUM ({old_values})")
    for table in _COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN trigger_source TYPE trigger_source"
            " USING trigger_source::text::trigger_source"
        )
    op.execute("DROP TYPE trigger_source_old")
    op.create_check_constraint(op.f(_AUDIT_CHECK[0]), "audit_events", _AUDIT_CHECK[1])
    op.create_check_constraint(
        op.f(_NOTIFICATION_CHECK[0]), "notification_events", _NOTIFICATION_CHECK[1]
    )
