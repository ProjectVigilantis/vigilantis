# ==============================================================================
# [파일 설명]
# POST /api/v1/actions/execute 통합 검증(PostgreSQL) — 요청 검증·404·409 2종·
# 멱등 재요청(200)·신규 예약(202)·후보 CLAIMED 전이·동시 요청 복구. (Issue #116)
#
#   - 실행은 스텁이라 AWS 호출은 없다. 검증 대상은 예약 레코드와 상태 전이다.
#   - 동시 요청은 결정적 재현(첫 멱등 조회만 경합 창처럼 비움) 2건 + 독립 세션
#     2개의 실제 경합 1건으로 검증한다. 실경합 테스트는 rollback 픽스처 밖이라
#     commit한 데이터를 직접 정리한다.
# ==============================================================================

from __future__ import annotations

import threading
import uuid

import pytest

from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.incidents import (
    IncidentCategory,
    IncidentStatus,
    ResponseMode,
    RiskLevel,
)
from schemas.candidates import CandidateStatus
from schemas.runbooks import RunbookId, TriggerSource

import workflows
from db import models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo

URL = "/api/v1/actions/execute"
KEY = "6dbfe076-1da1-4d35-88f8-b869dce44e61"
SUBJECT_EC2 = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa"
DEFAULT_RUNBOOK = RunbookId.RUNBOOK_NACL_ADD_DENY


def _seed_incident(db) -> models.Incident:
    incident = models.Incident(
        subject_arn=SUBJECT_EC2,
        category=IncidentCategory.SECOPS,
        status=IncidentStatus.AWAITING_APPROVAL,
        title="SSH 브루트포스 탐지",
        initial_risk_level=RiskLevel.MEDIUM,
        response_mode=ResponseMode.AGENT_WAIT,
        initial_risk_reason_codes=["SSH_BRUTE_FORCE"],
    )
    db.add(incident)
    db.flush()
    return incident


def _add_candidate(
    db,
    incident: models.Incident,
    runbook_id: RunbookId = DEFAULT_RUNBOOK,
    status: CandidateStatus = CandidateStatus.EXECUTABLE,
) -> models.RunbookCandidate:
    candidate = models.RunbookCandidate(
        incident_id=incident.incident_id,
        runbook_id=runbook_id,
        target_arn=SUBJECT_EC2,
        status=status,
    )
    db.add(candidate)
    db.flush()
    return candidate


def _seed_executable(db) -> tuple[models.Incident, models.RunbookCandidate]:
    incident = _seed_incident(db)
    return incident, _add_candidate(db, incident)


def _body(incident: models.Incident, runbook_id: RunbookId, key: str = KEY) -> dict:
    return {
        "incident_id": incident.incident_id,
        "runbook_id": runbook_id.value,
        "idempotency_key": key,
    }


# --- 요청 검증(DB 비의존) -------------------------------------------------------


@pytest.mark.parametrize("over", [
    {"target_arn": SUBJECT_EC2},          # 계약에 없는 필드
    {"idempotency_key": "k" * 129},       # 저장 컬럼 폭 초과
    {"runbook_id": "RUNBOOK_IP_BLOCK"},   # 폐기 ID
])
def test_contract_violation_returns_422_envelope(client, over):
    payload = {
        "incident_id": str(uuid.uuid4()),
        "runbook_id": DEFAULT_RUNBOOK.value,
        "idempotency_key": KEY,
    }
    payload.update(over)

    response = client.post(URL, json=payload)
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


# --- 404 · 409 -----------------------------------------------------------------


