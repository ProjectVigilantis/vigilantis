"""/api/v1/tenants — 고객(테넌트) 관리 라우터 (Post-MVP T-4). 실제 PostgreSQL + ADMIN 세션.

지키는 것.
  ① PLATFORM 계정 전용(고객사 ADMIN · OPERATOR · VIEWER 는 403) · 미인증 401 — 등록된 경로 전부.
  ② 받는 상태 밖은 409, 없거나 형식이 어긋난 식별자는 404, 있지만 담당 범위 밖이면 403(#572 7번) — 거절하면 DB 가 그대로다.
  ③ 온보딩 링크 설정이 없으면 링크를 싣는 응답만 503 이고, 그때 쓰기도 남지 않는다.
  ④ external_id 는 생성 응답과 재발급에서만 나간다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from auth import sessions
import subscription_workflow
from config import SubscriptionSettings, TenantSettings, get_tenant_settings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import tenants as tenants_repo
from fastapi.routing import iter_route_contexts
from schemas.api.auth import AccountType, PlatformRole, UserRole
from schemas.api.subscriptions import SubscriptionPurchaseRequest
from schemas.api.tenants import (
    TenantCreateResponse,
    TenantDetail,
    TenantStatus,
    TenantVerificationResult,
)
from sqlalchemy import delete, func, select, update
from tests.group_scope import assign_tenants

BASE = "/api/v1/tenants"
ACCOUNT = "123456789012"
OTHER = "111122223333"
TEMPLATE = "https://templates.example.com/vigilantis-access.yaml"
PRINCIPAL = "arn:aws:iam::999988887777:role/vigilantis-platform"
NOW = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)
CREATE = {"name": "학원", "aws_account_id": ACCOUNT, "regions": ["ap-northeast-2", "us-east-1"]}
MISSING = "00000000-0000-0000-0000-000000000000"

# 등록돼야 하는 경로 전부 — 새 경로가 생기면 이 표와 권한 테스트가 함께 바뀐다
REQUESTS = [
    ("GET", "", None),
    ("GET", "/search?q=학원", None),
    ("POST", "", CREATE),
    ("GET", "/{id}", None),
    ("PATCH", "/{id}", {"name": "x"}),
    ("DELETE", "/{id}", None),
    ("GET", "/{id}/onboarding", None),
    ("PUT", "/{id}/group", {"group_id": None}),
    ("POST", "/{id}/suspend", {"reason": "해지"}),
    ("POST", "/{id}/verify", None),
    ("POST", "/{id}/resume", None),
]
# 고객 ID 로 여는 상세 · 하위 경로
BY_ID = [r for r in REQUESTS if "{id}" in r[1]]


def _settings(ready: bool) -> TenantSettings:
    return TenantSettings(
        TENANT_ONBOARDING_TEMPLATE_URL=TEMPLATE if ready else None,
        PLATFORM_PRINCIPAL_ARN=PRINCIPAL if ready else None,
    )


@pytest.fixture()
def onboarding(client_pg):
    """온보딩 설정을 켠다. 끄려면 configure(False)."""
    def configure(ready: bool = True) -> None:
        client_pg.app.dependency_overrides[get_tenant_settings] = lambda: _settings(ready)

    configure(True)
    return configure


@pytest.fixture()
def admin(login_as, onboarding):
    return login_as(UserRole.ADMIN, platform_role=PlatformRole.CENTRAL_ADMIN)


def _assign(db, user_id: str, *tenant_ids: str) -> None:
    """담당 범위 시드 — 그 고객사들이 든 그룹을 배정한다(#572 2번 · tests/group_scope)."""
    assign_tenants(db, user_id=user_id, tenant_ids=tenant_ids, now=NOW)
    db.commit()


def _unassign(db, user_id: str) -> None:
    auth_repo.replace_assigned_groups(db, user_id=user_id, group_ids=(), now=NOW)
    db.commit()


@pytest.fixture()
def manager(login_as, onboarding):
    """배정이 없는 TENANT_MANAGER."""
    return login_as(UserRole.ADMIN, platform_role=PlatformRole.TENANT_MANAGER)


def _seed(db, status=TenantStatus.PENDING_CONNECTION, account=ACCOUNT, *, now=NOW) -> models.Tenant:
    tenant = tenants_repo.create_tenant(
        db, name="학원", aws_account_id=account, regions=["ap-northeast-2"], now=now
    )
    if status is not TenantStatus.PENDING_CONNECTION:
        assert tenants_repo.record_verification(
            db, tenant.tenant_id, result=TenantVerificationResult.OK, reason_code=None, now=NOW)
    if status is TenantStatus.SUSPENDED:
        assert tenants_repo.suspend_tenant(db, tenant.tenant_id, reason="계약 만료", now=NOW)
    # 운영처럼 요청 전에 확정해 둔다 — 서비스의 거절 경로 rollback 이 시드를 지우지 않게.
    # db 픽스처는 SAVEPOINT 모드라 테스트가 끝나면 그래도 전부 되돌아간다
    db.commit()
    return tenant


def _tenant_count(db) -> int:
    return db.scalar(select(func.count()).select_from(models.Tenant))


def _code(resp) -> str:
    return resp.json()["error"]["code"]


# ----- ① 권한 --------------------------------------------------------------------

def test_requests_cover_every_registered_tenants_route(client):
    registered = {
        (method, route.path)
        for route in iter_route_contexts(client.app.routes)
        if route.path == BASE or route.path.startswith(BASE + "/")
        # 고객 설정(T-7)은 고객사 계정도 받는 별도 라우터다(test_tenant_settings_api)
        if route.path != BASE + "/{tenant_id}/settings"
        for method in route.methods or ()
    }
    covered = {
        (method, BASE + path.split("?")[0].replace("{id}", "{tenant_id}")) for method, path, _ in REQUESTS
    }
    assert covered == registered


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER])
@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_tenant_account_is_forbidden_everywhere(
    client_pg, db, login_as, make_tenant, onboarding, role, method, path, body
):
    # 고객사 계정은 역할(ADMIN 포함)과 관계없이 막힌다 — PLATFORM 은 ADMIN 만 존재한다(DB CHECK)
    tenant = _seed(db)
    login_as(role, tenant_id=make_tenant())
    before = _tenant_count(db)
    resp = client_pg.request(method, BASE + path.replace("{id}", tenant.tenant_id), json=body)
    assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"
    assert _tenant_count(db) == before
    assert tenants_repo.get_tenant(db, tenant.tenant_id).tenant.name == "학원"


