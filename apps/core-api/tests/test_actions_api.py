# ==============================================================================
# [파일 설명]
# POST /api/v1/actions/execute 통합 검증(PostgreSQL) — 요청 검증·404·409 2종·
# 멱등 재요청(200)·신규 예약(202)·후보 CLAIMED 전이·동시 요청 복구. (Issue #116)
#
#   - 실행은 스텁이라 AWS 호출은 없다. 검증 대상은 예약 레코드와 상태 전이다.
#   - 동시 요청은 결정적 재현(첫 멱등 조회만 경합 창처럼 비움) 2건 + 독립 세션
#     2개의 실제 경합(같은 Incident 잠금·다른 Incident의 유니크 충돌)으로 검증한다.
#     실경합 테스트는 rollback 픽스처 밖이라
#     commit한 데이터를 직접 정리한다.
# ==============================================================================

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
from time import monotonic
import uuid

import pytest

from schemas.api.actions import ExecuteActionRequest, ExecutionStatus
from schemas.api.errors import ErrorCode
from schemas.api.incidents import (
    IncidentStatus,
)
from schemas.candidates import CandidateStatus
from schemas.runbooks import RunbookId, TriggerSource

import workflows
from db import models
from db.repositories import executions as executions_repo
from db.repositories import incidents as incidents_repo
from exceptions import ApiError

URL = "/api/v1/actions/execute"
KEY = "6dbfe076-1da1-4d35-88f8-b869dce44e61"
SUBJECT_EC2 = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa"
DEFAULT_RUNBOOK = RunbookId.RUNBOOK_NACL_ADD_DENY

# Runbook별 typed 파라미터(#154) — 접수가 저장 후보를 계약으로 재검증하므로
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
    # 상세 조회도 같은 값에 같은 코드로 답한다 — FE가 한 갈래로 분기한다
    detail = client_pg.get(f"/api/v1/incidents/{incident_id}")
    assert detail.status_code == 404
    assert detail.json()["error"]["code"] == "INCIDENT_NOT_FOUND"


@pytest.mark.parametrize("status", [
    CandidateStatus.PENDING_VALIDATION,   # 가드레일 판정 전
    CandidateStatus.REJECTED,             # 가드레일 거절
    CandidateStatus.CLAIMED,              # 이미 실행에 선점됨
    CandidateStatus.INVALIDATED,          # 실행 전 재확인에서 무효화됨
])
def test_candidate_not_executable_returns_409(client_pg, db, make_incident, make_candidate, status):
    incident = make_incident(db)
    make_candidate(db, incident, status=status)

    response = client_pg.post(URL, json=_body(incident, DEFAULT_RUNBOOK))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROPOSAL_NOT_EXECUTABLE"


