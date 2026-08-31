"""SECOPS 인시던트의 title 필수화 — category_risk_shape 제약 교체 (Issue #200)

카드 제목은 곧 위협 이름인데 title이 nullable이라 SECOPS 건에서도 비어 올 수 있었다.
비면 화면 제목이 위협이 아니라 자원 ID가 된다.

SECOPS 분기에만 title 조건을 더한다 — FINOPS는 위험 개념이 없어 진단명을 쓰고,
분석 전 null이 정상이므로 지금처럼 nullable로 남는다.

backfill은 없다. 인시던트를 만드는 운영 코드가 아직 없어(create_incident 호출부가
테스트뿐) 이 제약을 어길 기존 행이 없다. 어기는 행이 있다면 제약 생성이 실패해야
하며, 그 실패가 곧 필수화 이전에 새어 든 행이 있다는 신호다.

Revision ID: c3d5a81f47e2
Revises: b71c3f0a92d4
Create Date: 2026-08-28 11:50:00.000000
"""
from __future__ import annotations

from alembic import op

revision = 'c3d5a81f47e2'
down_revision = 'b71c3f0a92d4'
branch_labels = None
depends_on = None

_CONSTRAINT = 'ck_incidents_category_risk_shape'

_WITH_TITLE = (
    "CASE jsonb_typeof(initial_risk_reason_codes) WHEN 'array' THEN"
    " (category = 'SECOPS' AND title IS NOT NULL"
    " AND initial_risk_level IS NOT NULL"
    " AND jsonb_array_length(initial_risk_reason_codes) >= 1)"
    " OR "
    "(category = 'FINOPS' AND initial_risk_level IS NULL"
    " AND reviewed_risk_level IS NULL AND response_mode IS NULL"
    " AND jsonb_array_length(initial_risk_reason_codes) = 0)"
    " ELSE false END"
)

_WITHOUT_TITLE = (
    "CASE jsonb_typeof(initial_risk_reason_codes) WHEN 'array' THEN"
    " (category = 'SECOPS' AND initial_risk_level IS NOT NULL"
    " AND jsonb_array_length(initial_risk_reason_codes) >= 1)"
    " OR "
    "(category = 'FINOPS' AND initial_risk_level IS NULL"
    " AND reviewed_risk_level IS NULL AND response_mode IS NULL"
    " AND jsonb_array_length(initial_risk_reason_codes) = 0)"
    " ELSE false END"
)


def _replace(condition: str) -> None:
    # op.f()로 감싼다 — MetaData 명명 규칙(ck_%(table_name)s_...)이 다시 적용되면
    # ck_incidents_ck_incidents_... 로 접두가 겹친다 (baseline 리비전과 같은 표기).
    # op.f()는 migration context를 요구하므로 모듈 최상위가 아니라 여기서 부른다.
    op.drop_constraint(op.f(_CONSTRAINT), 'incidents', type_='check')
    op.create_check_constraint(op.f(_CONSTRAINT), 'incidents', condition)


def upgrade() -> None:
    _replace(_WITH_TITLE)


def downgrade() -> None:
    _replace(_WITHOUT_TITLE)