@pytest.mark.parametrize("method,path,body", [r for r in REQUESTS if r[0] != "GET"])
def test_state_changing_requests_from_another_origin_are_403(client_pg, db, admin, method, path, body):
    # 라우터의 Origin 관문(main.py protected)은 require_platform_account 가 세션을 따로 보기 때문에 401 · 403 테스트로는
    # 빠져도 드러나지 않는다 — 허용 밖 Origin 의 상태 변경이 본문 전에 막히는지 따로 본다(#519 리뷰)
    tenant = _seed(db)
    resp = client_pg.request(method, BASE + path.replace("{id}", tenant.tenant_id), json=body,
                             headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403 and _code(resp) == "AUTH_ORIGIN_REJECTED"
    assert _tenant_count(db) == 1
    assert tenants_repo.get_tenant(db, tenant.tenant_id).tenant.name == "학원"


def test_unauthenticated_is_401(client_pg):
    assert client_pg.get(BASE, headers={"Cookie": ""}).status_code == 401


# 관리 권한이 필요한 요청 — 쓰기 전부와 온보딩 자격 증명(GET /{id}/onboarding)
MANAGEMENT = [r for r in REQUESTS if r[0] != "GET" or r[1] == "/{id}/onboarding"]
WRITES = [r for r in MANAGEMENT if r[0] != "GET"]


def _state(db):
    return db.execute(
        select(models.Tenant.tenant_id, models.Tenant.name, models.Tenant.status,
               models.Tenant.suspended_reason, models.Tenant.updated_at)
        .order_by(models.Tenant.tenant_id)
    ).all()


@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_unassigned_manager_sees_and_changes_no_customer(client_pg, db, manager, method, path, body):
    tenant = _seed(db)
    before = _state(db)
    resp = client_pg.request(method, BASE + path.replace("{id}", tenant.tenant_id), json=body)
    if (method, path) == ("GET", ""):
        assert resp.status_code == 200 and resp.json()["tenants"] == []
    elif method == "GET" and path.startswith("/search"):  # 검색에는 뜨지만 열 수 없다(#572 7번)
        assert resp.status_code == 200
        assert resp.json()["items"] == [{"tenant_id": tenant.tenant_id, "name": "학원", "accessible": False}]
    elif (method, path) == ("POST", ""):  # 담당자의 생성은 담당 그룹 지정이 필수다
        assert resp.status_code == 422 and _code(resp) == "REQUEST_VALIDATION_FAILED"
    else:  # 있는 고객이지만 범위 밖이다 · 그룹 변경은 중앙 관리자만
        assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"
    assert tenant.external_id not in resp.text and "onboarding_url" not in resp.text
    assert _state(db) == before


def test_manager_is_confined_to_assigned_customers(client_pg, db, manager, onboarding):
    mine, other = _seed(db), _seed(db, TenantStatus.ACTIVE, OTHER)
    _assign(db, manager.user_id, mine.tenant_id)

    listing = client_pg.get(BASE)
    assert [t["tenant_id"] for t in listing.json()["tenants"]] == [mine.tenant_id]
    assert other.tenant_id not in listing.text
    # 담당 고객의 PENDING 상세 · 온보딩 · 쓰기는 된다
    shown = client_pg.get(f"{BASE}/{mine.tenant_id}")
    assert shown.status_code == 200 and shown.json()["onboarding_url"]
    assert client_pg.get(f"{BASE}/{mine.tenant_id}/onboarding").json()["external_id"] == mine.external_id
    assert client_pg.patch(f"{BASE}/{mine.tenant_id}", json={"name": "바뀜"}).status_code == 200
    # 담당 밖 고객은 있는 고객이라 403 이다(#572 7번) — 내용은 싣지 않는다
    for method, path, body in BY_ID:
        resp = client_pg.request(method, BASE + path.replace("{id}", other.tenant_id), json=body)
        assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"
        assert other.external_id not in resp.text and other.aws_account_id not in resp.text
    # 없는 고객은 그대로 404 다
    missing = client_pg.request("GET", f"{BASE}/{MISSING}")
    assert missing.status_code == 404 and _code(missing) == "TENANT_NOT_FOUND"
    assert tenants_repo.get_tenant(db, other.tenant_id).tenant.name == "학원"
    assert client_pg.post(BASE, json={**CREATE, "aws_account_id": "444455556666"}).status_code == 422
    assert client_pg.delete(f"{BASE}/{mine.tenant_id}").status_code == 204


def test_central_admin_sees_every_customer(client_pg, db, admin):
    a, b = _seed(db), _seed(db, TenantStatus.ACTIVE, OTHER)
    ids = {t["tenant_id"] for t in client_pg.get(BASE).json()["tenants"]}
    assert ids == {a.tenant_id, b.tenant_id}


def test_central_admin_keeps_the_whole_contract(client_pg, db, admin, onboarding):
    created = TenantCreateResponse.model_validate(client_pg.post(BASE, json=CREATE).json())
    tenant_id = created.tenant.tenant_id
    assert created.tenant.onboarding_url is not None
    assert TenantDetail.model_validate(client_pg.get(f"{BASE}/{tenant_id}").json()).onboarding_url
    issued = client_pg.get(f"{BASE}/{tenant_id}/onboarding")
    assert issued.status_code == 200 and issued.json()["external_id"] == created.external_id
    assert client_pg.patch(f"{BASE}/{tenant_id}", json={"name": "바뀜"}).status_code == 200
    assert client_pg.delete(f"{BASE}/{tenant_id}").status_code == 204
    assert _tenant_count(db) == 0


def _revoke_inside_the_lock(monkeypatch, revoke):
    """의존성이 신원을 본 뒤, 서비스가 users 잠금을 잡는 순간 회수가 먼저 커밋된 상황을 만든다."""
    original = auth_repo.lock_users_table

    def wrapped(session):
        revoke(session)
        original(session)

    monkeypatch.setattr(auth_repo, "lock_users_table", wrapped)


@pytest.mark.parametrize("method,path,body", WRITES)
def test_session_revoked_after_the_dependency_snapshot_writes_nothing(
    client_pg, db, admin, monkeypatch, method, path, body
):
    tenant = _seed(db)
    before = _state(db)
    _revoke_inside_the_lock(
        monkeypatch, lambda s: sessions.revoke_session(s, admin.token, now=datetime.now(UTC))
    )
    resp = client_pg.request(method, BASE + path.replace("{id}", tenant.tenant_id), json=body)
    assert resp.status_code == 401 and _code(resp) == "AUTH_REQUIRED"
    assert _state(db) == before


@pytest.mark.parametrize("method,path,body", WRITES)
def test_central_admin_demoted_after_the_dependency_snapshot_loses_global_writes(
    client_pg, db, admin, monkeypatch, method, path, body
):
    tenant = _seed(db)
    before = _state(db)

    def demote(session):
        session.get(models.AppUser, admin.user_id).platform_role = PlatformRole.TENANT_MANAGER
        session.flush()

    _revoke_inside_the_lock(monkeypatch, demote)
    resp = client_pg.request(method, BASE + path.replace("{id}", tenant.tenant_id), json=body)
    if (method, path) == ("POST", ""):  # 담당자로 다시 판정됐다 — 담당 그룹 지정이 필수다
        assert resp.status_code == 422 and _code(resp) == "REQUEST_VALIDATION_FAILED"
    else:  # 강등되어 담당 그룹이 없다 → 범위 밖(#572 7번) · 그룹 변경은 중앙 관리자만
        assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"
    assert _state(db) == before


@pytest.mark.parametrize("method,path,body", [r for r in WRITES if (r[0], r[1]) != ("POST", "")])
def test_assignment_revoked_after_the_dependency_snapshot_writes_nothing(
    client_pg, db, manager, monkeypatch, method, path, body
):
    # 정지는 활성 고객만 대상이 되므로 ACTIVE 로 시드한다(나머지 쓰기는 기존 PENDING 유지)
    tenant = _seed(db, TenantStatus.ACTIVE if path == "/{id}/suspend" else TenantStatus.PENDING_CONNECTION)
    _assign(db, manager.user_id, tenant.tenant_id)
    before = _state(db)
    _revoke_inside_the_lock(monkeypatch, lambda s: _unassign(s, manager.user_id))
    resp = client_pg.request(method, BASE + path.replace("{id}", tenant.tenant_id), json=body)
    # 회수된 범위 밖 고객은 403 이다(#572 7번). 그룹 변경은 관문이 먼저 403 으로 막는다
    assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"
    assert _state(db) == before


# ----- 생성 ----------------------------------------------------------------------

def test_create_returns_pending_tenant_with_external_id_and_onboarding_link(client_pg, db, admin):
    resp = client_pg.post(BASE, json=CREATE)

    assert resp.status_code == 201
    body = TenantCreateResponse.model_validate(resp.json())
    assert body.tenant.status is TenantStatus.PENDING_CONNECTION
    assert body.tenant.access_role_arn == f"arn:aws:iam::{ACCOUNT}:role/vigilantis-access"
    assert len(body.external_id) == 32
    link = urlsplit(body.tenant.onboarding_url)
    assert link.netloc == "ap-northeast-2.console.aws.amazon.com"  # 첫 리전
    params = parse_qs(link.fragment.split("?", 1)[1])
    assert params == {
        "templateURL": [TEMPLATE], "stackName": ["vigilantis-access"],
        "param_ExternalId": [body.external_id], "param_PlatformPrincipalArn": [PRINCIPAL],
        "param_RoleName": ["vigilantis-access"],
    }
    stored = tenants_repo.get_tenant(db, body.tenant.tenant_id).tenant
    assert stored.external_id == body.external_id


def test_create_same_account_twice_is_409_and_keeps_one_row(client_pg, db, admin):
    assert client_pg.post(BASE, json=CREATE).status_code == 201
    resp = client_pg.post(BASE, json=CREATE | {"name": "둘째"})
    assert resp.status_code == 409 and _code(resp) == "TENANT_ACCOUNT_CONFLICT"
    assert _tenant_count(db) == 1


def test_create_without_onboarding_settings_is_503_and_writes_nothing(client_pg, db, admin, onboarding):
    onboarding(False)
    resp = client_pg.post(BASE, json=CREATE)
    assert resp.status_code == 503 and _code(resp) == "TENANT_ONBOARDING_UNAVAILABLE"
    assert _tenant_count(db) == 0


def test_create_with_role_in_another_account_is_rejected_by_the_contract(client_pg, db, admin):
    resp = client_pg.post(BASE, json=CREATE | {"access_role_arn": f"arn:aws:iam::{OTHER}:role/x"})
    assert resp.status_code == 422
    assert _tenant_count(db) == 0


def test_central_creates_ungrouped_or_into_any_existing_group(client_pg, db, admin, make_group):
    group_id = make_group()
    ungrouped = client_pg.post(BASE, json=CREATE)
    assert ungrouped.status_code == 201 and ungrouped.json()["tenant"]["group_id"] is None
    grouped = client_pg.post(BASE, json={**CREATE, "aws_account_id": OTHER, "group_id": group_id})
    assert grouped.status_code == 201 and grouped.json()["tenant"]["group_id"] == group_id
    assert tenants_repo.get_tenant(db, grouped.json()["tenant"]["tenant_id"]).tenant.group_id == group_id
    assert {t["group_id"] for t in client_pg.get(BASE).json()["tenants"]} == {None, group_id}


def test_central_create_with_an_unknown_group_is_404_and_writes_nothing(client_pg, db, admin):
    resp = client_pg.post(BASE, json={**CREATE, "group_id": MISSING})
    assert resp.status_code == 404 and _code(resp) == "GROUP_NOT_FOUND"
    assert _tenant_count(db) == 0


def test_manager_creates_a_tenant_in_its_own_group(client_pg, db, manager, make_group):
    group_id = make_group()
    auth_repo.replace_assigned_groups(db, user_id=manager.user_id, group_ids=(group_id,), now=NOW)
    db.commit()

    resp = client_pg.post(BASE, json={**CREATE, "group_id": group_id})

    assert resp.status_code == 201
    created = TenantCreateResponse.model_validate(resp.json())
    assert created.tenant.group_id == group_id
    # 새 고객은 담당 그룹에 들었으므로 다음 요청부터 담당자의 범위다
    assert [t["tenant_id"] for t in client_pg.get(BASE).json()["tenants"]] == [created.tenant.tenant_id]
    assert client_pg.get(f"{BASE}/{created.tenant.tenant_id}").status_code == 200


@pytest.mark.parametrize("kind", ["missing", "foreign", "unknown"])
def test_manager_create_needs_one_of_its_own_groups(client_pg, db, manager, make_group, kind):
    mine, foreign = make_group(), make_group()
    auth_repo.replace_assigned_groups(db, user_id=manager.user_id, group_ids=(mine,), now=NOW)
    db.commit()
    body = {"missing": CREATE, "foreign": {**CREATE, "group_id": foreign}, "unknown": {**CREATE, "group_id": MISSING}}
    resp = client_pg.post(BASE, json=body[kind])
    if kind == "missing":
        assert resp.status_code == 422 and _code(resp) == "REQUEST_VALIDATION_FAILED"
    else:  # 담당 밖 그룹은 있든 없든 403 이다 — 그룹의 존재를 알리지 않는다
        assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"
    assert _tenant_count(db) == 0


# ----- 검색 (#572 7번) --------------------------------------------------------------

SEARCH_KEYS = {"tenant_id", "name", "accessible"}


def _named(db, name: str, account: str) -> models.Tenant:
    tenant = tenants_repo.create_tenant(db, name=name, aws_account_id=account, regions=["ap-northeast-2"], now=NOW)
    db.commit()
    return tenant


def test_manager_search_shows_outside_tenants_as_inaccessible_with_only_name_and_id(client_pg, db, manager):
    mine = _named(db, "검색 학원 본점", "100000000001")
    theirs = _named(db, "검색 학원 지점", "100000000002")
    _named(db, "다른 이름", "100000000003")
    _assign(db, manager.user_id, mine.tenant_id)

    resp = client_pg.get(f"{BASE}/search", params={"q": "검색 학원"})

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert all(set(item) == SEARCH_KEYS for item in items)
    assert items == [
        {"tenant_id": mine.tenant_id, "name": "검색 학원 본점", "accessible": True},
        {"tenant_id": theirs.tenant_id, "name": "검색 학원 지점", "accessible": False},
    ]
    for leaked in (theirs.aws_account_id, theirs.external_id, "status", "asset_count", "group_id"):
        assert leaked not in resp.text
    # 검색에 뜬 담당 밖 고객을 열면 권한 없음이고, 없는 고객은 404 다
    detail = client_pg.get(f"{BASE}/{theirs.tenant_id}")
    assert detail.status_code == 403 and _code(detail) == "AUTH_FORBIDDEN"
    assert client_pg.get(f"{BASE}/{MISSING}").status_code == 404
    # 목록에는 여전히 담당 고객만 나온다
    assert [t["tenant_id"] for t in client_pg.get(BASE).json()["tenants"]] == [mine.tenant_id]


def test_central_search_is_case_insensitive_and_everything_is_accessible(client_pg, db, admin):
    first = _named(db, "Acme Academy", "100000000011")
    second = _named(db, "ACME Labs", "100000000012")
    resp = client_pg.get(f"{BASE}/search", params={"q": "acme"})
    assert resp.status_code == 200
    assert {(i["tenant_id"], i["accessible"]) for i in resp.json()["items"]} == {
        (first.tenant_id, True), (second.tenant_id, True)}


def test_search_returns_at_most_twenty_by_name(client_pg, db, admin):
    for n in range(21):
        _named(db, f"상한 검색 {n:02d}", f"2000000000{n:02d}")
    items = client_pg.get(f"{BASE}/search", params={"q": "상한 검색"}).json()["items"]
    assert [i["name"] for i in items] == [f"상한 검색 {n:02d}" for n in range(20)]


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "가" * 101}])
def test_search_without_a_valid_query_is_422(client_pg, admin, params):
    resp = client_pg.get(f"{BASE}/search", params=params)
    assert resp.status_code == 422 and _code(resp) == "REQUEST_VALIDATION_FAILED"


