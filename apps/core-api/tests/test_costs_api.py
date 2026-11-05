# GET /api/v1/costs/by-asset-type (#572 11번) — 자산 유형별 비용.
#
# Cost Explorer 는 LocalStack Community 가 지원하지 않는다. 그래서 실제 ce 클라이언트에 botocore Stubber 를 끼워
# 요청 모양(구간 · 묶음 · 리전 · RECORD_TYPE 필터)과 응답 해석을 함께 고정한다. 실 동작은 실 계정 스모크 몫이다.
#
#   ① 네 칸 분류 · 반올림 · 합 · 추정치 · 페이지 합산
#   ② 받아 두기: 같은 날 두 번째 요청은 부르지 않고, 날짜가 바뀌면 부른다. 실패는 10분만 둔다. 동시 요청은 한 번만.
#   ③ 실패는 500 이 아니라 200 + UNAVAILABLE 과 사유 코드
#   ④ 대상 고객사와 범위: 고객사 계정은 자기 것만, 플랫폼 계정은 tenant_id 가 필요하고 범위 밖은 404

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
import pytest
from botocore.stub import Stubber
from pydantic import ValidationError

from schemas.api.auth import PlatformRole, UserRole
from schemas.api.costs import AssetCostsResponse, CostBucket, CostItem
from schemas.api.metrics import AxisStatus
from schemas.api.tenants import TenantStatus

from routers import costs as costs_router
from services import costs as costs_service
from services.aws.client import current_tenant

URL = "/api/v1/costs/by-asset-type"
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_cost_cache():
    """받아 둔 결과는 프로세스 전역이다 — 테스트마다 비운다."""
    costs_router._cost_cache.clear()
    costs_router._cost_inflight.clear()
    yield
    costs_router._cost_cache.clear()
    costs_router._cost_inflight.clear()


@pytest.fixture
def clock(monkeypatch):
    """라우터의 지금 시각. ``clock.now`` 를 바꿔 날짜 경계 · 실패 보존 시간을 넘긴다."""

    class _Clock:
        now = NOW

    c = _Clock()
    monkeypatch.setattr(costs_router, "_now", lambda: c.now)
    return c


@pytest.fixture
def ce(monkeypatch):
    """Stubber 를 끼운 실제 ce 클라이언트. 부를 때의 테넌트 문맥을 ``ce.tenants`` 에 남긴다.

    Stubber 에 넣은 응답보다 많이 부르면 실패한다 — 「부르지 않는다」를 그대로 확인한다."""
    client = boto3.session.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"
    ).client("ce")
    stubber = Stubber(client)
    tenants: list[str | None] = []

    def _client(service, region=None, **_):
        assert (service, region) == ("ce", costs_service.CE_REGION)
        access = current_tenant()
        tenants.append(access.tenant_id if access is not None else None)
        return client

    monkeypatch.setattr(costs_service, "aws_client", _client)
    stubber.activate()
    stubber.tenants = tenants
    yield stubber
    stubber.deactivate()


def _group(service: str, usage_type: str, amount: str, unit: str = "USD") -> dict:
    return {
        "Keys": [service, usage_type],
        "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": unit}},
    }


def _page(*groups: dict, estimated: bool = False, token: str | None = None) -> dict:
    page = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-10-06", "End": "2026-11-01"},
                "Total": {},
                "Groups": list(groups),
                "Estimated": estimated,
            }
        ]
    }
    if token is not None:
        page["NextPageToken"] = token
    return page


def _request(regions: list[str], *, start: str = "2026-10-06", end: str = "2026-11-05", token=None) -> dict:
    request = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "MONTHLY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
        ],
        "Filter": {
            "And": [
                {"Dimensions": {"Key": "REGION", "Values": regions}},
                {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}},
            ]
        },
    }
    if token is not None:
        request["NextPageToken"] = token
    return request


def _tenant_login(login_as, tenant_id: str):
    return login_as(UserRole.ADMIN, tenant_id=tenant_id)


def _manager(login_as, *tenant_ids: str):
    return login_as(
        UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER, assigned_tenant_ids=tenant_ids
    )


# --- ① 분류 · 금액 ------------------------------------------------------------