@pytest.mark.parametrize("incident_id", [
    str(uuid.uuid4()),   # 형식은 맞지만 없는 Incident
    "not-a-uuid",        # 계약은 통과하지만 저장 컬럼(uuid) 형식이 아님
])
def test_unknown_incident_returns_404_envelope(client_pg, incident_id):
    response = client_pg.post(
        URL,
        json={
            "incident_id": incident_id,
            "runbook_id": DEFAULT_RUNBOOK.value,
            "idempotency_key": KEY,
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "INCIDENT_NOT_FOUND"


@pytest.mark.parametrize("status", [
    CandidateStatus.PENDING_VALIDATION,   # 가드레일 판정 전
    CandidateStatus.REJECTED,             # 가드레일 거절
    CandidateStatus.CLAIMED,              # 이미 실행에 선점됨
    CandidateStatus.INVALIDATED,          # 실행 전 재확인에서 무효화됨
])
def test_candidate_not_executable_returns_409(client_pg, db, status):
    incident = _seed_incident(db)
    _add_candidate(db, incident, status=status)

    response = client_pg.post(URL, json=_body(incident, DEFAULT_RUNBOOK))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROPOSAL_NOT_EXECUTABLE"


def test_other_runbook_executable_returns_409(client_pg, db):
    """EXECUTABLE 후보가 있어도 요청한 Runbook과 다르면 실행 대상이 아니다."""
    incident, _ = _seed_executable(db)

    response = client_pg.post(
        URL, json=_body(incident, RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROPOSAL_NOT_EXECUTABLE"


# --- 예약 · 멱등 ---------------------------------------------------------------


def test_new_key_reserves_execution_and_claims_candidate(client_pg, db):
    incident, candidate = _seed_executable(db)
    seeded_updated_at = incident.updated_at

    response = client_pg.post(URL, json=_body(incident, candidate.runbook_id))
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"execution_id", "status", "updated_at"}
    assert body["status"] == "IN_PROGRESS"
    assert body["updated_at"].endswith("Z")

    db.expire_all()
    execution = executions_repo.get_execution(db, body["execution_id"])
    assert execution.status is ExecutionStatus.IN_PROGRESS
    assert execution.trigger_source is TriggerSource.USER_APPROVAL
    assert execution.idempotency_key == KEY
    # 대상 ARN·후보 결속은 요청이 아니라 저장된 제안에서 재구성한다
    assert execution.target_arn == candidate.target_arn
    assert execution.candidate_id == candidate.candidate_id
    assert execution.validated_command is None

    claimed = incidents_repo.get_candidate(db, candidate.candidate_id)
    assert claimed.status is CandidateStatus.CLAIMED

    # 상세 응답의 자식 상태가 바뀌었으므로 부모 updated_at도 올라간다(touch_incident)
    refreshed = incidents_repo.get_incident(db, incident.incident_id)
    assert refreshed.updated_at > seeded_updated_at


def test_same_key_replay_returns_200_with_same_execution(client_pg, db):
    """재요청 시점의 후보는 이미 CLAIMED다 — 멱등 조회가 앞서야 200이 나온다."""
    incident, candidate = _seed_executable(db)
    payload = _body(incident, candidate.runbook_id)

    first = client_pg.post(URL, json=payload)
    assert first.status_code == 202

    second = client_pg.post(URL, json=payload)
    assert second.status_code == 200
    assert second.json() == first.json()

    db.expire_all()
    assert len(executions_repo.list_by_incident(db, incident.incident_id)) == 1


def test_same_key_pointing_elsewhere_returns_409_conflict(client_pg, db):
    incident, candidate = _seed_executable(db)
    _add_candidate(db, incident, RunbookId.RUNBOOK_SG_DELETE_ISOLATED)
    other_incident, other_candidate = _seed_executable(db)

    first = client_pg.post(URL, json=_body(incident, candidate.runbook_id))
    assert first.status_code == 202

    # 같은 Key가 다른 Runbook을 가리킨다
    conflict = client_pg.post(
        URL, json=_body(incident, RunbookId.RUNBOOK_SG_DELETE_ISOLATED)
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    # 같은 Key가 다른 Incident를 가리킨다
    conflict = client_pg.post(URL, json=_body(other_incident, other_candidate.runbook_id))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_replay_accepts_equivalent_uuid_text_forms(client_pg, db):
    """저장 값은 정규형(소문자·하이픈)이다 — 대문자·하이픈 없는 표기의 동일
    재요청이 IDEMPOTENCY_KEY_CONFLICT로 오판되면 안 된다."""
    incident, candidate = _seed_executable(db)

    first = client_pg.post(URL, json=_body(incident, candidate.runbook_id))
    assert first.status_code == 202

    for variant in (
        incident.incident_id.upper(),
        incident.incident_id.replace("-", ""),
    ):
        replay = client_pg.post(
            URL,
            json={
                "incident_id": variant,
                "runbook_id": candidate.runbook_id.value,
                "idempotency_key": KEY,
            },
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()

    # uuid로 읽을 수 없는 값은 어떤 저장 값과도 같을 수 없다 — 404가 아니라 409
    mismatch = client_pg.post(
        URL,
        json={
            "incident_id": "not-a-uuid",
            "runbook_id": candidate.runbook_id.value,
            "idempotency_key": KEY,
        },
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_claimed_race_window_replays_existing_execution(client_pg, db, monkeypatch):
    """최초 멱등 조회가 앞선 요청의 commit 전에 실행되고 후보 확인이 commit 후에
    실행된 경합 창 — 후보는 이미 CLAIMED지만 같은 Key 재요청이므로 409
    PROPOSAL_NOT_EXECUTABLE이 아니라 200 재생이어야 한다."""
    incident = _seed_incident(db)
    candidate = _add_candidate(db, incident, status=CandidateStatus.CLAIMED)
    winner = models.ActionExecution(
        incident_id=incident.incident_id,
        runbook_id=candidate.runbook_id,
        target_arn=candidate.target_arn,
        trigger_source=TriggerSource.USER_APPROVAL,
        candidate_id=candidate.candidate_id,
        idempotency_key=KEY,
    )
    db.add(winner)
    db.flush()

    real_lookup = executions_repo.get_by_idempotency_key
    seen = {"calls": 0}

    def _blind_first(*args, **kwargs):
        # 첫 조회만 경합 창을 재현한다 — 앞선 요청의 예약이 아직 안 보이는 상태
        seen["calls"] += 1
        return None if seen["calls"] == 1 else real_lookup(*args, **kwargs)

    monkeypatch.setattr(
        workflows.executions_repo, "get_by_idempotency_key", _blind_first
    )

    response = client_pg.post(URL, json=_body(incident, candidate.runbook_id))
    assert response.status_code == 200
    assert response.json()["execution_id"] == winner.execution_id
    assert seen["calls"] == 2  # 최초 조회 + PROPOSAL_NOT_EXECUTABLE 확정 전 재확인

    db.expire_all()
    assert len(executions_repo.list_by_incident(db, incident.incident_id)) == 1


def test_duplicate_key_race_recovers_to_existing_execution(client_pg, db, monkeypatch):
    """앞선 요청이 이미 예약한 상태에서 뒤엣 요청이 INSERT까지 간 경우.

    유니크 제약이 거절하고, 그 오류를 재조회로 받아 200으로 돌린다 —
    db/repositories/executions.py 헤더가 규정한 해석이다.
    """
    incident, candidate = _seed_executable(db)
    winner = models.ActionExecution(
        incident_id=incident.incident_id,
        runbook_id=candidate.runbook_id,
        target_arn=candidate.target_arn,
        trigger_source=TriggerSource.USER_APPROVAL,
        idempotency_key=KEY,
    )
    db.add(winner)
    db.flush()

    real_lookup = executions_repo.get_by_idempotency_key
    seen = {"calls": 0}

    def _blind_first(*args, **kwargs):
        # 첫 조회만 경합 창을 재현한다 — 앞선 요청의 예약이 아직 안 보이는 상태
        seen["calls"] += 1
        return None if seen["calls"] == 1 else real_lookup(*args, **kwargs)

    monkeypatch.setattr(
        workflows.executions_repo, "get_by_idempotency_key", _blind_first
    )

    response = client_pg.post(URL, json=_body(incident, candidate.runbook_id))
    assert response.status_code == 200
    assert response.json()["execution_id"] == winner.execution_id
    assert seen["calls"] == 2  # 최초 조회 + 제약 위반 후 재조회

    db.expire_all()
    assert len(executions_repo.list_by_incident(db, incident.incident_id)) == 1
    # 패배한 요청은 후보를 선점하지 않는다
    assert (
        incidents_repo.get_candidate(db, candidate.candidate_id).status
        is CandidateStatus.EXECUTABLE
    )


# --- 실제 동시 경합(독립 세션) --------------------------------------------------


def test_concurrent_same_key_requests_reserve_exactly_once(pg_engine, monkeypatch):
    """독립 트랜잭션 2개가 같은 Key로 실제 경합한다 — 트랜잭션 간 유니크 충돌,
    선행 commit 대기, 충돌 후 재조회 가시성은 같은 세션 재현으로는 검증되지
    않는다. INSERT 직전 게이트로 두 트랜잭션이 모두 멱등 조회·후보 확인을
    통과한 뒤에야 INSERT를 시도하게 고정한다 — 한쪽이 먼저 commit을 끝내
    다른 쪽이 최초 멱등 조회에서 바로 재생해 버리는(충돌 경로를 건너뛰는)
    인터리빙을 배제한다. 결과는 신규 202 하나 + 재요청 200 하나여야 한다."""
    from sqlalchemy import delete
    from sqlalchemy.orm import Session

    race_key = "race-" + uuid.uuid4().hex  # 다른 테스트와 키를 공유하지 않는다

    setup = Session(bind=pg_engine)
    try:
        incident = _seed_incident(setup)
        candidate = _add_candidate(setup, incident)
        incident_id, candidate_id = incident.incident_id, candidate.candidate_id
        setup.commit()
    finally:
        setup.close()

    request = ExecuteActionRequest.model_validate(
        {
            "incident_id": incident_id,
            "runbook_id": DEFAULT_RUNBOOK.value,
            "idempotency_key": race_key,
        }
    )

    real_create = executions_repo.create_execution
    insert_gate = threading.Barrier(2, timeout=10)
    entered: list[int] = []

    def _gated_create(*args, **kwargs):
        # 두 트랜잭션이 모두 여기 도달할 때까지 대기 → 둘 다 INSERT를 시도한다.
        # 늦게 flush한 쪽은 상대의 미커밋 유니크 엔트리에 블로킹됐다가
        # 상대 commit 후 IntegrityError를 받는다 — 검증하려는 경로 그 자체다
        entered.append(threading.get_ident())
        insert_gate.wait()
        return real_create(*args, **kwargs)

    monkeypatch.setattr(workflows.executions_repo, "create_execution", _gated_create)

    results: list = [None, None]

    def _run(slot: int) -> None:
        session = Session(bind=pg_engine)
        try:
            results[slot] = workflows.reserve_execution(session, request)
        except Exception as exc:  # noqa: BLE001 — 실패도 수집해 assert가 보여 준다
            results[slot] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=_run, args=(slot,)) for slot in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads), "경합 요청이 끝나지 않음"
        assert all(
            isinstance(row, workflows.ExecutionReservation) for row in results
        ), f"예약 대신 예외가 나왔다: {results}"
        # 둘 다 INSERT까지 진입했다 — 재요청 200이 최초 멱등 조회가 아니라
        # 유니크 충돌 → 재조회 경로에서 나왔다는 뜻이다
        assert len(entered) == 2
        # 한쪽은 신규 예약(202 경로), 다른 쪽은 같은 Key 재요청(200 경로)
        assert sorted(row.created for row in results) == [False, True]
        assert results[0].response.execution_id == results[1].response.execution_id

        verify = Session(bind=pg_engine)
        try:
            stored = executions_repo.list_by_incident(verify, incident_id)
            assert len(stored) == 1
            assert stored[0].execution_id == results[0].response.execution_id
            assert (
                incidents_repo.get_candidate(verify, candidate_id).status
                is CandidateStatus.CLAIMED
            )
        finally:
            verify.close()
    finally:
        cleanup = Session(bind=pg_engine)
        try:
            cleanup.execute(
                delete(models.ActionExecution).where(
                    models.ActionExecution.incident_id == incident_id
                )
            )
            cleanup.execute(
                delete(models.RunbookCandidate).where(
                    models.RunbookCandidate.incident_id == incident_id
                )
            )
            cleanup.execute(
                delete(models.Incident).where(models.Incident.incident_id == incident_id)
            )
            cleanup.commit()
        finally:
            cleanup.close()