# ----- 조회 ----------------------------------------------------------------------

def test_list_is_in_creation_order_without_onboarding_settings(client_pg, db, admin, onboarding):
    second = _seed(db, TenantStatus.ACTIVE, OTHER, now=NOW + timedelta(minutes=1))
    first = _seed(db)  # 나중에 넣었지만 먼저 만들어진 테넌트 — 정렬은 넣은 순서가 아니라 created_at 이다
    db.add(models.Asset(arn=f"arn:aws:ec2:ap-northeast-2:{ACCOUNT}:instance/i-1", tenant_id=first.tenant_id,
                        asset_type="EC2", resource_id="i-1", account_id=ACCOUNT, region="ap-northeast-2"))
    db.flush()
    onboarding(False)  # 목록에는 링크가 없다 — 설정과 무관하다

    resp = client_pg.get(BASE)

    assert resp.status_code == 200
    rows = resp.json()["tenants"]
    assert [(r["tenant_id"], r["asset_count"]) for r in rows] == [(first.tenant_id, 1), (second.tenant_id, 0)]
    assert all("external_id" not in r and "onboarding_url" not in r for r in rows)


def test_detail_has_link_only_while_pending_and_never_external_id(client_pg, db, admin, onboarding):
    pending, active = _seed(db), _seed(db, TenantStatus.ACTIVE, OTHER)

    shown = TenantDetail.model_validate(client_pg.get(f"{BASE}/{pending.tenant_id}").json())
    assert shown.onboarding_url is not None and "external_id" not in client_pg.get(
        f"{BASE}/{pending.tenant_id}").json()

    onboarding(False)
    resp = client_pg.get(f"{BASE}/{active.tenant_id}")  # 연결 뒤에는 링크가 없어 설정이 필요 없다
    assert resp.status_code == 200 and resp.json()["onboarding_url"] is None
    assert resp.json()["last_verification"]["result"] == "OK"
    resp = client_pg.get(f"{BASE}/{pending.tenant_id}")
    assert resp.status_code == 503 and _code(resp) == "TENANT_ONBOARDING_UNAVAILABLE"


