"""실제 SDK 직렬화 지문으로 승인 summary v2와 채택한 V1 단가 요청을 대조한다. 외부 호출 0회."""

import hashlib
import json
from pathlib import Path

import httpx
from ai import agent, rate_estimator
from ai.evaluation.summary import finops_cases
from ai.openai_client import OpenAIModelClient
from openai import OpenAI
from schemas.assets import AssetInventory
from schemas.candidates import CandidateStatus, RunbookCandidateData

ROOT = Path(__file__).resolve().parents[4]
# 승인 summary v2와 2026-09-16 후보 B의 SDK 요청 지문. 현재 출력에서 재생성하지 않는다.
APPROVED_REQUESTS = {
    "A1": [
        "a91300587fa12fc3343f56f1400d950c9fe2a58bbff086abd02fca3d7e77fce9",
        "8a773d7024fda4832a77ef1afaf667b39290bd40f2b14bc798ce3f73172b11fb",
        "52a88b16a76b73390946ebcf7cae24d01a286baa49aac0a44b46f079c2ce3fc9",
    ],
    "A7": [
        "6628fb4b1be3dddfb9e9d89c98a77cd6aa550660d6df1c607295b72e36f7eb18",
        "0b75edc0620c737ddb3b17e20810c09d6268a4f2045595a23719a38edde79a32",
        "f6d5f9a653a2b9291d0d80d73d77d76af0265118967cc5d3fcd0a7406ed81880",
    ],
    "A11": [
        "45ce00d0c670c716376ce92c7bdd060715125d8d9cb8fe97b5cfd911b4c9d37f",
        "f8baa119fadc3f478e0b120e523380f97ecd3230500e2c8c1ba6dd76e44001da",
        "db0acd1ab555e69776b67f5689c3c5f25cecd47f0851dbd93b1ef9998d5ea931",
    ],
    "A12": [
        "f4208dc04f2c74fc45dd94fc65d958a4b84788f8d0a82b5866e2939e3fa0b146",
        "d16da3ee2cf0d6e5b13216dd832fe2b651a989aec184a1328fed3e67075164bd",
        "bab833caa199c1888350896959a5942c327f0e20247232bd39e037c5c38328ee",
    ],
    "A14": [
        "e2a06c286985ce7975a4cb08cb82f52bb801dda6ee6c3e77bd0c963eb191b357",
        "e88aaa47dda1a9894a9ed573f0d916456026d166fb5379ba8bb2620bf4676180",
        "f52bf4451e49f424c5568f3f05d56767f92cbb74a28ec6de22bbf0acc4741229",
    ],
    "A16": [
        "2e8f63cdd54cad74564c10c2813a9ddf3bc5b623ae0bba1ddd61a95ef7a0de5c",
        "4cc45b7165d53b743248472780199f1291b55f522d405892f6ff6a447629465f",
        "3a02e3480cb498012ebb24995fb69e48b74d6c7554c7ee712c64a4f9a5dd5584",
    ],
}
APPROVED_SAVINGS_PROMPT = "b52d6a31b0b7d0a632c8b61a58c6ad7de559ee2d8c6bbc0bb163f7db30e5bbc2"


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


def capture(case):
    requests = []
    asset = case.graph_input.asset_context
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
            "current_hourly_rate": "0.104000", "target_hourly_rate": "0.026000",
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
        output = agent.run_finops_graph(case.graph_input, client=client)
        draft = output.candidates[0]
        candidate = RunbookCandidateData(
            candidate_id="sdk-candidate", incident_id=case.graph_input.incident_id,
            status=CandidateStatus.PENDING_VALIDATION, **draft.model_dump(),
        )
        estimate = rate_estimator.estimate_candidate_savings(candidate, asset=asset, client=client)
        assert estimate.status.value == "ESTIMATED"
    return requests


def test_sdk_requests_match_approved_summary_and_v1_rates():
    cases = fixed_cases()
    assert [case.case_id for case in cases] == list(APPROVED_REQUESTS)
    assert agent.finops_prompt_fingerprint() == (
        "1e2e5c45cd1b1d250ebf371ba65699827bf3c42f88f6d77080515a07c22e1cc7"
    )
    assert rate_estimator.savings_prompt_fingerprint() == APPROVED_SAVINGS_PROMPT
    for case in cases:
        requests = capture(case)
        assert len(requests) == 3  # 그래프 2 + 독립 단가 1
        assert [digest(body) for body in requests] == APPROVED_REQUESTS[case.case_id]
        assert all(body["model"] == "gpt-5.6-luna" for body in requests)
        assert all(body["reasoning_effort"] == "low" for body in requests)
        assert all("temperature" not in body for body in requests)
