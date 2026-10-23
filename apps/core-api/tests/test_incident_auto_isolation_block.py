# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# 인시던트 상세의 auto_isolation_block (Post-MVP T-6 후속 · #521) — 실제 PostgreSQL · 실제 라우터.
#
# 지키는 것.
#   ① 꺼진 테넌트의 「지금 걸릴」 자동 발동(Medium 승인 대기 · 분석 전 High)만 AUTO_ACTION_DISABLED 다.
#   ② 다시 켠 뒤에도 그 발동을 정한 시각이 켠 시각 이전이면 막혀 있다 — 접수 관문과 같은 판정이다.
#   ③ 자동 발동 대상이 아닌 건(Low · 분석 뒤 High · 이미 발동한 건 · FINOPS)과 테넌트 없음은 null 이다.
# ==============================================================================

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import workflows
from db import models
from schemas.api.incidents import AutoIsolationBlock, IncidentCategory, IncidentStatus, ResponseMode
from tests.test_auto_isolation_workflow import PRE, SKIP, TIMEOUT, _attempt
from tests.test_tenant_auto_action import _seeded, _tenant, _toggle


def _block(client, incident_id: str):
    resp = client.get(f"/api/v1/incidents/{incident_id}")
    assert resp.status_code == 200
    return resp.json()["auto_isolation_block"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ----- ① ② 막혀 있는 건 — 접수 관문과 같은 판정 -------------------------------------

@pytest.mark.parametrize("kind, trigger", [("medium", TIMEOUT), ("high", PRE)])
def test_disabled_tenant_shows_the_block_and_the_gate_agrees(
    db, client_pg, make_incident, make_candidate, kind, trigger
):
    seeded = _seeded(db, make_incident, make_candidate, kind, _tenant(db, enabled=False))
    assert _block(client_pg, seeded.incident_id) == "AUTO_ACTION_DISABLED"
    # 화면이 본 값과 서버가 실제로 접수하지 않는 이유가 같다
    assert _attempt(db, seeded, trigger).skipped_reason is SKIP.AUTO_ACTION_DISABLED


@pytest.mark.parametrize("kind", ["medium", "high"])
def test_re_enabled_after_the_decision_still_shows_the_block(
    db, client_pg, make_incident, make_candidate, kind
):
    tenant = _tenant(db, enabled=False)
    seeded = _seeded(db, make_incident, make_candidate, kind, tenant)  # 기한 · 접수 시각이 켜기 전
    _toggle(db, tenant, True)
    db.commit()
    assert _block(client_pg, seeded.incident_id) == "AUTO_ACTION_DISABLED"


@pytest.mark.parametrize("kind", ["medium", "high"])
def test_re_enabled_before_the_decision_shows_no_block(
    db, client_pg, make_incident, make_candidate, kind
):
    tenant = _tenant(db, enabled=False)
    _toggle(db, tenant, True, now=_utcnow() - timedelta(minutes=10))  # 대기 시작 · 접수보다 먼저 켰다
    seeded = _seeded(db, make_incident, make_candidate, kind, tenant)
    assert _block(client_pg, seeded.incident_id) is None


# ----- ③ 자동 발동 대상이 아닌 건 -------------------------------------------------

def test_low_wait_shows_null_even_when_disabled(db, client_pg, make_incident, make_candidate):
    seeded = _seeded(db, make_incident, make_candidate, "low", _tenant(db, enabled=False))
    assert _block(client_pg, seeded.incident_id) is None


def test_high_after_analysis_shows_null_even_when_disabled(db, client_pg, make_incident, make_candidate):
    # 분석이 끝나 제안이 붙은 High 는 선차단 대상이 아니다(발동 조건은 ANALYZING) — 승인 대기는 제안이 있어야 한다
    seeded = _seeded(db, make_incident, make_candidate, "high", _tenant(db, enabled=False),
                     status=IncidentStatus.AWAITING_APPROVAL)
    make_candidate(db, db.get(models.Incident, seeded.incident_id))
    db.commit()
    assert _block(client_pg, seeded.incident_id) is None


def test_already_timed_out_medium_shows_null(db, client_pg, make_incident, make_candidate):
    seeded = _seeded(db, make_incident, make_candidate, "medium", _tenant(db, enabled=False))
    db.get(models.Incident, seeded.incident_id).response_mode = ResponseMode.TIMEOUT_ISOLATION_1M
    db.commit()
    assert _block(client_pg, seeded.incident_id) is None


@pytest.mark.parametrize("assigned", [False, True], ids=["no-tenant", "enabled-tenant"])
def test_unassigned_or_enabled_tenant_shows_null(db, client_pg, make_incident, make_candidate, assigned):
    seeded = _seeded(db, make_incident, make_candidate, "medium", _tenant(db) if assigned else None)
    assert _block(client_pg, seeded.incident_id) is None


def test_finops_shows_null_even_for_a_disabled_tenant(db, client_pg, make_incident):
    tenant = _tenant(db, enabled=False)
    incident = make_incident(db, category=IncidentCategory.FINOPS, status=IncidentStatus.ANALYZING)
    incident.tenant_id = tenant.tenant_id
    db.commit()
    assert _block(client_pg, incident.incident_id) is None


# ----- 계약 값 = 관문 사유 ------------------------------------------------------

def test_every_contract_value_is_a_gate_reason():
    # 라우터는 관문 사유를 철자 그대로 계약 값으로 옮긴다 — 계약에 없는 사유가 생기면 상세가 500 이 된다
    assert {v.value for v in AutoIsolationBlock} <= {r.value for r in workflows.AutoIsolationSkipReason}