def _listed_ids(client_pg, **params) -> list[str]:
    resp = client_pg.get(BASE, params=params)
    assert resp.status_code == 200
    return [t["tenant_id"] for t in resp.json()["tenants"]]


def test_unassigned_only_lists_ungrouped_customers_and_assigning_moves_one_out(
    client_pg, db, admin, make_group
):
    later = _seed(db, TenantStatus.ACTIVE, OTHER, now=NOW + timedelta(minutes=1))
    first = _seed(db)
    grouped = _seed(db, account="444455556666", now=NOW + timedelta(minutes=2))
    group_id = make_group(tenant_ids=[grouped.tenant_id])

    everything = client_pg.get(BASE).json()["tenants"]
    assert [t["tenant_id"] for t in everything] == [first.tenant_id, later.tenant_id, grouped.tenant_id]
    assert client_pg.get(BASE, params={"unassigned_only": "false"}).json()["tenants"] == everything
    # 그룹 없는 행만 같은 모양 · 같은 순서로 남는다 — 연결 상태(PENDING · ACTIVE)와 무관하다
    unassigned = client_pg.get(BASE, params={"unassigned_only": "true"}).json()["tenants"]
    assert unassigned == [t for t in everything if t["group_id"] is None]
    assert [t["tenant_id"] for t in unassigned] == [first.tenant_id, later.tenant_id]

    assert client_pg.put(f"{BASE}/{first.tenant_id}/group", json={"group_id": group_id}).status_code == 200
    assert _listed_ids(client_pg, unassigned_only="true") == [later.tenant_id]
    assert client_pg.get(BASE, params={"unassigned_only": "maybe"}).status_code == 422


