"""B 서비스 경로 검증. 기본은 SDK 대역 + 실제 로컬 DB·LocalStack, 유료 실행은 별도 인자."""

# 독립 CLI 실행을 위해 아래 저장소 경로 등록 뒤 내부 패키지를 import한다.
# ruff: noqa: E402

import argparse
import json
import logging
import os
import sys
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
sys.path[:0] = [str(ROOT / p) for p in ("apps/core-api", "packages", "scripts")]

import agent_dispatcher
import httpx
import workflows
from alembic import command
from alembic.config import Config
from config import get_settings
from db import models
from db.repositories import assets as assets_repo
from db.repositories import guardrails as guardrails_repo
from db.repositories import incidents as incidents_repo
from db.session import get_db, get_engine, get_session_factory
from fastapi.testclient import TestClient
from main import create_app
from openai import OpenAI
from schemas.agents import FinOpsGraphInput
from schemas.api.incidents import IncidentCategory, IncidentStatus
from schemas.evidence import EvidenceItem, EvidenceType
from schemas.incidents import AgentInvocationStatus
from services.aws.client import aws_client
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ai.agent import finops_prompt_fingerprint
from ai.evaluation.savings.numeric_validation import (
    digest,
    load_cases,
    read,
    sha,
    write,
)
from ai.evaluation.savings.scoring import score_estimate
from ai.model_client import build_outbound_payload
from ai.openai_client import OpenAIModelClient
from ai.rate_estimator import savings_prompt_fingerprint

ROUND = Path(__file__).parent / "results/20260916-numeric-service-1"
B_ROUND = ROUND.parent / "20260916-numeric-output-1"
SPEC_PATH = Path(__file__).parent / "spec.json"
LIMIT = 18
STAGES = ("EvidenceSummaryOutput", "CandidateProposalOutput", "ProposedHourlyRates")


def mapped_graph(case, *, instance_id=None, account_id=None):
    graph = case.graph_input.model_dump(mode="json")
    graph["incident_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "numeric-service:" + case.case_id))
    for item in graph["evidences"]:
        item["evidence_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "numeric-service:" + item["evidence_id"]))
    if instance_id is not None:
        asset = case.graph_input.asset_context
        arn = f"arn:aws:ec2:{asset.region}:{account_id}:instance/{instance_id}"
        # 식별자만 일대일 대응하며 현재/목표 사양과 가격 조건은 그대로 둔다.
        graph = json.loads(json.dumps(graph).replace(asset.arn, arn).replace(asset.resource_id, instance_id))
        graph["asset_context"]["account_id"] = account_id
    return FinOpsGraphInput.model_validate(graph)


def seed(db, graph):
    """Golden 사실값을 보존하고 DB 식별자만 UUID로 만든다. Intake 수용 검증은 별도다."""
    asset = graph.asset_context
    run = assets_repo.start_collection_run(
        db, account_id=asset.account_id, region=asset.region, mode="localstack",
        lookback_days=14, period_seconds=3600,
    )
    assets_repo.upsert_asset(
        db, arn=asset.arn, asset_type=asset.asset_type, resource_id=asset.resource_id,
        account_id=asset.account_id, region=asset.region, spec=asset.spec.model_dump(mode="json"),
        collection_run_id=run.collection_run_id, collected_at=asset.collected_at,
        name=asset.name, state=asset.state,
    )
    db.add(models.Incident(
        incident_id=graph.incident_id, subject_arn=asset.arn, category=IncidentCategory.FINOPS,
        status=IncidentStatus.ANALYZING, agent_invocation_status=AgentInvocationStatus.PENDING,
    ))
    db.flush()
    for item in graph.evidences:
        incidents_repo.add_evidence(db, EvidenceItem(
            **item.model_dump(), incident_id=graph.incident_id, source_type="golden_validation",
            source_id=item.evidence_id, occurred_at=asset.collected_at, collected_at=asset.collected_at,
        ))
    incidents_repo.add_evidence(db, EvidenceItem(
        evidence_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "asset:" + graph.incident_id)),
        incident_id=graph.incident_id, evidence_type=EvidenceType.ASSET,
        source_type="golden_validation", source_id=asset.arn,
        content={"collection_run_id": graph.rule_evaluation.collection_run_id,
                 "asset": asset.model_dump(mode="json")},
        occurred_at=asset.collected_at, collected_at=asset.collected_at,
    ))
    db.commit()
    restored = agent_dispatcher.build_graph_input(db, graph.incident_id)
    assert restored.model_dump(mode="json") == graph.model_dump(mode="json"), "SERVICE_INPUT_DRIFT"
    db.rollback()


