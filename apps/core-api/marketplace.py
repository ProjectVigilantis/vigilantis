# ==============================================================================
# [파일 설명]  담당: 김세혁 (BE · Marketplace 실연동)
# 실제 AWS Marketplace 권리(ADR-0012 2차 개정) — 등록 토큰 해석(ResolveCustomer) · 권리 재확인(GetEntitlements) ·
# 테넌트 구독 투영 값.
#
#   - 판매자 등록 · 상품 등록 · 실제 청구는 하지 않는다. 상품 코드(MARKETPLACE_PRODUCT_CODE)가 비어 있으면 이 모듈의
#     AWS 호출은 일어나지 않고 라이선스 행도 생기지 않는다. 그동안 유료 권리의 원천은 모의 계약이다.
#   - 권위 있는 원천은 AWS 다. 알림 내용을 옮겨 적지 않고, 알림을 받으면 그 계정의 권리를 GetEntitlements 로 다시
#     읽는다(AWS 문서 「License Updated → GetEntitlements」). 같은 계정을 몇 번 다시 읽어도 결과가 같다.
#   - 한 계정이 라이선스를 여럿 가질 수 있다(동시 계약). 투영은 유효한 것 가운데 **요금제가 가장 최근에 바뀐**
#     라이선스를 따른다 — 고객이 요금제를 올리거나 내리면 그 결정이 이긴다. 「바뀐 시각」은 AWS 가 주지 않으므로
#     이 서버가 그 변화를 처음 본 시각(decided_at)이다.
#   - AWS 호출은 플랫폼 자격(ECS 태스크 역할)으로 나간다. 고객 역할로 나가면 안 된다(ADR-0012 §2 문맥 밖 규칙).
#     ResolveCustomer 는 상품을 올린 판매자 계정에서만 된다. GetEntitlements 는 us-east-1 에서만 된다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import MarketplaceSettings
from db import models
from db.repositories import marketplace as repo
from schemas.api.tenants import TenantSubscription
from schemas.subscriptions import PAID_PLANS, SubscriptionSource, service_date
from sqlalchemy.orm import Session

logger = logging.getLogger("vigilantis.marketplace")

#: 한 라이선스에 가격 항목이 둘 이상 대응될 때만 쓰는 순서(같은 결정 시각 안의 동점 처리). 최근 결정 규칙과 별개다.
_PLAN_RANK = {"Business": 1, "Enterprise": 2}
assert set(_PLAN_RANK) == PAID_PLANS