def test_unassigned_only_stays_inside_the_manager_scope(client_pg, db, manager):
    mine = _seed(db)
    ungrouped = _seed(db, TenantStatus.ACTIVE, OTHER)  # 그룹 없는 고객은 담당 범위가 아니다
    _assign(db, manager.user_id, mine.tenant_id)

    resp = client_pg.get(BASE, params={"unassigned_only": "true"})

    assert resp.status_code == 200 and resp.json() == {"tenants": []}
    assert ungrouped.tenant_id not in resp.text
    assert _listed_ids(client_pg) == [mine.tenant_id]


@pytest.mark.parametrize("tenant_id", [MISSING, "not-a-uuid"])
@pytest.mark.parametrize("method,path,body", [r for r in REQUESTS if "{id}" in r[1]])
def test_unknown_or_malformed_id_is_404(client_pg, db, admin, tenant_id, method, path, body):
    resp = client_pg.request(method, BASE + path.replace("{id}", tenant_id), json=body)
    assert resp.status_code == 404 and _code(resp) == "TENANT_NOT_FOUND"


# ----- 수정 ----------------------------------------------------------------------

def test_patch_changes_only_sent_fields(client_pg, db, admin):
    tenant = _seed(db, TenantStatus.ACTIVE)
    resp = client_pg.patch(f"{BASE}/{tenant.tenant_id}",
                           json={"auto_action_enabled": False, "subscription": {"plan": "pilot"}})
    assert resp.status_code == 200
    body = TenantDetail.model_validate(resp.json())
    assert (body.auto_action_enabled, body.subscription.plan, body.name) == (False, "pilot", "학원")


