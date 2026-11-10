"""실제 AWS Marketplace 연동(ADR-0012 2차 개정) — 토큰 해석 · 권리 재확인 · 투영 · 등록 랜딩 · 알림 수신.

Marketplace 에는 샌드박스가 없어 실제 호출은 하지 않는다. AWS 호출 모양은 botocore Stubber 가 서비스 모델로 검사하고
(파라미터 이름 · 형식이 틀리면 Stubber 가 실패한다), DB 흐름은 실제 PostgreSQL 에서 가짜 클라이언트로 돈다.
  ① ResolveCustomer — 계정 ID 는 AWS 응답만 믿고, 다른 상품 · 만료 · 위조 토큰은 받지 않는다.
  ② GetEntitlements — 빈 쪽 다음 토큰까지 읽고, 가격 항목을 요금제로 대응시킨다. 대응이 없거나 값이 0 이면 권리가 아니다.
  ③ 투영 — 실제 권리가 모의 계약보다 먼저다. 유효한 라이선스가 여럿이면 요금제가 가장 최근에 바뀐 것을 따른다(올림 · 내림
     모두). 같은 값을 다시 읽어도 결정 시각이 그대로다. 권리가 끝나면 APP_FREE · 서비스 정지다. 운영자 PATCH 는 막힌다.
  ④ 등록 랜딩 — 상품 코드가 없으면 404. 테넌트가 있으면 구독 화면, 없으면 가입 화면, 토큰이 틀리면 실패 표시.
  ⑤ 알림 — 처리했거나 대상이 아닌 메시지만 지운다. 재확인이 실패한 메시지는 남는다. 권리 끝이 지난 투영은 알림 없이 다시 읽는다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import boto3
import marketplace
import marketplace_sync
import pytest
import subscription_workflow as workflow
from botocore.stub import Stubber
from config import MarketplaceSettings, get_marketplace_settings
from db import models
from db.repositories import tenants as tenants_repo
from schemas.api.tenants import TenantStatus, TenantSubscription, TenantUpdateRequest
from schemas.subscriptions import SubscriptionSource
from sqlalchemy import select

PRODUCT = "prod-abc123"
LICENSE_A = "arn:aws:license-manager::111111111111:license:l-aaaa"
LICENSE_B = "arn:aws:license-manager::111111111111:license:l-bbbb"
T0 = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)  # 11/10 12:00 KST
EXPIRES = datetime(2026, 12, 10, 5, 30, tzinfo=UTC)  # 12/10 14:30 KST — 자정이 아니다


def _settings(**overrides) -> MarketplaceSettings:
    return MarketplaceSettings(_env_file=None, MARKETPLACE_PRODUCT_CODE=PRODUCT, **overrides)


def _boto(service: str):
    return boto3.client(
        service, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )


def _ent(account, dimension, *, license_arn=LICENSE_A, expires=EXPIRES, value=None):
    item = {
        "ProductCode": PRODUCT,
        "Dimension": dimension,
        "CustomerAWSAccountId": account,
        "LicenseArn": license_arn,
        "ExpirationDate": expires,
    }
    if value is not None:
        item["Value"] = value
    return item


# --- ① ResolveCustomer ---------------------------------------------------------------


def _resolve(response=None, error=None, token="tok"):
    client = _boto("meteringmarketplace")
    with Stubber(client) as stub:
        if error:
            stub.add_client_error("resolve_customer", service_error_code=error, http_status_code=400,
                                  expected_params={"RegistrationToken": token})
        else:
            stub.add_response("resolve_customer", response, {"RegistrationToken": token})
        return marketplace.resolve_customer(token, settings=_settings(), client=client)


def test_resolve_customer_trusts_aws_account():
    # 설치된 botocore 의 서비스 모델에는 아직 Metadata(AgreementId)가 없어 Stubber 응답에 넣을 수 없다 — AWS 문서에는
    # 있다. AgreementId 를 읽는 경로는 아래 등록 랜딩 테스트(가짜 클라이언트)가 본다
    got = _resolve({
        "CustomerIdentifier": "", "ProductCode": PRODUCT, "CustomerAWSAccountId": "111111111111",
        "LicenseArn": LICENSE_A,
    })
    assert got == marketplace.ResolvedCustomer("111111111111", PRODUCT, LICENSE_A, None)


@pytest.mark.parametrize(
    "response, reason",
    [
        ({"ProductCode": "prod-other", "CustomerAWSAccountId": "111111111111"}, "product_mismatch"),
        ({"ProductCode": PRODUCT, "CustomerIdentifier": "legacy-only"}, "account_missing"),
    ],
)
def test_resolve_customer_rejects_other_product_and_legacy_shape(response, reason):
    with pytest.raises(marketplace.MarketplaceRejected) as exc:
        _resolve(response)
    assert exc.value.reason == reason


@pytest.mark.parametrize("code", ["InvalidTokenException", "ExpiredTokenException"])
def test_resolve_customer_rejects_bad_token(code):
    with pytest.raises(marketplace.MarketplaceRejected) as exc:
        _resolve(error=code)
    assert exc.value.reason == code


def test_resolve_customer_rejects_empty_token_without_calling_aws():
    with pytest.raises(marketplace.MarketplaceRejected):
        marketplace.resolve_customer("", settings=_settings(), client=object())


# --- ② GetEntitlements -----------------------------------------------------------------


def test_fetch_grants_reads_past_empty_page_and_maps_dimensions():
    account = "111111111111"
    client = _boto("marketplace-entitlement")
    base = {"ProductCode": PRODUCT, "Filter": {"CUSTOMER_AWS_ACCOUNT_ID": [account]}, "MaxResults": 25}
    with Stubber(client) as stub:
        stub.add_response("get_entitlements", {"Entitlements": [], "NextToken": "n1"}, base)
        stub.add_response(
            "get_entitlements",
            {"Entitlements": [
                _ent(account, "Enterprise", value={"IntegerValue": 1}),
                _ent(account, "unknown-tier", license_arn=LICENSE_B),
            ], "NextToken": "n2"},
            base | {"NextToken": "n1"},
        )
        stub.add_response(
            "get_entitlements",
            {"Entitlements": [_ent(account, "business", license_arn=LICENSE_B, value={"IntegerValue": 0})]},
            base | {"NextToken": "n2"},
        )
        grants = marketplace.fetch_grants(account, settings=_settings(), client=client)
    assert grants == [
        marketplace.LicenseGrant(LICENSE_A, "Enterprise", "Enterprise", EXPIRES),
        # 대응 없는 항목 + 수량 0 은 권리가 아니다 — 요금제 없이 남는다(투영하지 않는다)
        marketplace.LicenseGrant(LICENSE_B, "unknown-tier", None, EXPIRES),
    ]


def test_dimension_plan_table_rejects_unknown_plan():
    with pytest.raises(ValueError):
        _ = _settings(MARKETPLACE_DIMENSION_PLANS="business:Gold").dimension_plans


# --- ③ 투영 (실제 PostgreSQL) ------------------------------------------------------------


class _FakeEntitlement:
    """GetEntitlements 가짜 — 계정별로 돌려줄 권리를 바꿔 끼운다."""

    def __init__(self):
        self.items: list[dict] = []
        self.calls = 0
        self.fail = False

    def get_entitlements(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("throttled")
        account = kwargs["Filter"]["CUSTOMER_AWS_ACCOUNT_ID"][0]
        return {"Entitlements": [i for i in self.items if i["CustomerAWSAccountId"] == account]}


class _FakeMetering:
    class exceptions:  # noqa: N801 — boto3 클라이언트의 모양을 따른다
        class InvalidTokenException(Exception):
            pass

        class ExpiredTokenException(Exception):
            pass

    def __init__(self, account):
        self.account = account

    def resolve_customer(self, RegistrationToken):  # noqa: N803
        if RegistrationToken != "good":
            raise self.exceptions.InvalidTokenException()
        return {"ProductCode": PRODUCT, "CustomerAWSAccountId": self.account, "LicenseArn": LICENSE_A,
                "Metadata": {"AgreementId": "agmt-1"}}


class _FakeSqs:
    def __init__(self, bodies):
        self.messages = [{"Body": body, "ReceiptHandle": f"r{i}"} for i, body in enumerate(bodies)]
        self.deleted: list[str] = []
        self._given = False

    def receive_message(self, **_):
        if self._given:
            return {}
        self._given = True
        return {"Messages": list(self.messages)}

    def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803
        self.deleted.append(ReceiptHandle)


def _clients(entitlement=None, metering=None, sqs=None):
    table = {"marketplace-entitlement": entitlement, "meteringmarketplace": metering, "sqs": sqs}
    return lambda service, region: table[service]


def _account(db, tenant_id):
    return db.get(models.Tenant, tenant_id).aws_account_id


def _tenant(db, tenant_id):
    db.expire_all()
    return db.get(models.Tenant, tenant_id)


def _refresh(db, account, fake, now):
    marketplace_sync.refresh_account(db, account, settings=_settings(), clients=_clients(fake), now=now)


def test_real_entitlement_projects_before_mock_and_ends_in_suspension(db, make_tenant):
    tenant_id = make_tenant()
    account = _account(db, tenant_id)
    fake = _FakeEntitlement()
    fake.items = [_ent(account, "business")]
    _refresh(db, account, fake, T0)
    tenant = _tenant(db, tenant_id)
    assert tenant.subscription_source is SubscriptionSource.AWS_MARKETPLACE
    assert (tenant.plan, str(tenant.period_start), str(tenant.period_end)) == ("Business", "2026-11-10", "2026-12-10")
    assert tenant.entitlement_ends_at == EXPIRES
    assert tenant.billing_anchor_day is None and tenant.amount_minor is None and not tenant.cancel_at_period_end
    # 구독 화면 응답이 이 투영을 그대로 받는다(예정일은 내지 않는다)
    view = workflow.get_subscription(db, tenant_id, can_manage=True,
                                     settings=workflow.SubscriptionSettings(_env_file=None), now=T0)
    assert view.source is SubscriptionSource.AWS_MARKETPLACE
    assert view.next_renewal_date is None and view.expiry_date is None

    fake.items = []  # 권리가 끝났다(철회 · 만료)
    _refresh(db, account, fake, T0 + timedelta(days=31))
    tenant = _tenant(db, tenant_id)
    assert tenant.subscription_source is SubscriptionSource.APP_FREE and tenant.plan == "Free"
    assert tenant.status is TenantStatus.SUSPENDED
    assert tenant.suspended_reason == workflow.MARKETPLACE_EXPIRY_SUSPENSION_REASON


def test_latest_plan_decision_wins_across_concurrent_licenses(db, make_tenant):
    tenant_id = make_tenant()
    account = _account(db, tenant_id)
    fake = _FakeEntitlement()
    fake.items = [_ent(account, "enterprise")]
    _refresh(db, account, fake, T0)
    assert _tenant(db, tenant_id).plan == "Enterprise"

    # 나중에 산 더 낮은 등급이 이긴다 — 최근 결정을 따른다(내림)
    fake.items.append(_ent(account, "business", license_arn=LICENSE_B))
    _refresh(db, account, fake, T0 + timedelta(hours=1))
    assert _tenant(db, tenant_id).plan == "Business"

    # 같은 값을 다시 읽으면 결정 시각이 그대로다 — 순서가 바뀌지 않는다
    _refresh(db, account, fake, T0 + timedelta(hours=2))
    assert _tenant(db, tenant_id).plan == "Business"
    decided = {row.license_arn: row.decided_at for row in db.scalars(select(models.MarketplaceLicense))}
    assert decided == {LICENSE_A: T0, LICENSE_B: T0 + timedelta(hours=1)}

    # 첫 라이선스를 올리면 그 결정이 가장 최근이다(올림)
    fake.items = [_ent(account, "enterprise"), _ent(account, "business", license_arn=LICENSE_B)]
    fake.items[0]["Dimension"] = "business"
    _refresh(db, account, fake, T0 + timedelta(hours=3))
    fake.items[0]["Dimension"] = "enterprise"
    _refresh(db, account, fake, T0 + timedelta(hours=4))
    assert _tenant(db, tenant_id).plan == "Enterprise"


def test_operator_patch_cannot_overwrite_marketplace_projection(db, make_tenant):
    tenant_id = make_tenant()
    account = _account(db, tenant_id)
    fake = _FakeEntitlement()
    fake.items = [_ent(account, "business")]
    _refresh(db, account, fake, T0)
    changed = tenants_repo.update_tenant(
        db, tenant_id, TenantUpdateRequest(subscription=TenantSubscription(plan="Free")), now=T0
    )
    assert changed is False
    assert _tenant(db, tenant_id).subscription_source is SubscriptionSource.AWS_MARKETPLACE


def test_license_without_tenant_is_kept_until_the_account_connects(db, make_tenant):
    fake = _FakeEntitlement()
    fake.items = [_ent("222222222222", "business")]
    _refresh(db, "222222222222", fake, T0)
    row = db.get(models.MarketplaceLicense, LICENSE_A)
    assert row is not None and row.active and row.plan == "Business"


# --- ④ 등록 랜딩 ---------------------------------------------------------------------------


@pytest.fixture()
def landing(client_pg_anon, monkeypatch):
    """상품 코드를 켠 등록 랜딩 → (client, 가짜 GetEntitlements, 계정 설정 함수)."""
    app = client_pg_anon.app
    app.dependency_overrides[get_marketplace_settings] = lambda: _settings()
    fake = _FakeEntitlement()
    state = {"account": "333333333333"}
    monkeypatch.setattr(
        marketplace_sync, "default_client",
        lambda service, region: {"marketplace-entitlement": fake,
                                 "meteringmarketplace": _FakeMetering(state["account"])}[service],
    )
    yield client_pg_anon, fake, state
    app.dependency_overrides.pop(get_marketplace_settings, None)


def _post(client, token):
    return client.post(
        "/api/v1/marketplace/register",
        content=f"x-amzn-marketplace-token={token}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def test_landing_is_absent_without_product_code(client_pg_anon):
    client_pg_anon.app.dependency_overrides[get_marketplace_settings] = lambda: MarketplaceSettings(_env_file=None)
    try:
        assert _post(client_pg_anon, "good").status_code == 404
    finally:
        client_pg_anon.app.dependency_overrides.pop(get_marketplace_settings, None)


def test_landing_routes_by_tenant_and_token(landing, db, make_tenant):
    client, fake, state = landing
    # 테넌트가 없는 계정 — 가입 화면. 권리는 미리 읽어 둔다
    fake.items = [_ent("333333333333", "business")]
    response = _post(client, "good")
    assert (response.status_code, response.headers["location"]) == (303, "/signup-requests?marketplace=registered")
    assert db.get(models.MarketplaceLicense, LICENSE_A).agreement_id == "agmt-1"

    # 테넌트가 있는 계정 — 구독 화면, 그리고 투영까지 됐다
    tenant_id = make_tenant()
    state["account"] = _account(db, tenant_id)
    fake.items = [_ent(state["account"], "enterprise", license_arn=LICENSE_B)]
    response = _post(client, "good")
    assert (response.status_code, response.headers["location"]) == (303, "/subscription")
    assert _tenant(db, tenant_id).plan == "Enterprise"

    # 틀린 토큰 — 실패 표시만, 쓴 것이 없다
    response = _post(client, "forged")
    assert (response.status_code, response.headers["location"]) == (303, "/signup-requests?marketplace=failed")


# --- ⑤ 알림 수신 · 권리 끝 재확인 --------------------------------------------------------------


def _event(account, detail_type="License Updated - Manufacturer", product=PRODUCT, source="aws.agreement-marketplace"):
    detail = {"acceptor": {"accountId": account}, "license": {"arn": LICENSE_A}}
    if product is not None:
        detail["product"] = {"code": product}
    return json.dumps({"source": source, "detail-type": detail_type, "region": "us-east-1", "detail": detail})


def test_account_of_event_filters_source_type_product_and_shape():
    assert marketplace_sync.account_of_event(_event("111111111111"), product_code=PRODUCT) == "111111111111"
    agreement = _event("111111111111", "Purchase Agreement Created - Proposer", product=None)
    assert marketplace_sync.account_of_event(agreement, product_code=PRODUCT) == "111111111111"
    for body in (
        _event("111111111111", product="prod-other"),
        _event("111111111111", source="aws.other"),
        _event("111111111111", "Spend Threshold Reached"),
        _event("not-an-account"),
        "not json",
    ):
        assert marketplace_sync.account_of_event(body, product_code=PRODUCT) is None


def test_receive_events_deletes_only_handled_messages(db, make_tenant, monkeypatch):
    tenant_id = make_tenant()
    account = _account(db, tenant_id)
    fake = _FakeEntitlement()
    fake.items = [_ent(account, "business")]
    sqs = _FakeSqs([_event(account), _event(account), "noise"])
    settings = _settings(MARKETPLACE_EVENTS_QUEUE_URL="https://sqs.us-east-1.amazonaws.com/1/q")
    report = marketplace_sync.CycleReport()
    marketplace_sync.receive_events(db, settings=settings, clients=_clients(fake, sqs=sqs), now=T0, report=report)
    assert fake.calls == 1  # 같은 계정은 묶음에서 한 번만 읽는다
    assert sorted(sqs.deleted) == ["r0", "r1", "r2"]
    assert (report.refreshed, report.ignored, report.failed) == (1, 1, 0)
    assert _tenant(db, tenant_id).plan == "Business"

    # 재확인이 실패하면 그 계정의 메시지는 남는다(SQS 가 다시 준다)
    fake.fail = True
    sqs = _FakeSqs([_event(account)])
    report = marketplace_sync.CycleReport()
    marketplace_sync.receive_events(db, settings=settings, clients=_clients(fake, sqs=sqs), now=T0, report=report)
    assert sqs.deleted == [] and report.failed == 1


def test_refresh_due_rereads_ended_projection_without_event(db, make_tenant):
    tenant_id = make_tenant()
    account = _account(db, tenant_id)
    fake = _FakeEntitlement()
    fake.items = [_ent(account, "business")]
    _refresh(db, account, fake, T0)
    db.commit()

    # 권리 끝 시각이 지났다 — AWS 가 갱신해 끝 시각을 한 달 늘렸다
    renewed = EXPIRES + timedelta(days=30)
    fake.items = [_ent(account, "business", expires=renewed)]
    report = marketplace_sync.CycleReport()
    marketplace_sync.refresh_due(db, settings=_settings(), clients=_clients(fake), now=EXPIRES, report=report)
    tenant = _tenant(db, tenant_id)
    assert report.due_refreshed == 1
    assert tenant.entitlement_ends_at == renewed and tenant.status is TenantStatus.ACTIVE
