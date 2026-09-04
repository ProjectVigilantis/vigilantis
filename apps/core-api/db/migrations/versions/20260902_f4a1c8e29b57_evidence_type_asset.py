"""evidence_type에 ASSET 추가 — 판정 회차의 자산 스냅샷을 보존할 자리 (Issue #265)

자산 행은 수집 회차마다 최신 관측으로 덮어써지고(db/repositories/assets.py
upsert_asset) 판정만 회차 단위로 보존된다. 그래서 인시던트가 만들어진 뒤 자산을 다시
읽으면 예전 판정에 최신 자산이 붙는다 — t3.xlarge에서 난 저활성 판정이 이미
t3.medium으로 줄어든 인스턴스에 붙는 식이다. 그 회차의 자산을 남길 곳이 근거밖에
없어 유형을 하나 더한다. content는 판정 회차 + 공개 AssetItem이다
(packages/schemas/evidence.py DetectionAssetSnapshot).

기존 행은 건드리지 않는다 — 새 값은 이 마이그레이션 이후 만들어지는 인시던트부터 붙는다.

PostgreSQL은 enum 값 삭제를 지원하지 않으므로 downgrade는 타입을 새로 만들어 바꿔
낀다. 그 과정에서 ASSET 근거 행은 **삭제한다** — upgrade 이전 어휘에 그 자리를 대신할
값이 없고(자산을 담을 수 있는 유형이 없다), 다른 유형으로 접으면 evidence_type과
content 모델이 어긋난 행이 남아 조회가 계약 검증에서 깨진다.

Revision ID: f4a1c8e29b57
Revises: d9b3e5c71a08
Create Date: 2026-09-02 15:10:00.000000
"""
from __future__ import annotations

from alembic import op

revision = 'f4a1c8e29b57'
down_revision = 'd9b3e5c71a08'
branch_labels = None
depends_on = None

# 값 원천은 packages/schemas/evidence.py EvidenceType.
_NEW_VALUE = 'ASSET'
_OLD_VALUES = (
    'METRIC',
    'RULE',
    'THREAT',
    'EXECUTION',
)


def upgrade() -> None:
    # PG 12+는 트랜잭션 안에서 ADD VALUE를 허용한다. 같은 트랜잭션에서 그 값을
    # **사용**하는 것만 막히는데, 여기서는 추가만 하고 쓰지 않는다.
    # Python enum 선언 순서와 맞춰 마지막에 붙인다.
    op.execute(f"ALTER TYPE evidence_type ADD VALUE IF NOT EXISTS '{_NEW_VALUE}'")


def downgrade() -> None:
    old_values = ", ".join(f"'{value}'" for value in _OLD_VALUES)
    op.execute("ALTER TYPE evidence_type RENAME TO evidence_type_old")
    op.execute(f"CREATE TYPE evidence_type AS ENUM ({old_values})")
    # 새 값을 쓰던 행을 먼저 지운다 — 남겨 두면 USING 캐스트가 실패하고, 다른 유형으로
    # 접으면 content 모델이 어긋난 근거가 남는다
    op.execute(f"DELETE FROM evidence_items WHERE evidence_type::text = '{_NEW_VALUE}'")
    op.execute(
        "ALTER TABLE evidence_items ALTER COLUMN evidence_type TYPE evidence_type"
        " USING evidence_type::text::evidence_type"
    )
    op.execute("DROP TYPE evidence_type_old")