def test_고객사_계정은_자기_고객사의_30일_비용을_네_칸으로_받는다(client_pg, login_as, make_tenant, ce, clock):
    tenant = make_tenant()
    _tenant_login(login_as, tenant)
    ce.add_response(
        "get_cost_and_usage",
        _page(
            _group("Amazon Elastic Compute Cloud - Compute", "APN2-BoxUsage:t3.micro", "10.004"),
            _group("EC2 - Other", "APN2-EBS:VolumeUsage.gp3", "2.505"),
            _group("EC2 - Other", "APN2-EBS:SnapshotUsage", "0.5"),
            _group("EC2 - Other", "APN2-NatGateway-Hours", "3.2"),
            _group("Amazon Elastic Load Balancing", "APN2-LoadBalancerUsage", "1.111"),
            _group("AmazonCloudWatch", "APN2-CW:Requests", "0.004"),
            estimated=True,
        ),
        _request(["ap-northeast-2"]),
    )

    resp = client_pg.get(URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "READY",
        "reason_code": None,
        "currency": "USD",
        "period_start": "2026-10-06",
        "period_end": "2026-11-05",
        "regions": ["ap-northeast-2"],
        # 칸마다 반올림한 뒤 더한다: 10.00 + 3.01 + 1.11 + 3.20
        "total": "17.32",
        "items": [
            {"bucket": "EC2", "amount": "10.00"},
            {"bucket": "EBS", "amount": "3.01"},
            {"bucket": "ALB_TARGET_GROUP", "amount": "1.11"},
            {"bucket": "OTHER", "amount": "3.20"},
        ],
        "estimated": True,
        "retrieved_at": "2026-11-05T03:00:00Z",
    }
    assert ce.tenants == [tenant]  # 그 고객사 자격으로 불렀다
    ce.assert_no_pending_responses()


def test_비용이_없으면_네_칸_모두_0원이고_추정치가_아니다(client_pg, login_as, make_tenant, ce, clock):
    _tenant_login(login_as, make_tenant())
    ce.add_response("get_cost_and_usage", _page(), _request(["ap-northeast-2"]))

    body = client_pg.get(URL).json()

    assert body["status"] == "READY"
    assert body["total"] == "0.00"
    assert [i["amount"] for i in body["items"]] == ["0.00"] * 4
    assert body["estimated"] is False


def test_여러_페이지를_모두_더한다(client_pg, login_as, make_tenant, ce, clock):
    _tenant_login(login_as, make_tenant())
    ce.add_response(
        "get_cost_and_usage",
        _page(_group("Amazon Elastic Compute Cloud - Compute", "BoxUsage", "1.00"), token="p2"),
        _request(["ap-northeast-2"]),
    )
    ce.add_response(
        "get_cost_and_usage",
        _page(_group("Amazon Elastic Compute Cloud - Compute", "BoxUsage", "2.50")),
        _request(["ap-northeast-2"], token="p2"),
    )

    body = client_pg.get(URL).json()

    assert body["items"][0] == {"bucket": "EC2", "amount": "3.50"}
    ce.assert_no_pending_responses()


def test_EBS가_아닌_EC2_기타는_기타로_간다():
    assert costs_service.bucket_of("EC2 - Other", "APN2-EBS:VolumeIOUsage") is CostBucket.EBS
    assert costs_service.bucket_of("EC2 - Other", "APN2-DataTransfer-Regional-Bytes") is CostBucket.OTHER
    assert costs_service.bucket_of("EC2 - Other", "APN2-NatGateway-Bytes") is CostBucket.OTHER
    assert costs_service.bucket_of("Amazon Elastic Compute Cloud - Compute", "BoxUsage") is CostBucket.EC2
    assert costs_service.bucket_of("Elastic Load Balancing", "LCUUsage") is CostBucket.ALB_TARGET_GROUP
    # EBS 표식은 EC2 기타 안에서만 본다
    assert costs_service.bucket_of("AWS Backup", "EBS:SnapshotUsage") is CostBucket.OTHER


# --- ② 받아 두기 ----------------------------------------------------------------


def test_같은_날_두_번째_요청은_Cost_Explorer를_부르지_않는다(client_pg, login_as, make_tenant, ce, clock):
    _tenant_login(login_as, make_tenant())
    ce.add_response(
        "get_cost_and_usage",
        _page(_group("Amazon Elastic Compute Cloud - Compute", "BoxUsage", "4.00")),
        _request(["ap-northeast-2"]),
    )

    first = client_pg.get(URL).json()
    clock.now = NOW + timedelta(hours=20)  # 같은 UTC 날짜(23:00)
    second = client_pg.get(URL).json()

    assert second == first  # retrieved_at 도 처음 받은 시각 그대로
    assert len(ce.tenants) == 1