@contextmanager
def local_database():
    os.environ.update(DISPATCH_ENABLED="false", SCAN_ENABLED="false", MOCK_THREAT_INBOX_DIR="",
                      AWS_ENDPOINT_URL="http://localhost:4566", AWS_ACCESS_KEY_ID="test",
                      AWS_SECRET_ACCESS_KEY="test", AWS_DEFAULT_REGION="ap-northeast-2")
    url = make_url(os.environ.get("TEST_DATABASE_ADMIN_URL",
        "postgresql+psycopg://vigilantis:vigilantis@localhost:5432/postgres"))
    assert url.host in {"localhost", "127.0.0.1"}, "LOCAL_DATABASE_ONLY"
    name = "vigilantis_numeric_service_" + uuid.uuid4().hex[:12]
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    engine = None
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created = True
        test_url = url.set(database=name)
        os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
        get_settings.cache_clear()
        command.upgrade(Config(str(ROOT / "apps/core-api/alembic.ini")), "head")
        engine = create_engine(test_url)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if get_engine.cache_info().currsize:
            get_engine().dispose()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        if created:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


class MeasuredClient:
    def __init__(self, live, expected=None):
        self.live, self.expected = live, expected
        self.record = {"mode": "ONLINE" if live else "SDK_MOCK", "status": "RUNNING",
                       "started_at": datetime.now(UTC).isoformat(), "calls": [], "cases": []}
        self.path = ROUND / ("metadata.json" if live else "free-service.json")
        self.graph = self.db = None
        self.case_id = None
        self.stage = 0
        self.order = []
        self.active = None

    def persist(self):
        write(self.path, self.record)

    def mock_response(self, request):
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        if name == STAGES[0]:
            output = {"observation": "관측된 CPU 사용률이 낮다.",
                      "diagnosis": "과대 사양일 가능성이 있다.", "rationale": "사양 변경을 검토할 근거다."}
        elif name == STAGES[1]:
            output = {"candidates": [{"runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
                       "target_arn": self.graph.asset_context.arn,
                       "evidence_ids": [item.evidence_id for item in self.graph.evidences]}]}
        else:
            payload = json.loads(body["messages"][1]["content"])["savings_context"]
            prices = read(SPEC_PATH)["price_reference"]["hourly_rates"]
            output = {"status": "ESTIMATED",
                      "current_hourly_rate": prices[payload["current_instance_type"]]["usd"],
                      "target_hourly_rate": prices[payload["target_instance_type"]]["usd"]}
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 0,
            "model": "gpt-5.6-luna", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(output)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    def verify_wire(self, request):
        row = self.active
        assert row is not None and "wire_sha256" not in row, "UNEXPECTED_TRANSPORT_RETRY"
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.6-luna" and body["reasoning_effort"] == "low"
        assert "temperature" not in body
        assert body["messages"] == [
            {"role": "system", "content": self.outbound["system_prompt"]},
            {"role": "user", "content": self.outbound["user_json"]},
        ], "SDK_MESSAGE_DRIFT"
        row["wire_sha256"] = digest(body)
        shape = {"response_format": body["response_format"],
                 "system_prompt": body["messages"][0]["content"]}
        row["request_shape_sha256"] = digest(shape)
        if self.expected is not None:
            assert row["request_shape_sha256"] == self.expected["request_shapes"][row["stage"]]
        if row["stage"] == STAGES[2]:
            normalized = json.loads(json.dumps(body))
            payload = json.loads(normalized["messages"][1]["content"])
            reference = read(SPEC_PATH)["cases"][self.case_id]
            payload["savings_context"]["target_arn"] = reference["target_arn"]
            normalized["messages"][1]["content"] = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            assert digest(normalized) == read(B_ROUND / "preflight.json")["requests"][self.case_id]["wire_sha256"]
            row["b_request_matches_after_fixture_identity_mapping"] = True
        self.persist()

    def complete(self, request, response_model):
        assert not self.db.in_transaction(), "MODEL_WITHIN_DATABASE_TRANSACTION"
        assert self.stage < 3 and response_model.__name__ == STAGES[self.stage], "CALL_ORDER"
        assert len(self.record["calls"]) < LIMIT, "CALL_BUDGET_EXCEEDED"
        if self.stage == 2:
            assert "guardrail:PASS" in self.order, "PRICE_BEFORE_GUARDRAIL_PASS"
        row = {"number": len(self.record["calls"]) + 1, "case_id": self.case_id,
               "stage": response_model.__name__, "status": "STARTED"}
        self.record["calls"].append(row)
        self.order.append(row["stage"])
        self.stage += 1
        self.outbound = build_outbound_payload(request)
        row["outbound_sha256"] = digest(self.outbound)
        self.active = row
        self.persist()
        start = perf_counter()
        try:
            result = self.client.complete(request, response_model)
        except Exception as exc:
            row.update(status="ERROR", error_type=type(exc).__name__)
            raise
        else:
            row.update(status="RETURNED", usage=asdict(result.usage) if result.usage else None,
                       model=result.model)
            return result
        finally:
            row["elapsed_seconds"] = round(perf_counter() - start, 3)
            self.active = None
            self.persist()


