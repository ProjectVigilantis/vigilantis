# ==============================================================================
# [파일 설명]
# POST /api/v1/incidents/{id}/resolve 통합 검증(PostgreSQL) — 상태 전이·판단 저장·
# 잔여 제안 정리·멱등 재요청(200)·거절 2종(409)·404·422·WS 발행. (Issue #199)
#
#   - 종료 직후 상세·목록 재조회를 함께 본다. 잔여 제안을 정리하지 않으면 응답
#     계약(api/incidents.py)이 그 자리에서 500을 내므로, 전이만 보는 검증으로는
#     회귀를 잡지 못한다.
#   - 실행은 스텁이라 AWS 호출이 없다. ACTION_IN_PROGRESS는 상태만 세워 확인한다.
# ==============================================================================

from __future__ import annotations

import uuid

import pytest

from schemas.api.incidents import (
    IncidentStatus,
    ResolutionJudgement,
)
from schemas.api.actions import ExecutionStatus
from schemas.candidates import CandidateStatus
from schemas.runbooks import RunbookId, TriggerSource

from db import models

SUBJECT_EC2 = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa"


@pytest.fixture()
def seeded_incident(db, make_incident, seed_summary_lines):
    """이 파일의 인시던트는 요약 3줄을 갖는다.

    종료 판단 응답이 인시던트 상세를 돌려주고, 상세 계약이 `summary_lines`를
    **상태별로** 따지기 때문이다(`packages/schemas/api/incidents.py` — ANALYZING이면
    빈 배열이어야 한다). 모양이 아니라 이 파일의 선택이라 인자 하나만 얹는다.
    """

    def _make(status: IncidentStatus = IncidentStatus.AWAITING_APPROVAL):
        return make_incident(db, status=status, summary_lines=seed_summary_lines)

    return _make


def _url(incident: models.Incident) -> str:
    return f"/api/v1/incidents/{incident.incident_id}/resolve"


def test_resolve_stores_judgement_and_invalidates_remaining_proposals(client_pg, db, seeded_incident, make_candidate):
    incident = seeded_incident()
    candidate = make_candidate(db, incident)

    response = client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "RESOLVED"
    assert body["resolution"] == "JUSTIFIED"
    assert body["resolved_at"] is not None
    # RESOLVED는 제안이 비어 있어야 한다(api/incidents.py) — 정리가 응답에도 보인다
    assert body["recommendations"] == []

    db.refresh(candidate)
    assert candidate.status is CandidateStatus.INVALIDATED


def test_detail_and_list_after_resolve_still_serve_200(client_pg, db, seeded_incident, make_candidate):
    """종료가 만든 상태를 읽는 쪽 계약 회귀 — 잔여 제안이 남으면 여기서 500이 된다."""
    incident = seeded_incident()
    make_candidate(db, incident)
    client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})

    detail = client_pg.get(f"/api/v1/incidents/{incident.incident_id}")
    assert detail.status_code == 200
    assert detail.json()["resolution"] == "JUSTIFIED"

    listing = client_pg.get("/api/v1/incidents", params={"status": "RESOLVED"})
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [item["incident_id"] for item in items] == [incident.incident_id]
    # 목록은 상세의 부분집합 10필드다 — 종료 판단은 상세에만 실린다
    assert "resolution" not in items[0]


def test_resolve_twice_keeps_the_first_resolved_at(client_pg, db, seeded_incident, make_candidate):
    """Idempotency Key 없이 재요청이 안전한 지점 — 조건부 UPDATE라 두 번째 요청은
    아무것도 바꾸지 않고, 종료 시각도 처음 찍힌 값이 남는다."""
    incident = seeded_incident()
    first = client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})
    resolved_at = first.json()["resolved_at"]

    again = client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})

    assert again.status_code == 200
    assert again.json()["resolution"] == "JUSTIFIED"
    assert again.json()["resolved_at"] == resolved_at


def test_resolve_allows_failed_incident(client_pg, db, seeded_incident, make_candidate):
    """흐름이 멈춘 건에도 정당성 판단은 남겨야 한다."""
    incident = seeded_incident(IncidentStatus.FAILED)

    response = client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})

    assert response.status_code == 200
    assert response.json()["status"] == "RESOLVED"