class MarketplaceRejected(Exception):
    """등록 토큰을 받지 않는다 — 만료 · 위조 · 다른 상품. 고객에게는 같은 안내로 끝난다."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResolvedCustomer:
    """ResolveCustomer 결과. 계정 ID 는 AWS 가 돌려준 값만 믿는다(브라우저가 보낸 값을 쓰지 않는다)."""

    aws_account_id: str
    product_code: str
    license_arn: str | None
    agreement_id: str | None


@dataclass(frozen=True)
class LicenseGrant:
    """GetEntitlements 에서 라이선스 하나로 묶은 권리."""

    license_arn: str
    dimension: str | None
    plan: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class MarketplaceEntitlement:
    """지금 투영할 실제 권리 1건."""

    license_arn: str
    plan: str
    decided_at: datetime
    expires_at: datetime


# --- AWS 호출 ---------------------------------------------------------------------


def resolve_customer(token: str, *, settings: MarketplaceSettings, client) -> ResolvedCustomer:
    """등록 토큰 → 고객. 토큰 오류와 다른 상품의 토큰은 MarketplaceRejected 다(그 밖의 AWS 오류는 그대로 던진다)."""
    if not token or len(token) > 4096:
        raise MarketplaceRejected("token_shape")
    errors = client.exceptions
    try:
        result = client.resolve_customer(RegistrationToken=token)
    except (errors.InvalidTokenException, errors.ExpiredTokenException) as exc:
        raise MarketplaceRejected(type(exc).__name__) from exc
    account = result.get("CustomerAWSAccountId") or ""
    product = result.get("ProductCode") or ""
    if product != settings.MARKETPLACE_PRODUCT_CODE.strip():
        raise MarketplaceRejected("product_mismatch")
    if len(account) != 12 or not account.isdigit():
        # 새 연동은 계정 ID 가 반드시 온다(옛 연동의 CustomerIdentifier 만 오는 응답은 받지 않는다)
        raise MarketplaceRejected("account_missing")
    return ResolvedCustomer(
        aws_account_id=account,
        product_code=product,
        license_arn=result.get("LicenseArn") or None,
        agreement_id=(result.get("Metadata") or {}).get("AgreementId") or None,
    )


def _granted(value: dict | None) -> bool:
    """권리 값이 「있음」인가. 값이 없거나 참 · 양수면 있음이다(계약형 상품의 수량 · 여부 항목)."""
    if not value:
        return True
    if "BooleanValue" in value:
        return bool(value["BooleanValue"])
    for key in ("IntegerValue", "DoubleValue"):
        if key in value:
            return value[key] > 0
    return bool(value.get("StringValue", "x"))


def fetch_grants(account_id: str, *, settings: MarketplaceSettings, client) -> list[LicenseGrant]:
    """그 계정의 현재 권리를 라이선스별로 묶는다. 빈 쪽 다음 토큰이 와도 끝까지 읽는다(AWS 문서)."""
    plans = settings.dimension_plans
    by_license: dict[str, list[dict]] = {}
    token: str | None = None
    while True:
        kwargs = {
            "ProductCode": settings.MARKETPLACE_PRODUCT_CODE.strip(),
            "Filter": {"CUSTOMER_AWS_ACCOUNT_ID": [account_id]},
            "MaxResults": 25,
        }
        if token:
            kwargs["NextToken"] = token
        page = client.get_entitlements(**kwargs)
        for item in page.get("Entitlements") or ():
            if item.get("CustomerAWSAccountId") not in (None, account_id):
                continue  # 필터를 믿되, 다른 계정이 섞이면 버린다
            arn = item.get("LicenseArn")
            if arn:
                by_license.setdefault(arn, []).append(item)
        token = page.get("NextToken")
        if not token:
            break
    grants = []
    for arn, items in sorted(by_license.items()):
        mapped = [
            (plans[item["Dimension"].lower()], item)
            for item in items
            if item.get("Dimension") and item["Dimension"].lower() in plans and _granted(item.get("Value"))
        ]
        expiries = [item["ExpirationDate"] for item in items if item.get("ExpirationDate")]
        if mapped:
            plan, chosen = max(mapped, key=lambda pair: _PLAN_RANK[pair[0]])
            dimension = chosen["Dimension"]
        else:
            plan, dimension = None, (items[0].get("Dimension") if items else None)
            logger.warning("marketplace_license_without_plan", extra={"license_arn": arn})
        grants.append(
            LicenseGrant(
                license_arn=arn,
                dimension=dimension,
                plan=plan,
                expires_at=max(expiries) if expiries else None,
            )
        )
    return grants


# --- 사본 갱신 · 판정 -------------------------------------------------------------


def apply_grants(
    db: Session,
    account_id: str,
    grants: list[LicenseGrant],
    *,
    product_code: str,
    now: datetime,
    resolved: ResolvedCustomer | None = None,
) -> None:
    """읽은 권리로 그 계정의 라이선스 사본을 맞춘다. commit 하지 않는다.

    요금제가 바뀐 라이선스만 decided_at 을 지금으로 옮긴다 — 같은 값을 다시 읽으면 결정 시각이 그대로다.
    이번에 나오지 않은 라이선스는 유효하지 않다(끝났거나 철회됐다).
    """
    existing = {row.license_arn: row for row in repo.licenses_for_account(db, account_id)}
    for grant in grants:
        active = (
            grant.plan is not None and grant.expires_at is not None and grant.expires_at > now
        )
        row = existing.get(grant.license_arn)
        agreement_id = (
            resolved.agreement_id
            if resolved is not None and resolved.license_arn == grant.license_arn
            else None
        )
        if row is None:
            repo.add_license(
                db,
                models.MarketplaceLicense(
                    license_arn=grant.license_arn,
                    customer_aws_account_id=account_id,
                    product_code=product_code,
                    agreement_id=agreement_id,
                    dimension=grant.dimension,
                    plan=grant.plan,
                    expires_at=grant.expires_at,
                    active=active,
                    decided_at=now,
                    refreshed_at=now,
                    created_at=now,
                ),
            )
            continue
        if row.plan != grant.plan:
            row.decided_at = now
        row.dimension, row.plan = grant.dimension, grant.plan
        row.expires_at, row.active, row.refreshed_at = grant.expires_at, active, now
        if agreement_id:
            row.agreement_id = agreement_id
    repo.deactivate_missing(db, account_id, {grant.license_arn for grant in grants}, now=now)
    db.flush()


def current_entitlement(
    db: Session, account_id: str, *, now: datetime
) -> MarketplaceEntitlement | None:
    """그 계정에서 지금 투영할 실제 권리(요금제가 가장 최근에 바뀐 유효 라이선스). 없으면 None."""
    row = repo.latest_active_license(db, account_id, now=now)
    if row is None or row.plan is None or row.expires_at is None:
        return None
    return MarketplaceEntitlement(
        license_arn=row.license_arn,
        plan=row.plan,
        decided_at=row.decided_at,
        expires_at=row.expires_at,
    )


def projection(entitlement: MarketplaceEntitlement) -> TenantSubscription:
    """실제 권리 → 구독 칸. 기간은 결정 시각부터 권리 끝 시각 직전까지(KST 날짜)다.

    요금 · 기준일 · 갱신 끔 여부는 AWS 청구와 구매자 설정에 있고 여기에 옮겨 적지 않는다(비워 둔다).
    """
    return TenantSubscription(
        plan=entitlement.plan,
        period_start=service_date(entitlement.decided_at),
        period_end=service_date(entitlement.expires_at - timedelta(microseconds=1)),
    )


SOURCE = SubscriptionSource.AWS_MARKETPLACE
