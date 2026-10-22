"""/api/v1/tenants — 고객(테넌트) 관리 라우터 (Post-MVP T-4). 실제 PostgreSQL + ADMIN 세션.

지키는 것.
  ① ADMIN 전용 · 미인증 401 — 등록된 경로 전부. 연결 확인(verify · resume)은 아직 없다.
  ② 받는 상태 밖은 409, 없거나 형식이 어긋난 식별자는 404 — 거절하면 DB 가 그대로다.
  ③ 온보딩 링크 설정이 없으면 링크를 싣는 응답만 503 이고, 그때 쓰기도 남지 않는다.
  ④ external_id 는 생성 응답과 재발급에서만 나간다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from config import TenantSettings, get_tenant_settings
from db import models
from db.repositories import tenants as tenants_repo
from fastapi.routing import iter_route_contexts
from schemas.api.auth import UserRole
from schemas.api.tenants import (
    TenantCreateResponse,
    TenantDetail,
    TenantStatus,
    TenantVerificationResult,
)
from sqlalchemy import delete, func, select

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
    ("POST", "", CREATE),
    ("GET", "/{id}", None),
    ("PATCH", "/{id}", {"name": "x"}),
    ("DELETE", "/{id}", None),
    ("GET", "/{id}/onboarding", None),
    ("POST", "/{id}/suspend", {"reason": "해지"}),
]


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
    return login_as(UserRole.ADMIN)


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
        for method in route.methods or ()
    }
    covered = {(method, BASE + path.replace("{id}", "{tenant_id}")) for method, path, _ in REQUESTS}
    assert covered == registered
    # 연결 확인은 테넌트별 AssumeRole 과 함께 다음 카드다
    assert not any(path.endswith(("/verify", "/resume")) for _, path in registered)


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
@pytest.mark.parametrize("method,path,body", REQUESTS)
def test_non_admin_is_forbidden_everywhere(client_pg, db, login_as, onboarding, role, method, path, body):
    tenant = _seed(db)
    login_as(role)
    resp = client_pg.request(method, BASE + path.replace("{id}", tenant.tenant_id), json=body)
    assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"
    assert _tenant_count(db) == 1
    assert tenants_repo.get_tenant(db, tenant.tenant_id).tenant.name == "학원"


@pytest.mark.parametrize("method,path,body", [r for r in REQUESTS if r[0] != "GET"])
def test_state_changing_requests_from_another_origin_are_403(client_pg, db, admin, method, path, body):
    # 라우터의 Origin 관문(main.py protected)은 require_admin 이 세션을 따로 보기 때문에 401 · 403 테스트로는
    # 빠져도 드러나지 않는다 — 허용 밖 Origin 의 상태 변경이 본문 전에 막히는지 따로 본다(#519 리뷰)
    tenant = _seed(db)
    resp = client_pg.request(method, BASE + path.replace("{id}", tenant.tenant_id), json=body,
                             headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403 and _code(resp) == "AUTH_ORIGIN_REJECTED"
    assert _tenant_count(db) == 1
    assert tenants_repo.get_tenant(db, tenant.tenant_id).tenant.name == "학원"


def test_unauthenticated_is_401(client_pg):
    assert client_pg.get(BASE, headers={"Cookie": ""}).status_code == 401


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
    member = make_auth_session(UserRole.VIEWER)
    db.get(models.AppUser, member.user_id).tenant_id = tenant.tenant_id
    db.flush()
    resp = client_pg.delete(f"{BASE}/{tenant.tenant_id}")
    assert resp.status_code == 409 and _code(resp) == "TENANT_STATE_CONFLICT"
    assert _tenant_count(db) == 1
    assert db.get(models.AppUser, member.user_id).tenant_id == tenant.tenant_id


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
