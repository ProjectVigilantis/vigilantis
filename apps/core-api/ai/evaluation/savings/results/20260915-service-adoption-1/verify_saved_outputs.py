"""생성 내용은 보존하고 합성 식별자만 UUID로 대응해 저장·조회한다. 외부 호출은 없다."""

import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROUND = Path(__file__).resolve().parent
ROOT = ROUND.parents[6]
sys.path[:0] = [str(ROOT / p) for p in ("apps/core-api", "packages", "scripts")]
os.environ.update(DISPATCH_ENABLED="false", SCAN_ENABLED="false", MOCK_THREAT_INBOX_DIR="")

import agent_dispatcher
import workflows
from ai.openai_client import OpenAIModelClient
from alembic import command
from alembic.config import Config
from config import get_settings
from db import models
from db.repositories import assets as assets_repo
from db.repositories import incidents as incidents_repo
from db.session import get_db, get_engine, get_session_factory
from fastapi.testclient import TestClient
from finops_eval import _fixed_set, load_cases
from main import create_app
from schemas.agents import AgentGraphOutput, FinOpsGraphInput
from schemas.api.incidents import IncidentCategory, IncidentStatus
from schemas.evidence import EvidenceItem
from schemas.incidents import AgentInvocationStatus
from schemas.precheck import (
    PrecheckOutcome,
    VerificationMethod,
    build_verification_summary,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def database_identities(graph_input, output):
    """고정 세트의 비 UUID 식별자만 일대일 대응한다. 모델 생성 내용은 수정하지 않는다."""
    ids = {graph_input.incident_id, *(item.evidence_id for item in graph_input.evidences)}
    mapping = {value: str(uuid.uuid5(uuid.NAMESPACE_URL, "vigilantis:savings-replay:" + value))
               for value in ids}
    graph = graph_input.model_dump(mode="json")
    graph["incident_id"] = mapping[graph["incident_id"]]
    for item in graph["evidences"]:
        item["evidence_id"] = mapping[item["evidence_id"]]
    result = output.model_dump(mode="json")
    for candidate in result["candidates"]:
        candidate["evidence_ids"] = [mapping[item] for item in candidate["evidence_ids"]]
    mapped = AgentGraphOutput.model_validate(result)
    restored = mapped.model_dump(mode="json")
    inverse = {value: key for key, value in mapping.items()}
    for candidate in restored["candidates"]:
        candidate["evidence_ids"] = [inverse[item] for item in candidate["evidence_ids"]]
    assert restored == output.model_dump(mode="json")
    return FinOpsGraphInput.model_validate(graph), mapped, mapping


def seed(session, graph_input):
    """UUID를 대응한 자산·근거를 시드한다. Intake 실행 검증은 아니다."""
    asset = graph_input.asset_context
    run = assets_repo.start_collection_run(
        session, account_id=asset.account_id, region=asset.region, mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    assets_repo.upsert_asset(
        session, arn=asset.arn, asset_type=asset.asset_type, resource_id=asset.resource_id,
        account_id=asset.account_id, region=asset.region, spec=asset.spec.model_dump(mode="json"),
        collection_run_id=run.collection_run_id, collected_at=asset.collected_at,
        name=asset.name, state=asset.state,
    )
    session.add(models.Incident(
        incident_id=graph_input.incident_id, subject_arn=asset.arn,
        category=IncidentCategory.FINOPS, status=IncidentStatus.ANALYZING,
        agent_invocation_status=AgentInvocationStatus.IN_PROGRESS,
        agent_invocation_started_at=datetime.now(UTC),
    ))
    session.flush()
    for item in graph_input.evidences:
        incidents_repo.add_evidence(session, EvidenceItem(
            evidence_id=item.evidence_id, incident_id=graph_input.incident_id,
            evidence_type=item.evidence_type, source_type="golden_validation",
            source_id=item.evidence_id, content=item.content,
            occurred_at=asset.collected_at, collected_at=asset.collected_at,
        ))
    session.commit()


def precheck(_command, backup_loader=None):
    return PrecheckOutcome(
        passed=True,
        verification_summary=build_verification_summary(
            VerificationMethod.DRY_RUN, verified=["테스트 대역 응답"],
            unverified=["실제 AWS 대상 상태·IAM 권한"],
        ),
    )


def verify(engine, cases, raw):
    checked = []
    responses = {}
    replay_ids = {}
    with patch.object(workflows, "_candidate_precheck", precheck), patch.object(
        OpenAIModelClient, "complete", side_effect=AssertionError("UNEXPECTED_MODEL_CALL"),
    ) as model_calls:
        for case in cases:
            run = next(r for r in raw["runs"] if r["case_id"] == case.case_id and r["repeat"] == 1)
            output = AgentGraphOutput.model_validate({
                key: run[key] for key in AgentGraphOutput.model_fields
            })
            verified = agent_dispatcher._verified_output(
                case.graph_input, output, case.graph_input.incident_id,
            )
            assert verified == output, "저장 경계에서 실제 생성 결과가 바뀌었다"
            assert output.invocation_status is AgentInvocationStatus.SUCCEEDED
            graph, mapped, identities = database_identities(case.graph_input, output)
            assert agent_dispatcher._verified_output(graph, mapped, graph.incident_id) == mapped
            replay_ids[case.case_id] = graph.incident_id
            with Session(engine, expire_on_commit=False) as session:
                seed(session, graph)
                outcome = workflows.record_agent_analysis(
                    session, graph.incident_id, mapped,
                )
                saved = incidents_repo.list_candidates(session, graph.incident_id)
                assert len(saved) == len(output.candidates) == 1
                assert saved[0].ai_savings_estimate == output.candidates[0].ai_savings_estimate.model_dump(mode="json")
                assert saved[0].evidence_ids == list(mapped.candidates[0].evidence_ids)
            checked.append({
                "case_id": case.case_id, "repeat": 1,
                "incident_id": graph.incident_id,
                "source_output_accepted_without_change": True,
                "database_identity_mapping": identities,
                "summary_action_parameters_and_savings_unchanged": True,
                "amount": saved[0].ai_savings_estimate["amount"],
                "workflow_outcome": outcome.next_status.value,
            })

        def fresh_db():
            with Session(engine) as session:
                yield session

        # 각 앱이 새 요청 세션으로 이미 commit된 PostgreSQL 행을 읽는다.
        for app_index in range(2):
            app = create_app()
            app.dependency_overrides[get_db] = fresh_db
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200
                for case in cases:
                    response = client.get(f"/api/v1/incidents/{replay_ids[case.case_id]}")
                    assert response.status_code == 200
                    body = response.json()
                    assert body["status"] == "AWAITING_APPROVAL"
                    run = next(r for r in raw["runs"] if r["case_id"] == case.case_id and r["repeat"] == 1)
                    assert body["summary_lines"] == run["summary_lines"]
                    assert body["recommendations"][0]["ai_savings_estimate"] == run["candidates"][0]["ai_savings_estimate"]
                    if app_index:
                        assert body == responses[case.case_id]
                    responses[case.case_id] = body
        assert model_calls.call_count == 0
    return checked, responses


def main():
    raw_path = ROUND / "candidate-raw.json"
    raw_before = digest(raw_path)
    raw = json.loads(raw_path.read_text("utf-8"))
    metadata_path = ROUND / "metadata.json"
    metadata_before = digest(metadata_path)
    cases = load_cases()
    assert raw["fixed_set"] == _fixed_set(cases)
    assert len(raw["runs"]) == 60
    # 모델 단계가 종료된 뒤 실행하여 호출 기록의 불변성을 함께 확인한다.
    metadata = json.loads(metadata_path.read_text("utf-8"))
    assert all(metadata["stages"].get(stage, {}).get("status") == "COMPLETED"
               for stage in ("baseline", "candidate"))
    assert metadata["stages"]["judge"]["status"] in {"COMPLETED", "SKIPPED_PRICE_GATE"}
    admin_url = make_url(os.environ.get(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql+psycopg://vigilantis:vigilantis@localhost:5432/postgres",
    ))
    assert admin_url.host in {"localhost", "127.0.0.1"}, "로컬 검증 DB만 사용한다"
    database_name = "vigilantis_savings_verify_" + uuid.uuid4().hex[:12]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    engine = None
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        created = True
        test_url = admin_url.set(database=database_name)
        os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
        get_settings.cache_clear()
        command.upgrade(Config(str(ROOT / "apps/core-api/alembic.ini")), "head")
        engine = create_engine(test_url)
        checked, responses = verify(engine, cases, raw)
        assert digest(raw_path) == raw_before
        assert digest(metadata_path) == metadata_before
        report = {
            "checked_at": datetime.now(UTC).isoformat(), "passed": True,
            "candidate_raw_sha256": digest(raw_path), "metadata_sha256": metadata_before,
            "fixed_set": raw["fixed_set"], "cases": checked,
            "saved_and_fresh_read": len(checked), "fresh_app_instances": 2,
            "additional_model_calls": 0, "actual_aws_calls": 0,
            "source_sha256": digest(Path(__file__)),
            "scope": "실제 생성 내용 보존·합성 식별자 UUID 대응 → Dispatcher 출력 검증 → Workflow 가드레일 1~3 → PostgreSQL commit → 새 앱·세션 상세 조회",
            "limits": "원자료의 합성 식별자는 DB UUID로 일대일 대응했다. 온라인 생성·저장 연속 실행과 Intake 재실행·실제 AWS 사전검사는 포함하지 않는다. 가드레일 4단계는 대역이다.",
            "api_examples": responses,
        }
        (ROUND / "saved-output-verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(f"PASS: {len(checked)}/6 saved and fresh-read; model_calls=0; AWS_calls=0")
    finally:
        if engine is not None:
            engine.dispose()
        if get_engine.cache_info().currsize:
            get_engine().dispose()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        if created:
            # 이 실행이 생성에 성공한 고유 DB만 정리한다.
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    main()