def test_날짜가_바뀌면_새_구간으로_다시_받는다(client_pg, login_as, make_tenant, ce, clock):
    _tenant_login(login_as, make_tenant())
    ce.add_response("get_cost_and_usage", _page(), _request(["ap-northeast-2"]))
    ce.add_response(
        "get_cost_and_usage", _page(), _request(["ap-northeast-2"], start="2026-10-07", end="2026-11-06")
    )

    client_pg.get(URL)
    clock.now = NOW + timedelta(days=1)
    body = client_pg.get(URL).json()

    assert (body["period_start"], body["period_end"]) == ("2026-10-07", "2026-11-06")
    ce.assert_no_pending_responses()
    # 지난 구간의 결과는 버렸다
    assert [k.period_end.isoformat() for k in costs_router._cost_cache] == ["2026-11-06"]


def test_실패는_10분만_두고_그_뒤에_다시_묻는다(client_pg, login_as, make_tenant, ce, clock):
    _tenant_login(login_as, make_tenant())
    ce.add_client_error("get_cost_and_usage", service_error_code="LimitExceededException")
    ce.add_response("get_cost_and_usage", _page(), _request(["ap-northeast-2"]))

    first = client_pg.get(URL).json()
    clock.now = NOW + timedelta(minutes=9)
    held = client_pg.get(URL).json()
    clock.now = NOW + timedelta(minutes=10)
    retried = client_pg.get(URL).json()

    assert first["reason_code"] == held["reason_code"] == "COST_EXPLORER_THROTTLED"
    assert len(ce.tenants) == 2
    assert retried["status"] == "READY"


def test_동시에_들어온_요청은_한_번만_부른다(monkeypatch, make_tenant, db, clock):
    from services.aws.client import TenantAccess

    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def _slow(access, **_):
        calls.append(access.tenant_id)
        started.set()
        assert release.wait(5)
        return costs_service.FetchedCosts(
            amounts=tuple((b, Decimal("1.00")) for b in CostBucket), estimated=False
        )

    monkeypatch.setattr(costs_router, "fetch_costs", _slow)
    key = costs_router._CostKey(
        TenantAccess("t-1", "arn:aws:iam::111111111111:role/r", "x"), ("ap-northeast-2",), NOW.date()
    )
    results: list[AssetCostsResponse] = []
    workers = [
        threading.Thread(target=lambda: results.append(costs_router._cached(key, NOW.date() - timedelta(days=30))))
        for _ in range(3)
    ]
    workers[0].start()
    assert started.wait(5)
    for w in workers[1:]:
        w.start()
    release.set()
    for w in workers:
        w.join(5)

    assert calls == ["t-1"]
    assert len(results) == 3 and all(r is results[0] for r in results)


# --- ③ 실패 사유 ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message", "reason"),
    [
        ("AccessDeniedException", "User: x is not authorized to perform: ce:GetCostAndUsage", "COST_EXPLORER_ACCESS_DENIED"),
        ("AccessDeniedException", "User not enabled for cost explorer access", "COST_EXPLORER_NOT_ENABLED"),
        ("DataUnavailableException", "Data is not available.", "COST_EXPLORER_DATA_UNAVAILABLE"),
        ("ThrottlingException", "Rate exceeded", "COST_EXPLORER_THROTTLED"),
        ("InvalidNextTokenException", "bad token", "InvalidNextTokenException"),
    ],
)
def test_읽지_못하면_200과_사유_코드로_답한다(client_pg, login_as, make_tenant, ce, clock, code, message, reason):
    _tenant_login(login_as, make_tenant())
    ce.add_client_error("get_cost_and_usage", service_error_code=code, service_message=message)

    resp = client_pg.get(URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "UNAVAILABLE"
    assert body["reason_code"] == reason
    assert (body["total"], body["items"], body["estimated"]) == (None, [], False)
    assert body["regions"] == ["ap-northeast-2"]


def test_USD가_아닌_금액은_통화_사유로_내린다(client_pg, login_as, make_tenant, ce, clock):
    _tenant_login(login_as, make_tenant())
    ce.add_response(
        "get_cost_and_usage",
        _page(_group("Amazon Elastic Compute Cloud - Compute", "BoxUsage", "1000", unit="KRW")),
        _request(["ap-northeast-2"]),
    )

    assert client_pg.get(URL).json()["reason_code"] == "COST_CURRENCY_UNSUPPORTED"


def test_AssumeRole_거절도_그_코드로_내린다(client_pg, login_as, make_tenant, monkeypatch, clock):
    from botocore.exceptions import ClientError

    def _denied(*_a, **_k):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "not authorized to assume"}}, "AssumeRole")

    monkeypatch.setattr(costs_service, "aws_client", _denied)
    _tenant_login(login_as, make_tenant())

    assert client_pg.get(URL).json()["reason_code"] == "AccessDenied"