def test_other_runbook_executable_returns_409(client_pg, db, make_executable):
    """EXECUTABLE 후보가 있어도 요청한 Runbook과 다르면 실행 대상이 아니다."""
    incident, _ = make_executable(db)

    response = client_pg.post(
        URL, json=_body(incident, RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROPOSAL_NOT_EXECUTABLE"


def test_contract_invalid_candidate_returns_409(client_pg, db, make_incident, make_precontract_candidate):
    """typed 계약(#154)을 거치지 않은 저장 후보는 EXECUTABLE이어도 실행되지 않는다.

    계약 이전에 저장된 행·마이그레이션 backfill(빈 parameters)이 이 부류다.
    접수만 막고 상세 노출은 그대로 둔다 — 응답 계약이 AWAITING_APPROVAL에 제안
    1개 이상을 요구해, 노출을 거르면 이 인시던트의 상세가 500이 된다
    (workflows._candidate_meets_contract 참조).
    """
    incident = make_incident(db)
    make_precontract_candidate(db, incident)  # NACL_ADD_DENY 필수 키 누락

    response = client_pg.post(URL, json=_body(incident, DEFAULT_RUNBOOK))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROPOSAL_NOT_EXECUTABLE"
    # 실행 예약이 남지 않는다
    assert executions_repo.get_by_idempotency_key(db, KEY) is None

    # 상세는 계속 응답한다 — 후보는 보이되 실행만 막힌 상태다
    detail = client_pg.get(f"/api/v1/incidents/{incident.incident_id}")
    assert detail.status_code == 200
    assert [r["runbook_id"] for r in detail.json()["recommendations"]] == [
        DEFAULT_RUNBOOK.value
    ]


# --- 예약 · 멱등 ---------------------------------------------------------------


def test_new_key_reserves_execution_and_claims_candidate(client_pg, db, make_executable):
    incident, candidate = make_executable(db)
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

    # 상태 전이 UPDATE의 onupdate가 부모 updated_at을 함께 올린다
    refreshed = incidents_repo.get_incident(db, incident.incident_id)
    assert refreshed.updated_at > seeded_updated_at


def test_detail_stays_readable_after_reservation(client_pg, db, make_executable):
    """접수와 함께 Incident가 ACTION_IN_PROGRESS로 옮겨가지 않으면, 유일한 후보가
    CLAIMED로 빠지고 IN_PROGRESS 실행이 생겨 상세 응답 계약이 깨진다(500)."""
    incident, candidate = make_executable(db)
    detail = f"/api/v1/incidents/{incident.incident_id}"

    assert client_pg.get(detail).status_code == 200
    assert client_pg.post(URL, json=_body(incident, candidate.runbook_id)).status_code == 202

    after = client_pg.get(detail)
    assert after.status_code == 200, after.json()
    body = after.json()
    assert body["status"] == "ACTION_IN_PROGRESS"
    assert body["recommendations"] == []
    assert [item["status"] for item in body["executions"]] == ["IN_PROGRESS"]


def test_second_reservation_keeps_action_in_progress(client_pg, db, make_candidate, make_executable):
    """이미 ACTION_IN_PROGRESS인 Incident의 두 번째 접수 — 상태 전이 rowcount 0은
    정상 경로이며, 상세는 계속 200이어야 한다."""
    incident, first = make_executable(db)
    second = make_candidate(db, incident, runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED)
    detail = f"/api/v1/incidents/{incident.incident_id}"

    assert client_pg.post(URL, json=_body(incident, first.runbook_id)).status_code == 202
    db.expire_all()
    after_first = incidents_repo.get_incident(db, incident.incident_id).updated_at

    assert client_pg.post(
        URL, json=_body(incident, second.runbook_id, key=KEY + "-2")
    ).status_code == 202

    after = client_pg.get(detail)
    assert after.status_code == 200, after.json()
    assert after.json()["status"] == "ACTION_IN_PROGRESS"
    assert len(after.json()["executions"]) == 2
    # 상태는 그대로여도(moved=False) touch_incident가 updated_at을 올려야 한다
    db.expire_all()
    assert incidents_repo.get_incident(db, incident.incident_id).updated_at > after_first


def test_same_key_replay_returns_200_with_same_execution(client_pg, db, make_executable):
    """재요청 시점의 후보는 이미 CLAIMED다 — 멱등 조회가 앞서야 200이 나온다."""
    incident, candidate = make_executable(db)
    payload = _body(incident, candidate.runbook_id)

    first = client_pg.post(URL, json=payload)
    assert first.status_code == 202

    second = client_pg.post(URL, json=payload)
    assert second.status_code == 200
    assert second.json() == first.json()

    db.expire_all()
    assert len(executions_repo.list_by_incident(db, incident.incident_id)) == 1


def test_same_key_pointing_elsewhere_returns_409_conflict(client_pg, db, make_candidate, make_executable):
    incident, candidate = make_executable(db)
    make_candidate(db, incident, runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED)
    other_incident, other_candidate = make_executable(db)

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


def test_replay_accepts_equivalent_uuid_text_forms(client_pg, db, make_executable):
    """저장 값은 정규형(소문자·하이픈)이다 — 대문자·하이픈 없는 표기의 동일
    재요청이 IDEMPOTENCY_KEY_CONFLICT로 오판되면 안 된다."""
    incident, candidate = make_executable(db)

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


def test_claimed_race_window_replays_existing_execution(
    client_pg, db, make_incident, make_candidate, make_execution, monkeypatch
):
    """최초 멱등 조회가 앞선 요청의 commit 전에 실행되고 후보 확인이 commit 후에
    실행된 경합 창 — 후보는 이미 CLAIMED지만 같은 Key 재요청이므로 409
    PROPOSAL_NOT_EXECUTABLE이 아니라 200 재생이어야 한다."""
    incident = make_incident(db)
    candidate = make_candidate(db, incident, status=CandidateStatus.CLAIMED)
    winner = make_execution(
        db,
        incident,
        runbook_id=candidate.runbook_id,
        target_arn=candidate.target_arn,
        candidate=candidate,
        idempotency_key=KEY,
    )

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


def test_duplicate_key_race_recovers_to_existing_execution(
    client_pg, db, make_executable, make_execution, monkeypatch
):
    """앞선 요청이 이미 예약한 상태에서 뒤엣 요청이 INSERT까지 간 경우.

    유니크 제약이 거절하고, 그 오류를 재조회로 받아 200으로 돌린다 —
    db/repositories/executions.py 헤더가 규정한 해석이다.
    """
    incident, candidate = make_executable(db)
    winner = make_execution(
        db,
        incident,
        runbook_id=candidate.runbook_id,
        target_arn=candidate.target_arn,
        idempotency_key=KEY,
    )

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


@pytest.mark.parametrize("same_incident", [True, False], ids=["same-incident", "different-incidents"])
def test_concurrent_same_key_requests_reserve_exactly_once(
    pg_engine, make_incident, make_candidate, same_incident,
):
    """실제 PG 대기를 관찰한다. 같은 사건이면 재생, 다른 사건이면 키 충돌이다."""
    from sqlalchemy import delete, event, text
    from sqlalchemy.orm import Session

    race_key = "race-" + uuid.uuid4().hex  # 다른 테스트와 키를 공유하지 않는다
    with Session(pg_engine) as setup:
        seeded = []
        for _ in range(1 if same_incident else 2):
            incident = make_incident(setup)
            candidate = make_candidate(setup, incident)
            seeded.append((incident.incident_id, candidate.candidate_id, incident.status))
        setup.commit()
    requests = [ExecuteActionRequest(
        incident_id=row[0], runbook_id=DEFAULT_RUNBOOK, idempotency_key=race_key,
    ) for row in (seeded[0], seeded[-1])]
    pending_commit = threading.Event()
    release_commit = threading.Event()

    def hold_first_commit(session):
        if not session.in_nested_transaction():
            pending_commit.set()
            assert release_commit.wait(timeout=10), "첫 예약의 commit 해제 신호가 오지 않음"

    def reserve(session, request):
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        try:
            return workflows.reserve_execution(session, request)
        except ApiError as exc:
            return exc.code

    try:
        # 연결 생성 지연을 경합 시간으로 세지 않는다. 각 연결은 작업 스레드 하나만 쓴다.
        with pg_engine.connect() as first_conn, pg_engine.connect() as second_conn, pg_engine.connect() as observer:
            first_pid = first_conn.scalar(text("SELECT pg_backend_pid()"))
            second_pid = second_conn.scalar(text("SELECT pg_backend_pid()"))
            first_conn.rollback()
            second_conn.rollback()
            with Session(first_conn) as first, Session(second_conn) as second, ThreadPoolExecutor(max_workers=2) as pool:
                event.listen(first, "before_commit", hold_first_commit)
                first_future = pool.submit(reserve, first, requests[0])
                try:
                    assert pending_commit.wait(timeout=5), "첫 예약이 commit 직전까지 도달하지 않음"
                    second_future = pool.submit(reserve, second, requests[1])
                    deadline = monotonic() + 3
                    while True:
                        blockers = observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid})
                        if first_pid in blockers:
                            break
                        assert not second_future.done(), f"대기 없이 요청 종료: {second_future.result()}"
                        assert monotonic() < deadline, "두 번째 요청의 PostgreSQL 잠금 대기가 관찰되지 않음"
                finally:
                    release_commit.set()
                created = first_future.result(timeout=5)
                raced = second_future.result(timeout=5)
                assert created.created is True
                if same_incident:
                    assert isinstance(raced, workflows.ExecutionReservation)
                    assert raced.created is False
                    assert raced.response.execution_id == created.response.execution_id
                else:
                    # 부모 행이 다르므로 Incident 잠금은 통과한다. 미커밋 실행 INSERT의
                    # 유니크 충돌을 SAVEPOINT로 복구한 뒤 다른 사건의 같은 Key를 거절한다.
                    assert raced is ErrorCode.IDEMPOTENCY_KEY_CONFLICT

        with Session(pg_engine) as verify:
            stored = executions_repo.list_by_incident(verify, seeded[0][0])
            assert len(stored) == 1
            assert stored[0].execution_id == created.response.execution_id
            assert incidents_repo.get_candidate(verify, seeded[0][1]).status is CandidateStatus.CLAIMED
            if not same_incident:
                incident_id, candidate_id, original_status = seeded[1]
                assert executions_repo.list_by_incident(verify, incident_id) == []
                assert incidents_repo.get_candidate(verify, candidate_id).status is CandidateStatus.EXECUTABLE
                assert incidents_repo.get_incident(verify, incident_id).status is original_status
    finally:
        with Session(pg_engine) as cleanup:
            incident_ids = [row[0] for row in seeded]
            cleanup.execute(
                delete(models.ActionExecution).where(
                    models.ActionExecution.incident_id.in_(incident_ids)
                )
            )
            cleanup.execute(
                delete(models.RunbookCandidate).where(
                    models.RunbookCandidate.incident_id.in_(incident_ids)
                )
            )
            cleanup.execute(
                delete(models.Incident).where(models.Incident.incident_id.in_(incident_ids))
            )
            cleanup.commit()