@pytest.mark.parametrize("body", [{}, {"aws_account_id": OTHER}, {"name": None}])
def test_patch_outside_the_contract_is_422(client_pg, db, admin, body):
    tenant = _seed(db)
    assert client_pg.patch(f"{BASE}/{tenant.tenant_id}", json=body).status_code == 422


def test_patch_of_pending_tenant_without_settings_is_503_and_changes_nothing(client_pg, db, admin, onboarding):
    tenant = _seed(db)
    onboarding(False)
    resp = client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={"name": "바뀜"})
    assert resp.status_code == 503
    assert tenants_repo.get_tenant(db, tenant.tenant_id).tenant.name == "학원"


# ----- 삭제 ----------------------------------------------------------------------

def test_delete_removes_a_pending_tenant(client_pg, db, admin):
    tenant = _seed(db)
    assert client_pg.delete(f"{BASE}/{tenant.tenant_id}").status_code == 204
    assert _tenant_count(db) == 0


@pytest.mark.parametrize("status", [TenantStatus.ACTIVE, TenantStatus.SUSPENDED])
def test_delete_after_connection_is_409(client_pg, db, admin, status):
    tenant = _seed(db, status)
    resp = client_pg.delete(f"{BASE}/{tenant.tenant_id}")
    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert _tenant_count(db) == 1


def test_delete_with_a_member_is_409_and_keeps_both(client_pg, db, admin, make_auth_session):
    tenant = _seed(db)
    member = make_auth_session(UserRole.VIEWER, tenant_id=tenant.tenant_id)
    resp = client_pg.delete(f"{BASE}/{tenant.tenant_id}")
    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert _tenant_count(db) == 1
    assert db.get(models.AppUser, member.user_id).tenant_id == tenant.tenant_id


_LEGACY = dict(slack_source="LEGACY_ENV", slack_secret_ref="SLACK_ENV",
               email_source="LEGACY_TOPIC", email_legacy_topic_arn="arn:aws:sns:ap-northeast-2:1:t")
_MANAGED_SLACK = dict(slack_source="MANAGED", slack_secret_ref="arn:secret", slack_secret_version="v1",
                      slack_destination_hash="a" * 64)
_MANAGED_EMAIL = dict(email_source="MANAGED", email_address="a@example.com", email_generation=MISSING,
                      email_region="ap-northeast-2", email_status="SETUP_PENDING")


def _seed_settings(db, tenant_id, **columns) -> None:
    db.add(models.TenantSettingsRecord(tenant_id=tenant_id, updated_at=NOW, email_attempts=0, **columns))
    db.commit()


def _settings_rows(db) -> int:
    return db.scalar(select(func.count()).select_from(models.TenantSettingsRecord))


@pytest.mark.parametrize("columns", [
    _LEGACY, dict(protected_tags=[]), dict(slack_source="CLEARED", email_source="CLEARED"), {},
], ids=["legacy", "tags-only", "cleared", "empty"])
def test_delete_removes_a_pending_tenant_with_disposable_settings(client_pg, db, admin, columns):
    tenant = _seed(db)
    _seed_settings(db, tenant.tenant_id, **columns)
    assert client_pg.delete(f"{BASE}/{tenant.tenant_id}").status_code == 204
    assert _tenant_count(db) == 0 and _settings_rows(db) == 0


@pytest.mark.parametrize("columns", [_MANAGED_SLACK, _MANAGED_EMAIL], ids=["slack", "email"])
def test_delete_with_managed_settings_is_409_and_keeps_both(client_pg, db, admin, columns):
    tenant = _seed(db)
    _seed_settings(db, tenant.tenant_id, **columns)
    resp = client_pg.delete(f"{BASE}/{tenant.tenant_id}")
    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert _tenant_count(db) == 1 and _settings_rows(db) == 1


def test_delete_blocked_by_a_member_keeps_the_disposable_settings(client_pg, db, admin, make_auth_session):
    tenant = _seed(db)
    _seed_settings(db, tenant.tenant_id, **_LEGACY)
    make_auth_session(UserRole.VIEWER, tenant_id=tenant.tenant_id)
    assert client_pg.delete(f"{BASE}/{tenant.tenant_id}").status_code == 409
    assert _tenant_count(db) == 1 and _settings_rows(db) == 1


# ----- 재발급 · 정지 ---------------------------------------------------------------

@pytest.mark.parametrize("status", list(TenantStatus))
def test_onboarding_reissues_the_same_external_id_in_every_status(client_pg, db, admin, status):
    tenant = _seed(db, status)
    resp = client_pg.get(f"{BASE}/{tenant.tenant_id}/onboarding")
    assert resp.status_code == 200
    assert resp.json()["external_id"] == tenant.external_id
    assert resp.json()["onboarding_url"].startswith("https://ap-northeast-2.console.aws.amazon.com/")


def test_onboarding_without_settings_is_503(client_pg, db, admin, onboarding):
    tenant = _seed(db, TenantStatus.ACTIVE)
    onboarding(False)
    resp = client_pg.get(f"{BASE}/{tenant.tenant_id}/onboarding")
    assert resp.status_code == 503 and _code(resp) == "TENANT_ONBOARDING_UNAVAILABLE"