def localstack_graphs(audit):
    """이 실행이 만든 로컬 인스턴스만 제공·정리한다. 실제 AWS 호출은 허용하지 않는다."""
    ec2 = aws_client("ec2", "ap-northeast-2")
    # 환경 스위치 해석은 공용 팩토리 몫이다. 검증기는 실제 클라이언트의 목적지만 제한한다.
    assert ec2.meta.endpoint_url == "http://localhost:4566"
    account = aws_client("sts", "ap-northeast-2").get_caller_identity()["Account"]
    owned = []
    audit.record["localstack_fixture"] = {"endpoint": ec2.meta.endpoint_url,
                                         "instance_ids": owned, "cleaned": False}
    try:
        for case in load_cases():
            result = ec2.run_instances(
                ImageId="ami-0123456789abcdef0", MinCount=1, MaxCount=1,
                InstanceType=case.graph_input.asset_context.spec.instance_type,
            )
            instance_id = result["Instances"][0]["InstanceId"]
            owned.append(instance_id)
            audit.persist()
            yield case, mapped_graph(case, instance_id=instance_id, account_id=account)
    finally:
        if owned:
            ec2.terminate_instances(InstanceIds=owned)
            states = [item["State"]["Name"] for reservation in
                      ec2.describe_instances(InstanceIds=owned)["Reservations"]
                      for item in reservation["Instances"]]
            assert len(states) == len(owned) and set(states) == {"terminated"}, "FIXTURE_CLEANUP_FAILED"
        audit.record["localstack_fixture"]["cleaned"] = True
        audit.persist()