# --- 롤백 3종 접수 (Issue #126) --------------------------------------------------

ROLLBACK_PAIRS = [
    (RunbookId.RUNBOOK_EC2_ISOLATE, RunbookId.RUNBOOK_EC2_UNISOLATE),
    (RunbookId.RUNBOOK_SG_DELETE_ISOLATED, RunbookId.RUNBOOK_SG_RECREATE),
    (RunbookId.RUNBOOK_EC2_RIGHTSIZING, RunbookId.RUNBOOK_EC2_REVERT_SIZE),
]


@pytest.mark.parametrize("origin_runbook, rollback_runbook", ROLLBACK_PAIRS)
def test_rollback_reserves_child_bound_to_origin(
    client_pg, db, make_incident, make_execution, origin_runbook, rollback_runbook
):
    """롤백은 후보가 아니라 원본 실행에서 접수된다 — 결속은 parent_execution_id."""
    incident = make_incident(db)
    origin = make_execution(
        db, incident, runbook_id=origin_runbook, status=ExecutionStatus.SUCCESS
    )

    response = client_pg.post(URL, json=_body(incident, rollback_runbook))

    assert response.status_code == 202
    child = executions_repo.get_execution(db, response.json()["execution_id"])
    assert child.parent_execution_id == origin.execution_id
    assert child.candidate_id is None
    assert child.runbook_id is rollback_runbook
    assert child.trigger_source is TriggerSource.USER_APPROVAL
    assert child.status is ExecutionStatus.IN_PROGRESS


