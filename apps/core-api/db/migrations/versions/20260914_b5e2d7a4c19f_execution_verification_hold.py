"""action_executions 판정 불가 보류 — UNVERIFIED 상태와 typed 보류 기록 (Issue #249)

2/2 Status Check·원복 전 상태 대조가 AWS에 물어보지 못하면 실행은 IN_PROGRESS로 남아
스캔 주기마다 다시 물었다. 사유는 로그에만 남았고 재시도 상한이 없어, 권한 오류처럼
다시 물어도 답이 같은 실패도 인시던트를 영원히 '조치 진행 중'에 묶었다. 두 가지를 더한다.

  1. execution_status에 UNVERIFIED — 재시도를 소진해 결과를 확정하지 못한 채 자동 판정을
     멈춘 종료 상태. ROLLBACK_INITIATED가 아니므로 자동 원복의 입력이 아니다.
  2. 보류 기록 네 칸 — 사유 코드(precheck_reason_code, 가드레일 ④와 같은 어휘)·누적
     실패 횟수·처음/마지막 실패 시각. 재시도 판단과 관제자 확인이 이 칸을 읽는다.

기존 행은 건드리지 않는다 — 보류 기록은 비어 있고(attempts 0), 새 값은 이 마이그레이션
이후 판정 불가가 난 실행부터 붙는다.

UNVERIFIED 불변식(보류 기록 필수)은 status를 text로 비교한다. PostgreSQL은 ADD VALUE 한
enum 값을 같은 트랜잭션에서 **쓰는** 것을 막는데, 'UNVERIFIED'를 enum으로 캐스트하면 그
사용이 된다.

downgrade는 UNVERIFIED를 #249 이전 표현으로 접는다 — 판정 보류는 IN_PROGRESS로 남아
다음 주기가 다시 묻던 자리였다. 종료 시각을 지우고, 그 실행을 둔 Incident는
ACTION_IN_PROGRESS로 되돌린다(상세 응답 계약이 진행 중 실행과 그 상태를 짝짓는다).
PostgreSQL은 enum 값 삭제를 지원하지 않으므로 execution_status는 타입을 새로 만들어
바꿔 끼운다.

Revision ID: b5e2d7a4c19f
Revises: b2f9d4c81e07
Create Date: 2026-09-14 16:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'b5e2d7a4c19f'
down_revision = 'b2f9d4c81e07'
branch_labels = None
depends_on = None

# 값 원천은 packages/schemas/api/actions.py ExecutionStatus.
_NEW_STATUS = 'UNVERIFIED'
_OLD_STATUSES = (
    'IN_PROGRESS',
    'SUCCESS',
    'FAILED',
    'ROLLBACK_INITIATED',
    'ROLLED_BACK',
    'ROLLBACK_FAILED',
)
# 값 원천은 packages/schemas/guardrails.py PrecheckReasonCode — 선언 순서 그대로.
_REASON_CODES = (
    'PRECHECK_UNAUTHORIZED',
    'PRECHECK_TARGET_NOT_FOUND',
    'PRECHECK_INVALID_STATE',
    'PRECHECK_NOT_IMPLEMENTED',
    'PRECHECK_PARAM_INVALID',
    'PRECHECK_AWS_ERROR',
)
_HOLD_COLUMNS = (
    'verification_reason_code',
    'verification_attempts',
    'verification_first_failed_at',
    'verification_last_failed_at',
)

# 식은 db/models.py ActionExecution.__table_args__와 같아야 한다.
_HOLD_SHAPE = (
    "(verification_attempts = 0 AND verification_reason_code IS NULL"
    " AND verification_first_failed_at IS NULL"
    " AND verification_last_failed_at IS NULL)"
    " OR (verification_attempts >= 1 AND verification_reason_code IS NOT NULL"
    " AND verification_first_failed_at IS NOT NULL"
    " AND verification_last_failed_at IS NOT NULL"
    " AND verification_last_failed_at >= verification_first_failed_at)"
)
_UNVERIFIED_HAS_HOLD = "status::text <> 'UNVERIFIED' OR verification_attempts >= 1"
# downgrade가 타입을 바꿔 끼울 때 떼었다 다시 거는 baseline의 식 두 개
_ROLLBACK_CHILD_STATUS = (
    "parent_execution_id IS NULL OR status IN ('IN_PROGRESS', 'SUCCESS', 'FAILED')"
)
_NON_TERMINAL = "status IN ('IN_PROGRESS', 'ROLLBACK_INITIATED')"


def upgrade() -> None:
    # PG 12+는 트랜잭션 안에서 ADD VALUE를 허용한다. 같은 트랜잭션에서 그 값을
    # **사용**하는 것만 막히는데, 아래 CHECK는 text 비교라 사용이 아니다.
    # Python enum 선언 순서와 맞춰 마지막에 붙인다.
    op.execute(f"ALTER TYPE execution_status ADD VALUE IF NOT EXISTS '{_NEW_STATUS}'")

    bind = op.get_bind()
    postgresql.ENUM(*_REASON_CODES, name='precheck_reason_code').create(
        bind, checkfirst=True
    )
    op.add_column(
        'action_executions',
        sa.Column(
            'verification_reason_code',
            postgresql.ENUM(*_REASON_CODES, name='precheck_reason_code', create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        'action_executions',
        sa.Column(
            'verification_attempts',
            sa.Integer(),
            server_default=sa.text('0'),
            nullable=False,
        ),
    )
    op.add_column(
        'action_executions',
        sa.Column('verification_first_failed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'action_executions',
        sa.Column('verification_last_failed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint('verification_hold_shape', 'action_executions', _HOLD_SHAPE)
    op.create_check_constraint('unverified_has_hold', 'action_executions', _UNVERIFIED_HAS_HOLD)


def downgrade() -> None:
    # 1. UNVERIFIED 행을 #249 이전 표현으로 접는다 — 타입을 바꾸기 전, 새 값이 아직 있을 때.
    #    Incident를 먼저 옮긴다 — 실행을 먼저 접으면 어느 Incident가 대상인지 잃는다
    op.execute(
        "UPDATE incidents SET status = 'ACTION_IN_PROGRESS', resolution = NULL,"
        " resolved_at = NULL WHERE incident_id IN (SELECT incident_id FROM"
        f" action_executions WHERE status::text = '{_NEW_STATUS}')"
    )
    op.execute(
        "UPDATE action_executions SET status = 'IN_PROGRESS', finished_at = NULL"
        f" WHERE status::text = '{_NEW_STATUS}'"
    )

    # 2. 보류 기록을 걷는다
    op.drop_constraint('unverified_has_hold', 'action_executions', type_='check')
    op.drop_constraint('verification_hold_shape', 'action_executions', type_='check')
    for column in _HOLD_COLUMNS:
        op.drop_column('action_executions', column)
    postgresql.ENUM(name='precheck_reason_code').drop(op.get_bind(), checkfirst=True)

    # 3. execution_status 재생성. status를 참조하는 CHECK·부분 인덱스를 먼저 떼어 낸다 —
    #    두면 ALTER COLUMN TYPE이 옛 타입으로 굳은 비교식을 새 타입 컬럼에 다시 걸어
    #    연산자 없음으로 실패한다(20260901 마이그레이션과 같은 이유)
    op.drop_index(
        'ix_action_executions_non_terminal',
        table_name='action_executions',
        postgresql_where=sa.text(_NON_TERMINAL),
    )
    op.drop_constraint('rollback_child_status', 'action_executions', type_='check')
    old_values = ", ".join(f"'{value}'" for value in _OLD_STATUSES)
    op.execute("ALTER TYPE execution_status RENAME TO execution_status_old")
    op.execute(f"CREATE TYPE execution_status AS ENUM ({old_values})")
    op.execute(
        "ALTER TABLE action_executions ALTER COLUMN status TYPE execution_status"
        " USING status::text::execution_status"
    )
    op.execute("DROP TYPE execution_status_old")
    op.create_check_constraint(
        'rollback_child_status', 'action_executions', _ROLLBACK_CHILD_STATUS
    )
    op.create_index(
        'ix_action_executions_non_terminal',
        'action_executions',
        ['status'],
        unique=False,
        postgresql_where=sa.text(_NON_TERMINAL),
    )