def run_cases(audit):
    started = perf_counter()
    with local_database() as engine:
        original_guard = workflows._guard_candidate
        original_lock = incidents_repo.lock_incident

        def guard(*args):
            assert not audit.db.in_transaction()
            result = original_guard(*args)
            audit.order.append("guardrail:" + result.result.result.value)
            audit.record["last_guardrail"] = result.result.model_dump(mode="json")
            audit.persist()
            return result

        def lock(*args):
            audit.order.append("lock")
            return original_lock(*args)

        for case, graph in localstack_graphs(audit):
            audit.graph, audit.case_id, audit.stage, audit.order = graph, case.case_id, 0, []
            with Session(engine, expire_on_commit=False) as db:
                audit.db = db
                seed(db, audit.graph)
                events = []
                with patch.object(workflows, "_guard_candidate", guard), patch.object(
                    incidents_repo, "lock_incident", lock,
                ):
                    report = agent_dispatcher.dispatch_pending_analysis(db, publish=events.append, client=audit)
                audit.record["last_dispatch"] = {"case_id": case.case_id, "report": asdict(report),
                                                "order": list(audit.order)}
                audit.persist()
                assert (report.claimed, report.succeeded, report.errored) == (1, 1, 0), "DISPATCH_FAILED"
                assert audit.order == [*STAGES[:2], "guardrail:PASS", STAGES[2], "lock"], "SERVICE_ORDER"
                candidates = incidents_repo.list_candidates(db, audit.graph.incident_id)
                assert len(candidates) == 1 and candidates[0].status.value == "EXECUTABLE"
                candidate = candidates[0]
                saved = candidate.ai_savings_estimate
                audit.record["last_saved_candidate"] = {
                    "case_id": case.case_id, "status": candidate.status.value, "estimate": saved,
                }
                audit.persist()
                assert saved["status"] == "ESTIMATED", "ESTIMATE_NOT_AVAILABLE"
                assert saved["basis"]["explanation_source"] == "SERVER_TEMPLATE"
                checked = guardrails_repo.latest_for_candidate(db, candidate.candidate_id)
                assert checked.result.value == "PASS" and "ai_savings_estimate" not in checked.validated_command
                assert len(events) == 1
                spec = read(SPEC_PATH)
                expected = {**spec["cases"][case.case_id], "target_arn": graph.asset_context.arn}
                grade = score_estimate(saved, expected, spec["price_reference"], spec["criteria"])
                summary = incidents_repo.get_incident(db, audit.graph.incident_id).summary_lines
                row = {"case_id": case.case_id, "incident_id": audit.graph.incident_id,
                       "grade": grade, "estimate": saved, "summary_lines": summary,
                       "guardrail_steps": checked.steps, "order": list(audit.order)}
                audit.record["cases"].append(row)
                audit.persist()

            def fresh_db():
                with Session(engine) as session:
                    yield session

            first = None
            before = len(audit.record["calls"])
            with patch.object(OpenAIModelClient, "complete", side_effect=AssertionError("UNEXPECTED_READ_MODEL_CALL")):
                for _ in range(2):
                    app = create_app()
                    app.dependency_overrides[get_db] = fresh_db
                    with TestClient(app) as web:
                        for _ in range(2):
                            response = web.get(f"/api/v1/incidents/{audit.graph.incident_id}")
                            assert response.status_code == 200
                            body = response.json()
                            assert body["status"] == "AWAITING_APPROVAL" and body["summary_lines"] == summary
                            assert body["recommendations"][0]["ai_savings_estimate"] == saved
                            assert body["recommendations"][0]["display_parameters"] == {
                                "target_instance_type": saved["basis"]["target_instance_type"],
                            }
                            assert first is None or body == first
                            first = body
                with Session(engine) as db:
                    audit.db = db
                    assert agent_dispatcher.dispatch_pending_analysis(db, client=audit).claimed == 0
            assert before == len(audit.record["calls"]), "MODEL_RECALLED_ON_READ"
            row.update(fresh_apps=2, fresh_reads=4, read_model_calls=0, api_example=first)
            audit.persist()
            print(f"{case.case_id}: saved, 4 fresh reads, {grade['status']}; calls={before}/{LIMIT}", flush=True)
            assert grade["status"] == "PASS", "PRICE_DEVIATION"
    audit.record.update(status="COMPLETED", elapsed_seconds=round(perf_counter() - started, 3))


def current_sources():
    before = read(ROUND / "before.json")
    sources = set(before["source_sha256"]) | {
        "apps/core-api/ai/rate_estimator.py", "apps/core-api/ai/tests/test_rate_estimator.py",
        "apps/core-api/ai/tests/test_savings_service_validation.py",
        "apps/core-api/ai/evaluation/savings/contract.md",
        str(Path(__file__).relative_to(ROOT)).replace("\\", "/"),
    }
    return {p: sha(ROOT / p) for p in sorted(sources)}