@pytest.mark.parametrize(
    "origin_status",
    [ExecutionStatus.IN_PROGRESS, ExecutionStatus.FAILED, ExecutionStatus.ROLLED_BACK],
)
def test_rollback_without_recoverable_origin_returns_409(
    client_pg, db, make_incident, make_execution, origin_status
):
    """복구를 열어 주지 않는 상태의 원본은 접수 근거가 되지 않는다."""
    incident = make_incident(db)
    make_execution(
        db, incident, runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE, status=origin_status
    )

    response = client_pg.post(
        URL, json=_body(incident, RunbookId.RUNBOOK_EC2_UNISOLATE)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROPOSAL_NOT_EXECUTABLE"


def test_rollback_without_matching_pair_returns_409(client_pg, db, make_incident, make_execution):
    """짝이 아닌 원본은 복구를 열지 않는다 — NACL_ADD_DENY의 해제는 본편 경로다."""
    incident = make_incident(db)
    make_execution(
        db,
        incident,
        runbook_id=RunbookId.RUNBOOK_NACL_ADD_DENY,
        status=ExecutionStatus.SUCCESS,
    )

    response = client_pg.post(
        URL, json=_body(incident, RunbookId.RUNBOOK_EC2_UNISOLATE)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROPOSAL_NOT_EXECUTABLE"


def test_second_rollback_on_same_origin_returns_409(client_pg, db, make_incident, make_execution):
    """이중 롤백 방지 — 한 원본이 여는 복구는 1회뿐이다."""
    incident = make_incident(db)
    make_execution(
        db,
        incident,
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        status=ExecutionStatus.SUCCESS,
    )
    body = _body(incident, RunbookId.RUNBOOK_EC2_UNISOLATE)

    first = client_pg.post(URL, json=body)
    second = client_pg.post(URL, json={**body, "idempotency_key": str(uuid.uuid4())})

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "PROPOSAL_NOT_EXECUTABLE"


def test_rollback_same_key_replay_returns_200(client_pg, db, make_incident, make_execution):
    """멱등 처리는 #116 경로를 그대로 쓴다 — 롤백도 같은 Key면 200 + 같은 실행."""
    incident = make_incident(db)
    make_execution(
        db,
        incident,
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        status=ExecutionStatus.SUCCESS,
    )
    body = _body(incident, RunbookId.RUNBOOK_EC2_UNISOLATE)

    first = client_pg.post(URL, json=body)
    replay = client_pg.post(URL, json=body)

    assert (first.status_code, replay.status_code) == (202, 200)
    assert replay.json()["execution_id"] == first.json()["execution_id"]


def test_rollback_on_resolved_incident_resumes_action_in_progress(client_pg, db, make_incident, make_execution):
    """종료 상태에서도 관제자 복구는 접수되고, 그 뒤 상세 조회가 200으로 남는다.

    RESOLVED는 "더 진행할 제안·실행 없음"이지 자산이 원복됐다는 뜻이 아니다 —
    격리된 채 RESOLVED인 인시던트의 [원클릭 해제]가 ADR-0004의 정규 경로다.
    """
    incident = make_incident(db)
    incident.status = IncidentStatus.RESOLVED
    origin = make_execution(
        db,
        incident,
        runbook_id=RunbookId.RUNBOOK_EC2_ISOLATE,
        status=ExecutionStatus.SUCCESS,
    )
    detail_url = f"/api/v1/incidents/{incident.incident_id}"

    before = client_pg.get(detail_url)
    response = client_pg.post(
        URL, json=_body(incident, RunbookId.RUNBOOK_EC2_UNISOLATE)
    )
    after = client_pg.get(detail_url)

    assert before.status_code == 200
    assert before.json()["executions"][0]["available_recovery_runbook_ids"] == [
        "RUNBOOK_EC2_UNISOLATE"
    ]
    assert response.status_code == 202
    assert after.status_code == 200
    assert after.json()["status"] == IncidentStatus.ACTION_IN_PROGRESS.value
    # 복구가 접수된 원본은 더 이상 복구를 열지 않는다
    summaries = {e["execution_id"]: e for e in after.json()["executions"]}
    assert summaries[origin.execution_id]["available_recovery_runbook_ids"] == []
