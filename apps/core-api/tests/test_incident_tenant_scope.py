# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 인시던트 경로의 테넌트 범위 (Post-MVP T-4 2단계 C · #533) — 실제 PostgreSQL · 실제 라우터.
#
# 계약: packages/schemas/api/tenants.py 머리말 「기존 경로의 테넌트 범위」(PR #516).
#   - 범위 밖은 없는 것이다 — 목록에서 빠지고 상세 · 실행 · 종료는 404 INCIDENT_NOT_FOUND.
#     플랫폼 계정은 전부 본다. 테넌트 없는 MVP 기록(tenant_id NULL)은 어느 고객사에도 속하지 않는다.
#   - 판정 순서: 역할(403) → 범위(404) → 같은 Key 재요청(200 재생) → 대상 인시던트 테넌트의 정지(409)
#     → 인시던트 · 제안 상태(409). 범위를 정지보다 먼저 봐야 남의 정지 여부가 새지 않고, 재요청을 정지보다
#     먼저 봐야 정지 전에 접수된 실행의 재요청이 거절로 읽히지 않는다.
#   - 정지된 테넌트: 새 실행은 플랫폼 계정을 포함해 409, 종료는 고객사 사용자만 409(플랫폼은 받는다).
#
# 박지현 님의 T-5(#529)는 같은 계약을 경로 전체 회귀로 덮고, 이 파일은 판정 순서와 경계에 집중한다.
# ==============================================================================

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import workflows
from db import models
from db.repositories import tenants as tenants_repo
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.auth import UserRole
from schemas.incidents import AgentInvocationStatus
from schemas.runbooks import ROLLBACK_RUNBOOK_BY_MAIN_ID, RunbookId

EXECUTE = "/api/v1/actions/execute"
KEY = "f1d0c5e2-3a4b-4c5d-8e9f-0a1b2c3d4e5f"
NOW = datetime(2026, 10, 27, 4, 0, tzinfo=UTC)
SUMMARY = ["요약 1", "요약 2", "요약 3"]


def _code(resp) -> str:
    return resp.json()["error"]["code"]


def _incident(db, make_incident, make_executable, tenant_id: str | None) -> models.Incident:
    """분석이 끝나 승인 대기인 인시던트 + 실행 가능한 제안 하나 — 상세 · 실행 · 종료가 모두 받는 모양.

    종료는 SecOps 분석이 끝나야 받는다(workflows.resolve_incident) — 분석 대기로 두면 종료가 정지와 무관하게
    INCIDENT_NOT_RESOLVABLE 이 된다.
    """
    incident = make_incident(db, summary_lines=SUMMARY)
    incident.agent_invocation_status = AgentInvocationStatus.SUCCEEDED
    make_executable(db, incident=incident)
    incident.tenant_id = tenant_id
    db.commit()
    return incident


def _execute(client, incident: models.Incident, key: str = KEY):
    return client.post(EXECUTE, json={
        "incident_id": incident.incident_id,
        "runbook_id": RunbookId.RUNBOOK_NACL_ADD_DENY.value,
        "idempotency_key": key,
    })


def _resolve(client, incident: models.Incident):
    return client.post(f"/api/v1/incidents/{incident.incident_id}/resolve",
                       json={"resolution": "NO_FURTHER_ACTION"})


def _suspend(db, tenant_id: str) -> None:
    assert tenants_repo.suspend_tenant(db, tenant_id, reason="해지", now=NOW)
    db.commit()


@pytest.fixture()
def two_tenants(db, make_tenant, make_incident, make_executable):
    """테넌트 A · B 와 각자의 인시던트 하나, 그리고 테넌트 없는 MVP 기록 하나."""
    a, b = make_tenant(), make_tenant()
    return {
        "a": a, "b": b,
        "mine": _incident(db, make_incident, make_executable, a),
        "theirs": _incident(db, make_incident, make_executable, b),
        "mvp": _incident(db, make_incident, make_executable, None),
    }


# ----- 범위 밖은 없는 것이다 ------------------------------------------------------

def test_tenant_account_lists_only_its_own_incidents(client_pg, login_as, two_tenants):
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    resp = client_pg.get("/api/v1/incidents")
    assert resp.status_code == 200
    assert {item["incident_id"] for item in resp.json()["items"]} == {two_tenants["mine"].incident_id}


def test_platform_account_lists_every_incident(client_pg, two_tenants):
    # client_pg 기본 세션 = PLATFORM ADMIN
    ids = {item["incident_id"] for item in client_pg.get("/api/v1/incidents").json()["items"]}
    assert {two_tenants[k].incident_id for k in ("mine", "theirs", "mvp")} <= ids


@pytest.mark.parametrize("target", ["theirs", "mvp"])
def test_out_of_scope_detail_is_404(client_pg, login_as, two_tenants, target):
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    resp = client_pg.get(f"/api/v1/incidents/{two_tenants[target].incident_id}")
    assert resp.status_code == 404 and _code(resp) == "INCIDENT_NOT_FOUND"


def test_own_detail_is_200(client_pg, login_as, two_tenants):
    login_as(UserRole.VIEWER, tenant_id=two_tenants["a"])
    assert client_pg.get(f"/api/v1/incidents/{two_tenants['mine'].incident_id}").status_code == 200


@pytest.mark.parametrize("call", [_execute, _resolve], ids=["execute", "resolve"])
def test_out_of_scope_change_is_404_and_writes_nothing(db, client_pg, login_as, two_tenants, call):
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    theirs = two_tenants["theirs"]
    resp = call(client_pg, theirs)
    assert resp.status_code == 404 and _code(resp) == "INCIDENT_NOT_FOUND"
    db.expire_all()
    incident = db.get(models.Incident, theirs.incident_id)
    assert incident.status.value == "AWAITING_APPROVAL"
    assert db.query(models.ActionExecution).filter_by(incident_id=theirs.incident_id).count() == 0