@pytest.mark.parametrize(
    "status", [IncidentStatus.ACTION_IN_PROGRESS, IncidentStatus.ANALYZING]
)
def test_resolve_rejects_statuses_that_would_break_the_contract(
    client_pg, db, seeded_incident, status
):
    """ACTION_IN_PROGRESS는 RESOLVED에 진행 중 실행이 없어야 한다는 계약 때문에,
    ANALYZING은 분석이 끝나며 제안이 붙어 종료가 뒤집히기 때문에 거절한다."""
    incident = seeded_incident(status)

    response = client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "INCIDENT_NOT_RESOLVABLE"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]

    db.refresh(incident)
    assert incident.status is status
    assert incident.resolution is None


@pytest.mark.parametrize("path_id", [str(uuid.uuid4()), "not-a-uuid"])
def test_resolve_unknown_or_malformed_id_returns_404(client_pg, path_id):
    response = client_pg.post(
        f"/api/v1/incidents/{path_id}/resolve", json={"resolution": "JUSTIFIED"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "INCIDENT_NOT_FOUND"


@pytest.mark.parametrize(
    "payload", [{"resolution": "MAYBE"}, {}, {"resolution": "JUSTIFIED", "note": "x"}]
)
def test_resolve_rejects_payloads_outside_the_contract(client_pg, db, seeded_incident, payload):
    incident = seeded_incident()

    response = client_pg.post(_url(incident), json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_resolve_publishes_incident_updated_once(client_pg, db, seeded_incident, monkeypatch):
    """서버가 INCIDENT_UPDATED를 실제로 내보내는 첫 지점이다(#73 발행 장치 이후).
    재요청은 상태가 그대로라 발행하지 않는다."""
    incident = seeded_incident()
    published: list = []
    monkeypatch.setattr(
        client_pg.app.state.realtime, "publish", published.append
    )

    client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})
    assert len(published) == 1
    event = published[0]
    assert event.event_type.value == "INCIDENT_UPDATED"
    assert event.data.incident_id == incident.incident_id

    client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})
    assert len(published) == 1


def test_resolve_rejected_status_publishes_nothing(client_pg, db, seeded_incident, monkeypatch):
    incident = seeded_incident(IncidentStatus.ACTION_IN_PROGRESS)
    published: list = []
    monkeypatch.setattr(
        client_pg.app.state.realtime, "publish", published.append
    )

    client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})

    assert published == []


def test_resolution_judgement_values_match_db_enum(client_pg, db, seeded_incident, make_candidate):
    """계약 Enum 값 전수가 DB 타입에 그대로 저장된다 — migration의 값 목록 회귀."""
    for judgement in ResolutionJudgement:
        incident = seeded_incident()
        response = client_pg.post(_url(incident), json={"resolution": judgement.value})
        assert response.status_code == 200
        db.refresh(incident)
        assert incident.resolution is judgement


def _add_execution(
    db, incident: models.Incident, runbook_id: RunbookId
) -> models.ActionExecution:
    execution = models.ActionExecution(
        incident_id=incident.incident_id,
        runbook_id=runbook_id,
        target_arn=SUBJECT_EC2,
        status=ExecutionStatus.SUCCESS,
        trigger_source=TriggerSource.USER_APPROVAL,
    )
    db.add(execution)
    db.flush()
    return execution


def test_recovery_after_resolve_resumes_and_clears_the_judgement(client_pg, db, seeded_incident, make_candidate):
    """종료한 뒤 관제자 복구를 접수하는 정규 경로(ADR-0004) — 판단은 초기화된다.

    resolution은 "지금 이 인시던트가 종료된 이유"라 재개되면 거짓이 되고, 남겨
    두면 DB 제약(resolution_with_resolved_status)이 전이 자체를 막는다. 종료했다
    재개한 이력은 복구 실행 레코드가 남긴다.
    """
    incident = seeded_incident()
    origin = _add_execution(db, incident, RunbookId.RUNBOOK_EC2_ISOLATE)
    detail_url = f"/api/v1/incidents/{incident.incident_id}"

    resolved = client_pg.post(_url(incident), json={"resolution": "JUSTIFIED"})
    assert (resolved.status_code, resolved.json()["resolution"]) == (200, "JUSTIFIED")

    recovery = client_pg.post(
        "/api/v1/actions/execute",
        json={
            "incident_id": incident.incident_id,
            "runbook_id": RunbookId.RUNBOOK_EC2_UNISOLATE.value,
            "idempotency_key": uuid.uuid4().hex,
        },
    )

    assert recovery.status_code == 202
    detail = client_pg.get(detail_url)
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "ACTION_IN_PROGRESS"
    assert (body["resolution"], body["resolved_at"]) == (None, None)
    assert origin.execution_id in {e["execution_id"] for e in body["executions"]}