def test_suspend_active_tenant(client_pg, db, admin):
    tenant = _seed(db, TenantStatus.ACTIVE)
    resp = client_pg.post(f"{BASE}/{tenant.tenant_id}/suspend", json={"reason": "해지"})
    assert resp.status_code == 200
    body = TenantDetail.model_validate(resp.json())
    assert (body.status, body.suspended_reason) == (TenantStatus.SUSPENDED, "해지")
    # 두 번째 정지는 받는 상태가 아니다
    again = client_pg.post(f"{BASE}/{tenant.tenant_id}/suspend", json={"reason": "다시"})
    assert again.status_code == 409 and _code(again) == "TENANT_STATE_CONFLICT"
    assert tenants_repo.get_tenant(db, tenant.tenant_id).tenant.suspended_reason == "해지"


def test_suspend_pending_tenant_is_409(client_pg, db, admin):
    tenant = _seed(db)
    resp = client_pg.post(f"{BASE}/{tenant.tenant_id}/suspend", json={"reason": "해지"})
    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert tenants_repo.get_tenant(db, tenant.tenant_id).tenant.status is TenantStatus.PENDING_CONNECTION


def test_tenant_deleted_between_read_and_transition_is_404_not_409(client_pg, db, admin, monkeypatch):
    # 처음 읽은 뒤 다른 관리자가 지웠다 → 전이는 0행. 「상태가 맞지 않다」가 아니라 「없다」로 답한다
    tenant = _seed(db, TenantStatus.ACTIVE)

    def vanish(session, tenant_id, **_):
        session.execute(delete(models.Tenant).where(models.Tenant.tenant_id == tenant_id))
        return False

    monkeypatch.setattr(tenants_repo, "suspend_tenant", vanish)
    resp = client_pg.post(f"{BASE}/{tenant.tenant_id}/suspend", json={"reason": "해지"})
    assert resp.status_code == 404 and _code(resp) == "TENANT_NOT_FOUND"


def test_suspend_without_reason_is_422(client_pg, db, admin):
    tenant = _seed(db, TenantStatus.ACTIVE)
    assert client_pg.post(f"{BASE}/{tenant.tenant_id}/suspend", json={}).status_code == 422


# ----- 구독 (T-16 · #528) ----------------------------------------------------------

PAID_SUB = {"plan": "Business", "period_start": "2026-10-31", "period_end": "2026-11-29",
            "cancel_at_period_end": True, "billing_anchor_day": 31, "amount_minor": 990000, "currency": "KRW"}
MOCK_ON = SubscriptionSettings(_env_file=None, SUBSCRIPTION_MOCK_MARKETPLACE_ENABLED=True)


def test_new_tenant_starts_free_and_the_list_carries_every_subscription_field(client_pg, db, admin):
    created = client_pg.post(BASE, json=CREATE).json()["tenant"]
    free = {"plan": "Free", "period_start": None, "period_end": None, "cancel_at_period_end": False,
            "billing_anchor_day": None, "amount_minor": None, "currency": None}
    assert (created["subscription"], created["subscription_source"], created["entitlement_ends_at"]) == (
        free, "APP_FREE", None)
    [row] = client_pg.get(BASE).json()["tenants"]
    assert (row["subscription"], row["subscription_source"]) == (free, "APP_FREE")


def test_list_shows_null_for_an_unregistered_legacy_tenant(client_pg, db, admin):
    tenant = _seed(db, TenantStatus.ACTIVE)
    # 이 변경 전의 테넌트 — 구독이 등록되지 않았다(Free 로 읽지 않는다)
    db.execute(update(models.Tenant).where(models.Tenant.tenant_id == tenant.tenant_id).values(
        plan=None, subscription_source=None))
    db.commit()
    [row] = client_pg.get(BASE).json()["tenants"]
    assert "subscription" in row and (row["subscription"], row["subscription_source"]) == (None, None)


def test_patch_full_subscription_then_unrelated_patch_keeps_schedule_and_anchor(client_pg, db, admin):
    tenant = _seed(db, TenantStatus.ACTIVE)
    resp = client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={"subscription": PAID_SUB})
    assert resp.status_code == 200 and resp.json()["subscription"] == PAID_SUB
    assert resp.json()["subscription_source"] == "MANUAL"

    resp = client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={"name": "학원 본점"})
    assert resp.status_code == 200 and resp.json()["subscription"] == PAID_SUB
    [row] = client_pg.get(BASE).json()["tenants"]
    assert row["subscription"] == PAID_SUB


def test_patch_with_the_old_shape_replaces_the_whole_subscription(client_pg, db, admin):
    tenant = _seed(db, TenantStatus.ACTIVE)
    assert client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={"subscription": PAID_SUB}).status_code == 200
    resp = client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={
        "subscription": {"plan": "pilot", "period_start": "2026-11-01", "period_end": "2026-12-31"}})
    assert resp.status_code == 200
    # 기준일 · 예약 · 요금이 옛 기간에 남지 않는다
    assert resp.json()["subscription"] == {
        "plan": "pilot", "period_start": "2026-11-01", "period_end": "2026-12-31", "cancel_at_period_end": False,
        "billing_anchor_day": None, "amount_minor": None, "currency": None}


