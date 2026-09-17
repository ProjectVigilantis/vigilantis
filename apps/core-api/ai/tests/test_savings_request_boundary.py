"""실제 SDK 직렬화 지문으로 승인 v2와 이동 전 단가 요청을 대조한다. 외부 호출 0회."""

import hashlib
import json
from pathlib import Path

import httpx
from ai import agent, savings
from ai.evaluation.summary import finops_cases
from ai.openai_client import OpenAIModelClient
from openai import OpenAI
from schemas.assets import AssetInventory
from schemas.candidates import CandidateStatus, RunbookCandidateData

ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT = ROOT / "apps/core-api/ai/evaluation/savings/request_snapshot.json"


def digest(value):
    # JSON object 순서도 SDK 출력 스키마의 생성 순서이므로 보존한다.
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def fixed_cases():
    golden = ROOT / "datasets/golden/finops"
    cases = []
    for suffix in ("001", "002", "003"):
        name = f"asset_inventory_{suffix}.json"
        inventory = AssetInventory.model_validate_json((golden / "input" / name).read_bytes())
        expected = json.loads((golden / "expected" / name).read_text("utf-8"))
        cases.extend(finops_cases(inventory, expected))
    return cases


def capture(case, graph_module=agent, *, followup=True):
    requests = []
    asset = case.graph_input.asset_context
    context = savings.savings_context(asset)
    responses = {
        "EvidenceSummaryOutput": {
            "observation": "관측된 CPU 사용률이 낮다.",
            "diagnosis": "과대 사양일 가능성이 있다.",
            "rationale": "사양 변경을 검토할 근거다.",
        },
        "CandidateProposalOutput": {"candidates": [{
            "runbook_id": "RUNBOOK_EC2_RIGHTSIZING", "target_arn": asset.arn,
            "evidence_ids": [item.evidence_id for item in case.graph_input.evidences],
        }]},
        "ProposedHourlyRates": {
            "status": "ESTIMATED",
            **{key: context[key] for key in (
                "target_arn", "region", "current_instance_type", "target_instance_type",
            )},
            "current_hourly_rate": "0.104000", "target_hourly_rate": "0.026000",
            "explanation": "SDK 검증용 합성 단가다.",
        },
    }

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        name = body["response_format"]["json_schema"]["name"]
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 0,
            "model": "gpt-5.6-luna",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(responses[name]),
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    with OpenAI(api_key="test-key", base_url="https://unit.test/v1", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as sdk:
        client = OpenAIModelClient(
            client=sdk, model="gpt-5.6-luna", reasoning_effort="low", temperature=None,
            max_attempts=1, timeout_seconds=30, retry_backoff_seconds=0,
        )
        output = graph_module.run_finops_graph(case.graph_input, client=client)
        if followup:
            draft = output.candidates[0]
            candidate = RunbookCandidateData(
                candidate_id="sdk-candidate", incident_id=case.graph_input.incident_id,
                status=CandidateStatus.PENDING_VALIDATION, **draft.model_dump(),
            )
            estimate = savings.estimate_candidate_savings(candidate, asset=asset, client=client)
            assert estimate.status.value == "ESTIMATED"
    return requests


def test_sdk_requests_match_approved_summary_and_pre_move_rates():
    snapshot = json.loads(SNAPSHOT.read_text("utf-8"))
    cases = fixed_cases()
    assert [case.case_id for case in cases] == snapshot["case_ids"]
    assert agent.finops_prompt_fingerprint() == snapshot["approved_v2_prompt_sha256"]
    assert savings.savings_prompt_fingerprint() == snapshot["savings_prompt_sha256"]
    for case in cases:
        requests = capture(case)
        assert len(requests) == 3  # 그래프 2 + 독립 단가 1
        assert [digest(body) for body in requests] == snapshot["requests"][case.case_id]
        assert all(body["model"] == "gpt-5.6-luna" for body in requests)
        assert all(body["reasoning_effort"] == "low" for body in requests)
        assert all("temperature" not in body for body in requests)