def execute(live):
    if not live:
        assert not (ROUND / "started.json").exists(), "ROUND_ALREADY_STARTED"
    expected = read(ROUND / "preflight.json") if live else None
    prior = read(B_ROUND / "analysis.json")
    assert prior["cumulative_attempted_calls"] == 62 and 62 + LIMIT <= 80
    assert prior["metadata_sha256"] == sha(B_ROUND / "metadata.json")
    if live:
        assert not (ROUND / "started.json").exists() and not (ROUND / "metadata.json").exists(), "ROUND_ALREADY_STARTED"
        assert expected["source_sha256"] == current_sources(), "SOURCE_DRIFT"
        assert expected["prior_metadata_sha256"] == sha(B_ROUND / "metadata.json")
        approval = read(ROUND / "approval.json")
        assert approval["approved"] is True
        assert approval["maximum_new_calls"] == LIMIT and approval["total_limit"] == 80
        assert approval["preflight_sha256"] == sha(ROUND / "preflight.json")
        assert expected["free_service_passed"]
        assert expected["spec_sha256"] == sha(SPEC_PATH)
        assert all(sha(ROOT / p) == value for p, value in read(ROUND / "before.json")["history_sha256"].items())
        write(ROUND / "started.json", {"at": datetime.now(UTC).isoformat(), "maximum_calls": LIMIT})
    audit = MeasuredClient(live, expected)
    key = "test-key"
    if live:
        from dotenv import dotenv_values

        key = dotenv_values(Path("C:/labs/team/vigilantis/.env")).get("OPENAI_API_KEY")
        assert key, "KEY_UNAVAILABLE"
    started = perf_counter()
    try:
        with OpenAI(api_key=key, base_url="https://api.openai.com/v1" if live else "https://unit.test/v1",
                    max_retries=0, http_client=httpx.Client(
                        transport=None if live else httpx.MockTransport(audit.mock_response),
                        event_hooks={"request": [audit.verify_wire]},
                    )) as sdk:
            audit.client = OpenAIModelClient(
                client=sdk, model="gpt-5.6-luna", reasoning_effort="low", temperature=None,
                max_attempts=1, timeout_seconds=30, retry_backoff_seconds=0,
            )
            run_cases(audit)
    except Exception as exc:
        audit.record.update(status="STOPPED", error_type=type(exc).__name__,
                            error_code=str(exc) if isinstance(exc, AssertionError) else None)
        raise
    finally:
        audit.record.update(ended_at=datetime.now(UTC).isoformat(),
                            elapsed_seconds=round(perf_counter() - started, 3))
        audit.persist()
    calls = audit.record["calls"]
    assert len(calls) == LIMIT
    if not live:
        shapes = {row["stage"]: row["request_shape_sha256"] for row in calls}
        assert all(row["request_shape_sha256"] == shapes[row["stage"]] for row in calls)
        write(ROUND / "preflight.json", {
            "free_service_passed": True, "maximum_new_calls": LIMIT, "prior_calls": 62,
            "spec_sha256": sha(SPEC_PATH),
            "prior_metadata_sha256": sha(B_ROUND / "metadata.json"),
            "source_sha256": current_sources(), "request_shapes": shapes,
            "graph_inputs": {case.case_id: mapped_graph(case).model_dump(mode="json") for case in load_cases()},
            "summary_fingerprint": finops_prompt_fingerprint(),
            "savings_fingerprint": savings_prompt_fingerprint(),
        })
    else:
        usage = {k: sum((c.get("usage") or {}).get(k, 0) for c in calls)
                 for k in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")}
        cost = (Decimal(usage["prompt_tokens"]) * Decimal("0.20")
                + Decimal(usage["completion_tokens"]) * Decimal("1.20")
                - Decimal(usage["cached_prompt_tokens"]) * Decimal("0.18")) / 1_000_000
        write(ROUND / "analysis.json", {
            "passed": True, "scope": "Six online service paths; not a new 60-repeat price study",
            "new_live_calls": len(calls), "cumulative_live_calls": 62 + len(calls),
            "unused_limit": 18 - len(calls), "usage": usage,
            "cost_usage_estimate_usd": str(cost),
            "cumulative_cost_usage_estimate_usd": str(cost + Decimal(prior["cumulative_cost_usage_estimate_usd"])),
            "metadata_sha256": sha(audit.path), "preflight_sha256": sha(ROUND / "preflight.json"),
        })
    print(json.dumps({"status": audit.record["status"], "live_calls": len(calls) if live else 0,
                      "completed_service_paths": len(audit.record["cases"])}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-approved", action="store_true")
    logging.disable(logging.CRITICAL)
    execute(parser.parse_args().execute_approved)