@pytest.mark.parametrize("manual", [
    pytest.param({"plan": "Business", "period_start": "2026-10-01", "period_end": "2026-10-31"}, id="three-fields"),
    # 옛 규칙의 예약 · 기준일 칸이 있어도 기록일 뿐이다 — 로컬 자동 갱신 · Free 전환이 없다
    pytest.param(PAID_SUB, id="legacy-anchor-and-cancel"),
])
def test_manual_records_are_never_renewed_or_converted_by_the_contract_cycle(client_pg, db, admin, manual):
    tenant = _seed(db, TenantStatus.ACTIVE)
    resp = client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={"subscription": manual})
    expected = {"cancel_at_period_end": False, "billing_anchor_day": None, "amount_minor": None,
                "currency": None} | manual
    assert resp.status_code == 200 and resp.json()["subscription"] == expected

    after_end = datetime(2027, 1, 5, tzinfo=UTC)
    subscription_workflow.process_subscription_cycle(
        db, settings=MOCK_ON, sweep=subscription_workflow.EventSweep(), now=after_end)
    detail = client_pg.get(f"{BASE}/{tenant.tenant_id}").json()
    assert (detail["subscription"], detail["subscription_source"], detail["status"]) == (expected, "MANUAL", "ACTIVE")
    shown = subscription_workflow.get_subscription(
        db, tenant.tenant_id, can_manage=False, settings=MOCK_ON, now=after_end)
    assert (shown.next_renewal_date, shown.expiry_date, shown.agreement) == (None, None, None)


def _mock_agreement(db, tenant, *, activate: bool):
    """고객사 ADMIN 의 모의 구매(→ 활성화). 운영처럼 commit 해 둔다."""
    buyer = auth_repo.create_user(
        db, issuer="https://issuer.example", cognito_sub=f"buyer-{tenant.tenant_id}",
        email=f"buyer-{tenant.tenant_id[:8]}@example.com",
        role=UserRole.ADMIN, account_type=AccountType.TENANT, tenant_id=tenant.tenant_id, now=NOW,
        email_verified_at=NOW)
    db.commit()
    subscription_workflow.purchase_subscription(
        db, tenant.tenant_id, SubscriptionPurchaseRequest(plan="Business"), requested_by_user_id=buyer.user_id,
        authorize=lambda: None, settings=MOCK_ON, now=NOW)
    if activate:
        subscription_workflow.process_subscription_cycle(
            db, settings=MOCK_ON, sweep=subscription_workflow.EventSweep(), now=NOW + timedelta(minutes=1))


@pytest.mark.parametrize("activate", [False, True], ids=["pending", "active"])
def test_patch_cannot_erase_a_valid_mock_agreement(client_pg, db, admin, activate):
    tenant = _seed(db, TenantStatus.ACTIVE)
    _mock_agreement(db, tenant, activate=activate)
    before = client_pg.get(f"{BASE}/{tenant.tenant_id}").json()

    for subscription in (PAID_SUB, {"plan": "Free"}, None):
        resp = client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={"subscription": subscription})
        assert (resp.status_code, _code(resp)) == (409, "TENANT_STATE_CONFLICT")
    after = client_pg.get(f"{BASE}/{tenant.tenant_id}").json()
    assert after == before
    assert after["subscription_source"] == ("MOCK_MARKETPLACE" if activate else "APP_FREE")
    if activate:
        assert after["subscription"]["plan"] == "Business" and after["entitlement_ends_at"] is not None
    # 구독 밖의 칸만 보낸 PATCH 는 받는다
    resp = client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={"name": "학원 본점"})
    assert resp.status_code == 200 and resp.json()["subscription"] == before["subscription"]


@pytest.mark.parametrize("subscription", [
    # 예약의 전환일(period_end 다음 날)이 달력 밖 — 500 이 아니라 요청 검증 오류다
    pytest.param({"plan": "Business", "period_start": "2026-10-01", "period_end": "9999-12-31",
                  "cancel_at_period_end": True}, id="cancel-on-last-calendar-day"),
    pytest.param(PAID_SUB | {"period_end": "2026-11-30"}, id="period-not-one-anchored-month"),
    pytest.param({"plan": "pilot", "period_end": "2026-12-31", "cancel_at_period_end": True}, id="legacy-cancel"),
    pytest.param(PAID_SUB | {"currency": None}, id="amount-without-currency"),
    pytest.param(PAID_SUB | {"amount_minor": -1}, id="negative-amount"),
])
def test_patch_with_an_incoherent_subscription_is_422_and_changes_nothing(client_pg, db, admin, subscription):
    tenant = _seed(db, TenantStatus.ACTIVE)
    resp = client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={"subscription": subscription})
    assert resp.status_code == 422 and _code(resp) == "REQUEST_VALIDATION_FAILED"
    assert tenants_repo.get_tenant(db, tenant.tenant_id).tenant.plan == "Free"


def test_admin_suspend_keeps_the_last_subscription(client_pg, db, admin):
    tenant = _seed(db, TenantStatus.ACTIVE)
    assert client_pg.patch(f"{BASE}/{tenant.tenant_id}", json={"subscription": PAID_SUB}).status_code == 200
    resp = client_pg.post(f"{BASE}/{tenant.tenant_id}/suspend", json={"reason": "해지"})
    assert resp.status_code == 200
    assert (resp.json()["status"], resp.json()["subscription"]) == ("SUSPENDED", PAID_SUB)


def test_admin_suspend_does_not_cancel_a_mock_agreement(client_pg, db, admin):
    tenant = _seed(db, TenantStatus.ACTIVE)
    _mock_agreement(db, tenant, activate=True)
    before = client_pg.get(f"{BASE}/{tenant.tenant_id}").json()
    resp = client_pg.post(f"{BASE}/{tenant.tenant_id}/suspend", json={"reason": "해지"})
    assert resp.status_code == 200
    assert (resp.json()["status"], resp.json()["subscription"], resp.json()["subscription_source"]) == (
        "SUSPENDED", before["subscription"], "MOCK_MARKETPLACE")