@pytest.mark.parametrize("status", [TenantStatus.SUSPENDED, TenantStatus.PENDING_CONNECTION])
def test_ACTIVE가_아닌_고객사는_부르지_않고_상태를_사유로_싣는다(
    client_pg, login_as, make_tenant, ce, clock, status
):
    tenant = make_tenant(status)
    login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)

    body = client_pg.get(URL, params={"tenant_id": tenant}).json()

    assert body["status"] == "UNAVAILABLE"
    assert body["reason_code"] == f"TENANT_{status.value}"
    assert ce.tenants == []


# --- ④ 대상 고객사 · 범위 --------------------------------------------------------


def test_고객사_계정은_자기_고객사_ID를_주어도_되고_다른_고객사는_404(client_pg, login_as, make_tenant, ce, clock):
    mine, other = make_tenant(), make_tenant()
    _tenant_login(login_as, mine)
    ce.add_response("get_cost_and_usage", _page(), _request(["ap-northeast-2"]))

    assert client_pg.get(URL, params={"tenant_id": mine}).status_code == 200
    resp = client_pg.get(URL, params={"tenant_id": other})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "TENANT_NOT_FOUND"
    assert ce.tenants == [mine]


def test_플랫폼_계정은_tenant_id가_없으면_422(client_pg, login_as, ce, clock):
    login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)

    resp = client_pg.get(URL)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_담당_직원은_배정_고객사만_받고_나머지는_404(client_pg, login_as, make_tenant, ce, clock):
    assigned, other = make_tenant(), make_tenant()
    _manager(login_as, assigned)
    ce.add_response("get_cost_and_usage", _page(), _request(["ap-northeast-2"]))

    assert client_pg.get(URL, params={"tenant_id": assigned}).json()["status"] == "READY"
    assert client_pg.get(URL, params={"tenant_id": other}).status_code == 404
    assert client_pg.get(URL, params={"tenant_id": "not-a-uuid"}).status_code == 404
    assert ce.tenants == [assigned]


def test_중앙_관리계정은_모든_고객사를_고를_수_있다(client_pg, login_as, make_tenant, ce, clock):
    tenant = make_tenant()
    login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)
    ce.add_response("get_cost_and_usage", _page(), _request(["ap-northeast-2"]))

    assert client_pg.get(URL, params={"tenant_id": tenant.upper()}).json()["status"] == "READY"
    assert ce.tenants == [tenant]


def test_로그인하지_않으면_401(client_pg_anon):
    assert client_pg_anon.get(URL).status_code == 401


# --- 계약 ------------------------------------------------------------------------


def _ready(**overrides) -> dict:
    values = {
        "status": AxisStatus.READY,
        "period_start": NOW.date() - timedelta(days=30),
        "period_end": NOW.date(),
        "regions": ["ap-northeast-2"],
        "total": Decimal("3.00"),
        "items": [CostItem(bucket=b, amount=Decimal("0.75")) for b in CostBucket],
        "retrieved_at": NOW,
    }
    return {**values, **overrides}


def test_계약은_합이_맞지_않는_READY를_받지_않는다():
    AssetCostsResponse(**_ready())
    with pytest.raises(ValidationError):
        AssetCostsResponse(**_ready(total=Decimal("3.01")))
    with pytest.raises(ValidationError):
        AssetCostsResponse(**_ready(items=_ready()["items"][:3], total=Decimal("2.25")))
    with pytest.raises(ValidationError):
        AssetCostsResponse(**_ready(reason_code="X"))


def test_계약은_데이터를_실은_UNAVAILABLE을_받지_않는다():
    base = _ready(status=AxisStatus.UNAVAILABLE, reason_code="X", total=None, items=[])
    AssetCostsResponse(**base)
    with pytest.raises(ValidationError):
        AssetCostsResponse(**{**base, "reason_code": None})
    with pytest.raises(ValidationError):
        AssetCostsResponse(**{**base, "estimated": True})
    with pytest.raises(ValidationError):
        AssetCostsResponse(**{**base, "total": Decimal("0.00")})