# ----- 판정 순서 --------------------------------------------------------------

def test_role_comes_before_scope(client_pg, login_as, two_tenants):
    # VIEWER 는 대상과 무관하게 403 — 대상을 읽기 전이라 존재가 새지 않는다
    login_as(UserRole.VIEWER, tenant_id=two_tenants["a"])
    for target in ("mine", "theirs"):
        resp = _execute(client_pg, two_tenants[target])
        assert resp.status_code == 403 and _code(resp) == "AUTH_FORBIDDEN"


def test_scope_comes_before_suspension(db, client_pg, login_as, two_tenants):
    # 남의 테넌트가 정지됐는지가 409 로 새지 않는다
    _suspend(db, two_tenants["b"])
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    resp = _execute(client_pg, two_tenants["theirs"])
    assert resp.status_code == 404 and _code(resp) == "INCIDENT_NOT_FOUND"


def test_replay_of_an_execution_accepted_before_suspension_is_200(db, client_pg, login_as, two_tenants):
    # 정지 전에 접수된 실행을 같은 Key 로 다시 보내면 200 재생이다 — 재요청은 새 예약을 만들지 않는다(PR #516 리뷰)
    mine = two_tenants["mine"]
    accepted = workflows.reserve_execution(db, ExecuteActionRequest(
        incident_id=mine.incident_id, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY, idempotency_key=KEY))
    db.commit()
    _suspend(db, two_tenants["a"])
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    resp = _execute(client_pg, mine)
    assert resp.status_code == 200
    assert resp.json()["execution_id"] == accepted.response.execution_id


def test_scope_comes_before_replay(db, client_pg, login_as, two_tenants):
    # 다른 테넌트의 Key 를 알아도 그 실행 응답이 재생되지 않는다 — 재요청은 범위 뒤 자리다(계약)
    theirs = two_tenants["theirs"]
    workflows.reserve_execution(db, ExecuteActionRequest(
        incident_id=theirs.incident_id, runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY, idempotency_key=KEY))
    db.commit()
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    resp = _execute(client_pg, theirs)
    assert resp.status_code == 404 and _code(resp) == "INCIDENT_NOT_FOUND"


# ----- 정지된 테넌트 ------------------------------------------------------------

def test_new_execution_on_a_suspended_tenant_is_409_even_for_the_platform(db, client_pg, two_tenants):
    # 정지는 「요청자의 소속」이 아니라 대상 인시던트의 테넌트로 본다 — 플랫폼 계정도 새 실행은 409(ADR-0012 §3)
    _suspend(db, two_tenants["a"])
    resp = _execute(client_pg, two_tenants["mine"], key="0e7c9a8b-1111-4222-8333-444455556666")
    assert resp.status_code == 409 and _code(resp) == "TENANT_SUSPENDED"


def test_resolve_on_a_suspended_tenant_is_409_for_its_users_but_allowed_for_the_platform(
    db, client_pg, login_as, two_tenants
):
    _suspend(db, two_tenants["a"])
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    resp = _resolve(client_pg, two_tenants["mine"])
    assert resp.status_code == 409 and _code(resp) == "TENANT_SUSPENDED"
    login_as(UserRole.ADMIN)  # PLATFORM ADMIN — 해지한 고객의 열린 인시던트 정리
    resp = _resolve(client_pg, two_tenants["mine"])
    assert resp.status_code == 200, resp.json()


def test_resolve_replay_on_a_suspended_tenant_is_still_409_for_its_users(db, client_pg, login_as, two_tenants):
    # 종료의 재요청(이미 종료면 200)은 Key 가 아니라 상태로 판정하므로 정지 뒤 자리다(계약)
    assert _resolve(client_pg, two_tenants["mine"]).status_code == 200  # PLATFORM ADMIN 이 먼저 종료
    _suspend(db, two_tenants["a"])
    login_as(UserRole.OPERATOR, tenant_id=two_tenants["a"])
    resp = _resolve(client_pg, two_tenants["mine"])
    assert resp.status_code == 409 and _code(resp) == "TENANT_SUSPENDED"


@pytest.mark.parametrize(("suspended", "expected"), [(False, 202), (True, 409)], ids=["active", "suspended"])
def test_operator_recovery_is_a_new_execution(db, client_pg, make_execution, two_tenants, suspended, expected):
    # 관제자 복구(롤백 접수)도 고객 계정에 새로 들어가는 실행이다 — 정지 뒤에는 플랫폼 계정도 409
    mine = two_tenants["mine"]
    origin = make_execution(db, mine, runbook_id=RunbookId.RUNBOOK_EC2_RIGHTSIZING, status=ExecutionStatus.SUCCESS)
    db.commit()
    if suspended:
        _suspend(db, two_tenants["a"])
    resp = client_pg.post(EXECUTE, json={
        "incident_id": mine.incident_id,
        "runbook_id": ROLLBACK_RUNBOOK_BY_MAIN_ID[origin.runbook_id.value],
        "idempotency_key": KEY,
    })
    assert resp.status_code == expected, resp.json()
    if suspended:
        assert _code(resp) == "TENANT_SUSPENDED"


def test_suspended_tenant_users_can_still_read(db, client_pg, login_as, two_tenants):
    _suspend(db, two_tenants["a"])
    login_as(UserRole.VIEWER, tenant_id=two_tenants["a"])
    assert client_pg.get(f"/api/v1/incidents/{two_tenants['mine'].incident_id}").status_code == 200
    ids = {item["incident_id"] for item in client_pg.get("/api/v1/incidents").json()["items"]}
    assert ids == {two_tenants["mine"].incident_id}

