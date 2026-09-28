"""incident_status에 AWAITING_CLOSURE 추가 — 조치 종료·판단 대기 자리 (Issue #240)

2/2 Status Check가 통과해 실행이 SUCCESS로 확정되면 그 Incident에는 진행 중 실행도
남은 제안도 없다. 기존 5종에는 그 조합이 갈 자리가 없어 _incident_status_after가
FAILED로 떨어뜨렸다 — 계약은 통과하지만(FAILED = 흐름 진행 불가) 성공한 조치가
화면에서 빨간 '진행 불가'로 읽힌다. RESOLVED로 시스템이 먼저 옮기는 것은 관제자
종료 판단을 영구히 비우므로(Issue #199) 쓸 수 없다.

기존 행은 건드리지 않는다 — 새 값은 이 마이그레이션 이후 확정되는 실행부터 붙는다.

PostgreSQL은 enum 값 삭제를 지원하지 않으므로 downgrade는 타입을 새로 만들어
바꿔 끼운다. 그 과정에서 AWAITING_CLOSURE 행은 FAILED로 접힌다 — upgrade 이전
어휘에서 그 자리를 대신하던 값이 FAILED이기 때문이다.

Revision ID: d9b3e5c71a08
Revises: a7f2c91d4e63
Create Date: 2026-09-01 14:20:00.000000
"""
from __future__ import annotations

from alembic import op

revision = 'd9b3e5c71a08'
down_revision = 'a7f2c91d4e63'
branch_labels = None
depends_on = None

# 값 원천은 packages/schemas/api/incidents.py IncidentStatus.
_NEW_VALUE = 'AWAITING_CLOSURE'
_OLD_VALUES = (
    'ANALYZING',
    'AWAITING_APPROVAL',
    'ACTION_IN_PROGRESS',
    'RESOLVED',
    'FAILED',
)


def upgrade() -> None:
    # PG 12+는 트랜잭션 안에서 ADD VALUE를 허용한다. 같은 트랜잭션에서 그 값을
    # **사용**하는 것만 막히는데, 여기서는 추가만 하고 쓰지 않는다.
    # RESOLVED 앞에 끼워 Python enum 선언 순서와 저장 순서를 맞춘다.
    op.execute(
        f"ALTER TYPE incident_status ADD VALUE IF NOT EXISTS '{_NEW_VALUE}'"
        " BEFORE 'RESOLVED'"
    )


def downgrade() -> None:
    old_values = ", ".join(f"'{value}'" for value in _OLD_VALUES)
    # status를 참조하는 CHECK를 먼저 떼어 낸다. 두면 ALTER COLUMN TYPE이 옛 타입으로
    # 굳은 비교식('RESOLVED')을 새 타입 컬럼에 다시 걸어 연산자 없음으로 실패한다.
    op.drop_constraint('resolution_with_resolved_status', 'incidents', type_='check')
    op.execute("ALTER TYPE incident_status RENAME TO incident_status_old")
    op.execute(f"CREATE TYPE incident_status AS ENUM ({old_values})")
    # 새 값을 쓰던 행을 먼저 접는다 — 남겨 두면 USING 캐스트가 실패한다
    op.execute(
        f"UPDATE incidents SET status = 'FAILED' WHERE status::text = '{_NEW_VALUE}'"
    )
    op.execute(
        "ALTER TABLE incidents ALTER COLUMN status TYPE incident_status"
        " USING status::text::incident_status"
    )
    op.execute("DROP TYPE incident_status_old")
    op.create_check_constraint(
        'resolution_with_resolved_status',
        'incidents',
        "((resolution IS NULL) = (resolved_at IS NULL))"
        " AND (resolution IS NULL OR status = 'RESOLVED')",
    )
