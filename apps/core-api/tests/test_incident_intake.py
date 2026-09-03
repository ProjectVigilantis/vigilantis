"""Detection → Incident 생성 워크플로 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip).

계약 검증(schemas/intake.py)은 단위 테스트가 맡고, 여기서는 **저장·중복·트랜잭션**을
본다. 이 셋이 어긋나면 근거 없는 Incident나 한 자산에 쌓인 카드가 남는다.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import incident_intake  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.evidence import EvidenceType  # noqa: E402
from schemas.intake import FinOpsIncidentIntake, SecOpsIncidentIntake  # noqa: E402

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
EC2_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc123456789def0"
RUN_ID = "1f2e3d4c-5b6a-4978-8899-aabbccddee00"
COLLECTED_AT = "2026-09-02T09:00:00Z"
EVALUATED_AT = "2026-09-02T09:00:05Z"


def asset_payload(**over):
    base = {
        "arn": EC2_ARN,
        "resource_id": "i-0abc123456789def0",
        "asset_type": "EC2",
        "resource_role": "PRIMARY",
        "name": "batch-dev",
        "account_id": ACCOUNT,
        "region": REGION,
        "state": "running",
        "spec": {"instance_type": "t3.xlarge"},
        "relationships": [],
        "evaluation_status": "COMPLETED",
        "health_score": 4,
        "verdict": "COST_CANDIDATE",
        "skip_reason_code": None,
        "collected_at": COLLECTED_AT,
    }
    base.update(over)
    return base


def finops_intake(**over) -> FinOpsIncidentIntake:
    base = {
        "asset_snapshot": {"collection_run_id": RUN_ID, "asset": asset_payload()},
        "rule_evaluation": {
            "asset_arn": EC2_ARN,
            "collection_run_id": RUN_ID,
            "evaluation_status": "COMPLETED",
            "verdict": "COST_CANDIDATE",
            "health_score": 4,
            "skip_reason_code": None,
            "reason": "2일 평균 CPU 4.9% — 다운사이징 후보",
            "evaluated_at": EVALUATED_AT,
        },
    }
    base.update(over)
    return FinOpsIncidentIntake.model_validate(base)


def secops_intake(**over) -> SecOpsIncidentIntake:
    base = {
        "title": "SSH 브루트포스 시도",
        "threat_event": {
            "threat_event_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c60",
            "source_event_id": "evt-mock-001",
            "event_type": "SSH_BRUTE_FORCE",
            "target_arn": EC2_ARN,
            "occurred_at": "2026-09-02T09:00:00Z",
            "payload": {
                "source_ip": "203.0.113.10",
                "failed_attempt_count": 120,
                "window_seconds": 300,
            },
            "deduplication_key": "SSH_BRUTE_FORCE:i-0abc123456789def0:203.0.113.10",
            "collected_at": "2026-09-02T09:00:01Z",
        },
        "initial_risk": {
            "threat_event_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c60",
            "initial_risk_level": "HIGH",
            "response_mode": "PRE_MITIGATION_0_5S",
            "reason_codes": ["RISK_SSH_BRUTEFORCE"],
        },
    }
    base.update(over)
    return SecOpsIncidentIntake.model_validate(base)


# --- FINOPS --------------------------------------------------------------------


def test_finops_creates_incident_with_rule_and_asset_evidence(db):
    outcome = incident_intake.create_incident_from_intake(db, finops_intake())

    assert outcome.created is True
    incident = incidents_repo.get_incident(db, outcome.incident_id)
    assert incident.category is IncidentCategory.FINOPS
    assert incident.status is IncidentStatus.ANALYZING
    # FINOPS는 위험 대응 축이 전부 null이다(api/incidents.py 불변식)
    assert incident.initial_risk_level is None and incident.response_mode is None
    assert incident.initial_risk_reason_codes == []

    by_type = {e.evidence_type: e for e in incidents_repo.list_evidence(db, outcome.incident_id)}
    assert set(by_type) == {EvidenceType.RULE, EvidenceType.ASSET}
    # 근거로 저장한 값은 intake가 들고 온 객체 그 자체다 — 빌더가 이 행에서 읽는다
    assert by_type[EvidenceType.RULE].content["evaluation"]["collection_run_id"] == RUN_ID
    assert by_type[EvidenceType.ASSET].content["collection_run_id"] == RUN_ID
    assert by_type[EvidenceType.ASSET].content["asset"]["spec"]["instance_type"] == "t3.xlarge"


def _seed_asset_row(db, *, instance_type: str):
    """수집 회차 1개를 열고 자산 행을 그 회차 관측으로 upsert한다."""
    run = assets_repo.start_collection_run(
        db, account_id=ACCOUNT, region=REGION,
        mode="localstack", lookback_days=14, period_seconds=3600,
    )
    return assets_repo.upsert_asset(
        db, arn=EC2_ARN, asset_type=AssetType.EC2, resource_id="i-0abc123456789def0",
        account_id=ACCOUNT, region=REGION, spec={"instance_type": instance_type},
        collection_run_id=run.collection_run_id, collected_at=datetime.now(timezone.utc),
    )


def test_finops_asset_evidence_survives_asset_row_change(db):
    """자산 행이 다음 회차에 덮어써져도 근거는 판정 시점 스펙을 그대로 들고 있다.

    자산 행은 ARN당 1행이라 다음 회차 관측이 그 행을 덮어쓴다(upsert_asset). 그 뒤에
    근거를 다시 읽어, 자산 행과 근거가 서로 다른 스펙을 말하는지가 이 테스트다.
    """
    _seed_asset_row(db, instance_type="t3.xlarge")
    outcome = incident_intake.create_incident_from_intake(db, finops_intake())

    # 다음 회차 — 인스턴스가 이미 줄어든 채 관측된다
    _seed_asset_row(db, instance_type="t3.medium")
    assert assets_repo.get_asset_by_arn(db, EC2_ARN).spec["instance_type"] == "t3.medium"

    asset_evidence = next(
        e for e in incidents_repo.list_evidence(db, outcome.incident_id)
        if e.evidence_type is EvidenceType.ASSET
    )
    assert asset_evidence.content["asset"]["spec"]["instance_type"] == "t3.xlarge"
    assert asset_evidence.content["asset"]["collected_at"] == COLLECTED_AT


def test_finops_duplicate_returns_existing_without_creating(db):
    first = incident_intake.create_incident_from_intake(db, finops_intake())
    second = incident_intake.create_incident_from_intake(db, finops_intake())

    assert second.created is False
    assert second.incident_id == first.incident_id
    assert len(incidents_repo.list_incidents(db, category=IncidentCategory.FINOPS)) == 1
    # 근거도 두 벌이 되지 않는다
    assert len(incidents_repo.list_evidence(db, first.incident_id)) == 2


def test_finops_creates_again_after_resolved(db):
    """RESOLVED는 관제자가 닫은 건이다 — 같은 자산이 다시 판정되면 새 카드가 맞다."""
    first = incident_intake.create_incident_from_intake(db, finops_intake())
    incidents_repo.update_incident_status(
        db, first.incident_id,
        expected=IncidentStatus.ANALYZING, next_status=IncidentStatus.RESOLVED,
    )
    db.commit()

    second = incident_intake.create_incident_from_intake(db, finops_intake())
    assert second.created is True
    assert second.incident_id != first.incident_id


def test_finops_failed_incident_still_blocks_new_card(db):
    """FAILED는 미종료다 — 관제자가 닫기 전까지 같은 자산에 카드가 쌓이지 않는다.

    빼면 실패 원인이 그대로인 자산에 수집 주기마다 새 카드가 생긴다. 실패 카드는
    종료 처리로 닫을 수 있으므로(INCIDENT_RESOLVABLE_STATUSES) 사람이 닫아야 풀린다.
    """
    first = incident_intake.create_incident_from_intake(db, finops_intake())
    incidents_repo.update_incident_status(
        db, first.incident_id,
        expected=IncidentStatus.ANALYZING, next_status=IncidentStatus.FAILED,
    )
    db.commit()

    second = incident_intake.create_incident_from_intake(db, finops_intake())
    assert second.created is False
    assert second.incident_id == first.incident_id


# --- SECOPS --------------------------------------------------------------------


def test_secops_creates_threat_event_incident_and_evidence(db):
    intake = secops_intake()
    outcome = incident_intake.create_incident_from_intake(db, intake)

    assert outcome.created is True
    incident = incidents_repo.get_incident(db, outcome.incident_id)
    assert incident.category is IncidentCategory.SECOPS
    assert incident.title == "SSH 브루트포스 시도"
    assert incident.threat_event_id == intake.threat_event.threat_event_id
    assert incident.initial_risk_reason_codes == ["RISK_SSH_BRUTEFORCE"]

    evidences = incidents_repo.list_evidence(db, outcome.incident_id)
    assert [e.evidence_type for e in evidences] == [EvidenceType.THREAT]
    assert evidences[0].content["event"]["deduplication_key"] == (
        intake.threat_event.deduplication_key
    )
    assert (
        incidents_repo.get_threat_event_by_dedup_key(
            db, intake.threat_event.deduplication_key
        )
        is not None
    )


# --- 만든 것이 조회 계약을 통과하는가 -------------------------------------------


@pytest.mark.parametrize("make_intake", [finops_intake, secops_intake], ids=["finops", "secops"])
def test_created_incident_passes_public_response_contract(client_pg, db, make_intake):
    """이 계층이 쓴 상태를 읽는 쪽 계약(api/incidents.py `_enforce_contract`)에 대조한다.

    생성 직후의 조합(ANALYZING · 빈 요약 · 제안 없음 · 실행 없음)이 그 검증기를 통과하지
    못하면 상세 조회가 500이 된다 — 만드는 쪽에서는 드러나지 않는다.
    """
    outcome = incident_intake.create_incident_from_intake(db, make_intake())

    response = client_pg.get(f"/api/v1/incidents/{outcome.incident_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ANALYZING"
    assert body["summary_lines"] == []
    assert body["recommendations"] == []
    assert body["evidence_ids"]

    listed = client_pg.get("/api/v1/incidents").json()["items"]
    assert [item["incident_id"] for item in listed] == [outcome.incident_id]


def test_secops_orphan_threat_event_is_continued_from_stored_event(db):
    """이벤트만 남고 Incident가 없는 상태 — 저장된 이벤트를 기준으로 이어 만든다.

    새로 들어온 event의 식별자로 만들면 있지도 않은 행에 FK를 건다. 정상 경로에서는
    생기지 않는 상태지만, 이 분기가 있는 이상 실제로 동작해야 한다.
    """
    stored = secops_intake().threat_event
    incidents_repo.insert_threat_event(db, stored)
    db.commit()

    fresh_id = "2b3c4d5e-6f70-4812-9345-56789abcdef0"
    again = secops_intake(
        threat_event=dict(stored.model_dump(mode="json"), threat_event_id=fresh_id),
        initial_risk=dict(
            secops_intake().initial_risk.model_dump(mode="json"), threat_event_id=fresh_id
        ),
    )
    outcome = incident_intake.create_incident_from_intake(db, again)

    assert outcome.created is True
    incident = incidents_repo.get_incident(db, outcome.incident_id)
    assert incident.threat_event_id == stored.threat_event_id
    evidences = incidents_repo.list_evidence(db, outcome.incident_id)
    assert evidences[0].content["event"]["threat_event_id"] == stored.threat_event_id
    assert incidents_repo.get_threat_event_by_dedup_key(db, stored.deduplication_key) is not None


def test_secops_duplicate_key_returns_existing(db):
    first = incident_intake.create_incident_from_intake(db, secops_intake())
    # 같은 위협이 다시 들어온다 — 서버 발급 식별자는 다르지만 dedup 키가 같다
    again = secops_intake(threat_event=dict(
        secops_intake().threat_event.model_dump(mode="json"),
        threat_event_id="2b3c4d5e-6f70-4812-9345-56789abcdef0",
    ), initial_risk=dict(
        secops_intake().initial_risk.model_dump(mode="json"),
        threat_event_id="2b3c4d5e-6f70-4812-9345-56789abcdef0",
    ))
    second = incident_intake.create_incident_from_intake(db, again)

    assert second.created is False
    assert second.incident_id == first.incident_id
    assert len(incidents_repo.list_incidents(db, category=IncidentCategory.SECOPS)) == 1
