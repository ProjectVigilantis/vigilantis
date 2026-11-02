"""T-5 테넌트 격리 회귀 테스트 — 경로 전체 회귀(#529 기대값 17행).

검증 층은 **DB 행 단위 격리**(pytest + PostgreSQL)다. 플랫폼(ECS·RDS) 위 E2E는
Cognito 전달·고객 계정 연결(#523) 뒤의 별도 카드이며 이 파일의 범위가 아니다.
ADR-0012 §3 ②(IAM 회수 = 앱 밖 운영자 권한)도 이 파일 밖이다 — 아래 「정지 409」는
앱 안 상태 전이만 본다.

판정 순서(계약 원문 `packages/schemas/api/tenants.py` 머리말 「기존 경로의 테넌트 범위」):
    역할(403) → 범위(404) → 같은 Key 재요청 → 정지(409) → 상태(409)

전제(모두 `dev`에 있음 — 1~15·16a를 켠다):
    ① `CurrentUser.account_type`·`tenant_id`(T-1 PR 4 = PR #527 · `auth/service.py`)
    ② 인시던트 목록·상세·실행·종료 경로의 테넌트 범위(T-4 2단계 = #533 C = PR #550 · `routers/incidents.py`)
    ③ 픽스처 `make_tenant(status=None)` · `make_auth_session(role, tenant_id=...)` · `login_as(role, tenant_id=...)`
      (PR #527 conftest — 기본은 플랫폼 ADMIN, 고객사 세션은 `login_as(role, tenant_id=make_tenant())`)
    ④ PR #527의 임시 업무 관문(`require_business_access`) 해제 — 걷는 자리는 경로별로
      인시던트·조치·자산 목록 = PR #550(걷힘) / 메트릭 시계열 = #536(16b 보류) / WebSocket = #535(17 보류)

분담(#529 10/28): 경로 전체 회귀는 이 파일이, 판정 순서의 **경계**(정지 전 접수 Key 재요청 200 ·
소속 없는 MVP 기록 404)는 `test_incident_tenant_scope.py`(PR #550)가 맡는다 — 같은 기대값을 두 파일이
지키지 않도록 18·19를 더하지 않는다. 시드·호출 헬퍼도 그 파일 것을 그대로 쓴다(`two_tenants` =
테넌트 A·B 각 인시던트 1건(승인 대기 · EXECUTABLE 후보 1) + 소속 없는 MVP 기록 1건 · `published` =
WS 발행 가로채기).

이슈: #529(T-5) — 기대값 17행·전제·열린 질문의 원천.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schemas.api.auth import UserRole
from schemas.collections import CollectionRunStatus
from tests.test_asset_tenant_scope import MVP_ACCOUNT, REGION, _collected
from tests.test_incident_tenant_scope import (
    _code,
    _effects,
    _execute,
    _executions,
    _key,
    _resolve,
    _suspend,
    published,  # noqa: F401 — 픽스처 재사용
    two_tenants,  # noqa: F401 — 픽스처 재사용
)

INCIDENTS = "/api/v1/incidents"


def _listed(client) -> set[str]:
    resp = client.get(INCIDENTS)
    assert resp.status_code == 200, resp.json()
    return {item["incident_id"] for item in resp.json()["items"]}


def _detail(client, incident):
    return client.get(f"{INCIDENTS}/{incident.incident_id}")


# --- 범위: A 세션 → B 인시던트 -------------------------------------------------


def test_list_hides_other_tenants_incidents(client_pg, login_as, two_tenants):
    """1. A-OPERATOR `GET /incidents` → 200, items에 A만, B `incident_id` 없음."""
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    assert _listed(client_pg) == {two_tenants["mine"].incident_id}


def test_detail_of_other_tenant_is_404(client_pg, login_as, two_tenants):
    """2. A-OPERATOR `GET /incidents/{B}` → 404 `INCIDENT_NOT_FOUND`(403 아님 — 존재가 새지 않는다)."""
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    resp = _detail(client_pg, two_tenants["theirs"])
    assert (resp.status_code, _code(resp)) == (404, "INCIDENT_NOT_FOUND")


def test_execute_on_other_tenant_is_404_and_writes_nothing(db, client_pg, login_as, two_tenants):
    """3. A-OPERATOR `POST /actions/execute`(B 후보) → 404 · `executions` 행 수 변화 0."""
    theirs = two_tenants["theirs"]
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    before = len(_executions(db, theirs))

    resp = _execute(client_pg, theirs, _key())

    assert (resp.status_code, _code(resp)) == (404, "INCIDENT_NOT_FOUND")
    assert len(_executions(db, theirs)) == before == 0


def test_resolve_of_other_tenant_is_404_and_changes_nothing(db, client_pg, login_as, two_tenants, published):
    """4. A-OPERATOR `POST /incidents/{B}/resolve` → 404 · B 상태 그대로 · WS 발행 0."""
    theirs = two_tenants["theirs"]
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    before = _effects(db, theirs)

    resp = _resolve(client_pg, theirs)

    assert (resp.status_code, _code(resp)) == (404, "INCIDENT_NOT_FOUND")
    assert _effects(db, theirs) == before
    assert published == []


# --- 역할이 범위보다 먼저 -------------------------------------------------------


def test_viewer_cannot_execute_own_tenant_403(db, client_pg, login_as, two_tenants):
    """5. A-VIEWER `POST /actions/execute`(A 후보) → 403 `AUTH_FORBIDDEN`."""
    mine = two_tenants["mine"]
    login_as(UserRole.VIEWER, tenant_id=two_tenants["a"])

    resp = _execute(client_pg, mine, _key())

    assert (resp.status_code, _code(resp)) == (403, "AUTH_FORBIDDEN")
    assert _executions(db, mine) == []


def test_viewer_on_other_tenant_is_403_not_404(client_pg, login_as, two_tenants):
    """6. A-VIEWER `POST /incidents/{B}/resolve` → 403(404 아님 — 역할 검사가 대상을 읽기 전)."""
    login_as(UserRole.VIEWER, tenant_id=two_tenants["a"])
    resp = _resolve(client_pg, two_tenants["theirs"])
    assert (resp.status_code, _code(resp)) == (403, "AUTH_FORBIDDEN")


# --- 플랫폼 운영자 · 대조군 -----------------------------------------------------


def test_platform_operator_lists_every_tenant(client_pg, two_tenants):
    """7. 플랫폼 운영자 `GET /incidents` → 200 · A+B(ADR-0012 §4)."""
    # client_pg 기본 세션 = PLATFORM ADMIN
    assert {two_tenants["mine"].incident_id, two_tenants["theirs"].incident_id} <= _listed(client_pg)


def test_platform_operator_reads_and_resolves_other_tenant(client_pg, two_tenants):
    """8. 플랫폼 운영자 `GET /incidents/{B}` · `resolve` → 200."""
    theirs = two_tenants["theirs"]
    assert _detail(client_pg, theirs).status_code == 200

    resolved = _resolve(client_pg, theirs)

    assert resolved.status_code == 200, resolved.json()
    assert resolved.json()["status"] == "RESOLVED"


def test_own_tenant_detail_and_resolve_are_200(client_pg, login_as, two_tenants):
    """9. A-OPERATOR `GET /incidents/{A}` · `resolve` → 200(격리가 자기 것까지 막지 않는다)."""
    mine = two_tenants["mine"]
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    assert _detail(client_pg, mine).status_code == 200

    resolved = _resolve(client_pg, mine)

    assert resolved.status_code == 200, resolved.json()
    assert _detail(client_pg, mine).json()["status"] == "RESOLVED"


# --- 정지(SUSPENDED) --------------------------------------------------------------


def test_suspended_tenant_execute_is_409_for_its_operator(db, client_pg, login_as, two_tenants):
    """10. B 정지 · B-OPERATOR `POST /actions/execute`(B) → 409 `TENANT_SUSPENDED`."""
    theirs = two_tenants["theirs"]
    _suspend(db, two_tenants["b"])
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["b"])

    resp = _execute(client_pg, theirs, _key())

    assert (resp.status_code, _code(resp)) == (409, "TENANT_SUSPENDED")
    assert _executions(db, theirs) == []


def test_suspended_tenant_execute_is_409_for_platform_operator(db, client_pg, two_tenants):
    """11. B 정지 · 플랫폼 운영자 `POST /actions/execute`(B) → 409(운영자 포함)."""
    theirs = two_tenants["theirs"]
    _suspend(db, two_tenants["b"])

    resp = _execute(client_pg, theirs, _key())

    assert (resp.status_code, _code(resp)) == (409, "TENANT_SUSPENDED")
    assert _executions(db, theirs) == []


def test_suspended_tenant_resolve_is_409_for_its_operator(db, client_pg, login_as, two_tenants, published):
    """12. B 정지 · B-OPERATOR `resolve`(B) → 409."""
    theirs = two_tenants["theirs"]
    _suspend(db, two_tenants["b"])
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["b"])
    before = _effects(db, theirs)

    resp = _resolve(client_pg, theirs)

    assert (resp.status_code, _code(resp)) == (409, "TENANT_SUSPENDED")
    assert _effects(db, theirs) == before
    assert published == []


def test_suspended_tenant_resolve_is_200_for_platform_operator(db, client_pg, two_tenants):
    """13. B 정지 · 플랫폼 운영자 `resolve`(B) → 200."""
    theirs = two_tenants["theirs"]
    _suspend(db, two_tenants["b"])

    resolved = _resolve(client_pg, theirs)

    assert resolved.status_code == 200, resolved.json()
    assert resolved.json()["status"] == "RESOLVED"


def test_other_tenants_suspension_is_404_not_409(db, client_pg, login_as, two_tenants):
    """14. B 정지 · A-OPERATOR `POST /actions/execute`(B) → 404(409 아님 — 범위 → 정지 순서 · 정지 사실이 새지 않는다)."""
    theirs = two_tenants["theirs"]
    _suspend(db, two_tenants["b"])
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])

    resp = _execute(client_pg, theirs, _key())

    assert (resp.status_code, _code(resp)) == (404, "INCIDENT_NOT_FOUND")
    assert _executions(db, theirs) == []


# --- 회귀 1건 · 자산·메트릭 · WS -------------------------------------------------


def test_unauthenticated_list_is_401(client_pg_anon):
    """15. 비로그인 `GET /incidents` → 401(#502 회귀 방지 1건)."""
    resp = client_pg_anon.get(INCIDENTS)
    assert (resp.status_code, _code(resp)) == (401, "AUTH_REQUIRED")


def test_assets_hide_other_tenant(db, client_pg, login_as, make_tenant, monkeypatch):
    """16a. A-OPERATOR `GET /assets` → B 자산·MVP(테넌트 없음) 자산 없음 · 수집 상태·마지막 수집 시각도
    자기 테넌트 회차만(필터 · 404 자리 없음) — PR #550."""
    from db import models

    monkeypatch.setattr("routers.assets._configured_regions", lambda: [REGION])
    a, b = make_tenant(), make_tenant()
    acct_a, acct_b = (db.get(models.Tenant, t).aws_account_id for t in (a, b))
    t0 = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)
    # A 는 가장 오래된 성공 회차, B 와 소속 없는 기록은 더 새 실패 회차 — 범위가 새면 A 의 상태 · 시각이 바뀐다
    mine = _collected(
        db, tenant_id=a, account=acct_a, instance="i-0a5",
        status=CollectionRunStatus.SUCCESS, finished_at=t0,
    )
    theirs = _collected(
        db, tenant_id=b, account=acct_b, instance="i-0b5", status=CollectionRunStatus.FAILED,
        finished_at=t0 + timedelta(minutes=5), error_summary='{"auto_scaling_groups": "AccessDenied"}',
    )
    mvp = _collected(
        db, tenant_id=None, account=MVP_ACCOUNT, instance="i-0c5", status=CollectionRunStatus.PARTIAL,
        finished_at=t0 + timedelta(minutes=10), error_summary='{"alb_target_groups": "Throttling"}',
    )
    db.commit()

    login_as(UserRole.OPERATOR, tenant_id=a)
    resp = client_pg.get("/api/v1/assets")

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    arns = {item["arn"] for item in body["items"]}
    assert mine.arn in arns
    assert arns.isdisjoint({theirs.arn, mvp.arn})
    assert body["collection_status"] == "READY"
    assert datetime.fromisoformat(body["last_collected_at"].replace("Z", "+00:00")) == t0
    assert body["uncollected"] == []


@pytest.mark.skip(reason="보류 — #536(메트릭 시계열 테넌트 범위) 머지 뒤 켠다(#529 16b)")
def test_metrics_timeseries_hide_other_tenant(client_pg, db, login_as, make_tenant):
    """16b. A-OPERATOR `GET /metrics/timeseries`(B 자산) → B 시계열 없음(필터 · 404 자리 없음) — #536."""
    raise NotImplementedError("T-5 16b — #536 머지 뒤 채운다")


@pytest.mark.skip(reason="보류 — #535(WebSocket 테넌트 범위) 머지 뒤 켠다(#529 17 · 전제 ③ WS)")
def test_ws_does_not_deliver_other_tenants_events(ws_client, db, make_tenant):
    """17. A WS 연결 중 B 인시던트 갱신 → 이벤트 미수신 — #535(`ws_client`는 플랫폼 세션이라 고객사 WS 세션 픽스처도 그 카드 뒤)."""
    raise NotImplementedError("T-5 17 — #535 머지 뒤 채운다")
