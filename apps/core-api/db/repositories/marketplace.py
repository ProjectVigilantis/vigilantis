# ==============================================================================
# [파일 설명]  담당: 김세혁 (BE · Marketplace 실연동)
# 실제 AWS Marketplace 라이선스 사본(marketplace_licenses)의 읽기 · 쓰기. 판정은 marketplace.py 가 한다.
#
#   - 트랜잭션은 호출부가 소유한다. 여기서는 commit 하지 않는다.
#   - 라이선스는 고객 AWS 계정으로 묶는다. 테넌트 FK 가 없다(등록 랜딩이 테넌트보다 먼저 올 수 있다).
# ==============================================================================

from __future__ import annotations

from datetime import datetime

from db import models
from schemas.subscriptions import SubscriptionSource
from sqlalchemy import select, update
from sqlalchemy.orm import Session

_L = models.MarketplaceLicense
_T = models.Tenant


def licenses_for_account(db: Session, account_id: str) -> list[models.MarketplaceLicense]:
    """그 계정의 라이선스 전부. 쓰기 전에 읽으므로 행을 잠근다(겹친 재확인이 같은 행을 두 번 쓰지 않는다)."""
    return list(
        db.scalars(
            select(_L).where(_L.customer_aws_account_id == account_id).order_by(_L.license_arn).with_for_update()
        )
    )


def add_license(db: Session, row: models.MarketplaceLicense) -> None:
    db.add(row)
    db.flush()


def deactivate_missing(db: Session, account_id: str, seen: set[str], *, now: datetime) -> int:
    """마지막 GetEntitlements 에 나오지 않은 그 계정의 유효 라이선스를 끈다 → 바꾼 행 수."""
    statement = (
        update(_L)
        .where(_L.customer_aws_account_id == account_id, _L.active.is_(True))
        .values(active=False, refreshed_at=now)
    )
    if seen:
        statement = statement.where(_L.license_arn.not_in(seen))
    return db.execute(statement).rowcount or 0


def latest_active_license(
    db: Session, account_id: str, *, now: datetime
) -> models.MarketplaceLicense | None:
    """지금 유효한 라이선스 중 요금제가 가장 최근에 바뀐 것(같은 시각이면 ARN 순 첫째)."""
    return db.scalars(
        select(_L)
        .where(
            _L.customer_aws_account_id == account_id,
            _L.active.is_(True),
            _L.expires_at > now,
        )
        .order_by(_L.decided_at.desc(), _L.license_arn)
        .limit(1)
    ).first()


def due_accounts(db: Session, *, now: datetime, limit: int) -> list[str]:
    """실제 Marketplace 투영인데 권리 끝 시각이 지난 테넌트의 계정 — 갱신됐는지 다시 확인할 대상."""
    return list(
        db.scalars(
            select(_T.aws_account_id)
            .where(
                _T.subscription_source == SubscriptionSource.AWS_MARKETPLACE,
                _T.entitlement_ends_at <= now,
            )
            .order_by(_T.entitlement_ends_at, _T.aws_account_id)
            .limit(limit)
        )
    )
