"""실제 SDK 직렬화 지문으로 측정 통과 summary v3와 채택한 V1 단가 요청을 대조한다. 외부 호출 0회."""

import hashlib
import json
from pathlib import Path

import httpx
from ai import agent, rate_estimator
from ai.evaluation.summary import finops_cases
from ai.model_retry import RetryPolicy
from ai.openai_client import OpenAIModelClient
from openai import OpenAI
from schemas.assets import AssetInventory
from schemas.candidates import CandidateStatus, RunbookCandidateData

ROOT = Path(__file__).resolve().parents[4]
# #583 3단계 2라운드 통과 summary v3와 2026-09-16 후보 B의 SDK 요청 지문.
# 단가 요청은 그대로이며 요약·후보 요청은 실측 통과 판에 맞춰 재고정했다. 테스트 중 재생성하지 않는다.
APPROVED_REQUESTS = {
    "A1": [
        "f9883e13431f757a9dc350ed38cfae92a441957e49ba96de9067417fc9066d56",
        "015d5bb41614cd1f33e2820e92bdd6442321ce800c9c44c2e2cccb6742cf453e",
        "52a88b16a76b73390946ebcf7cae24d01a286baa49aac0a44b46f079c2ce3fc9",
    ],
    "A7": [
        "7f1b3e3f5566cc145430788f491d50835bfa9d509f1fa126aaa1994a31616cb5",
        "0270ed7b445996cd9417297f8728b1e3a36ab4bf885d6138f44616d499fe80f5",
        "f6d5f9a653a2b9291d0d80d73d77d76af0265118967cc5d3fcd0a7406ed81880",
    ],
    "A11": [
        "7980b6652c002191bcc9f33f2a7274ffe41537df49bc911877eca6f474beec22",
        "0a0f47ca10e47212ff0c2ac696f0c527ff311cbdd3e620be0b6c402f4d2df434",
        "db0acd1ab555e69776b67f5689c3c5f25cecd47f0851dbd93b1ef9998d5ea931",
    ],
    "A12": [
        "87467503812d56514d9416ba30bbea3aa76dd03f0f2277c1916b74f005ccd550",
        "3af4b39b3ef57fa535d0ab7866409bd413ba4aa86a03d4d23c11ef722c247a80",
        "bab833caa199c1888350896959a5942c327f0e20247232bd39e037c5c38328ee",
    ],
    "A14": [
        "d494a8d6a84014ec63cc153d2becd001757f343bf252250b07a4392f68bf4677",
        "0b6ae89954c1bbfcdc54d32182b87d90d0ad1c61bac6db42ea8d0845fa2c58ef",
        "f52bf4451e49f424c5568f3f05d56767f92cbb74a28ec6de22bbf0acc4741229",
    ],
    "A16": [
        "130074123da0e2e971d5e6b18081de05a83741f2d634c24d4c9e7bc2022959a5",
        "4d8e09f81c0653d65255b2efb5e84350d37a859de50806e3204c5f4cc86e37fe",
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
    # decision은 메뉴(capabilities 순서) 안의 다운사이징 action_id다. 대상은 코드가 정하므로 후보에 없다
    action_id = 1 + [
        item.runbook_id.value for item in case.graph_input.capabilities
    ].index("RUNBOOK_EC2_RIGHTSIZING")
    responses = {
        "FinOpsEvidenceDecisionOutput": {
            "observation": "관측된 CPU 사용률이 낮다.",
            "diagnosis": "과대 사양일 가능성이 있다.",
            "rationale": "사양 변경을 검토할 근거다.",
            "decision": [action_id],
        },
        "FinOpsCandidateProposalOutput": {"candidates": [{
            "runbook_id": "RUNBOOK_EC2_RIGHTSIZING",
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
            policy=RetryPolicy(timeout_seconds=30, max_attempts=1, retry_backoff_seconds=0),
        )
        output = agent.run_finops_graph(case.graph_input, client=client)
        draft = output.candidates[0]
        assert draft.target_arn == asset.arn
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
        "a0fab6a357b80fbf87f775b6007bb411852584586a832d769cc32c691f0b4d4f"
    )
    assert rate_estimator.savings_prompt_fingerprint() == APPROVED_SAVINGS_PROMPT
    for case in cases:
        requests = capture(case)
        assert len(requests) == 3  # 그래프 2 + 독립 단가 1
        assert [digest(body) for body in requests] == APPROVED_REQUESTS[case.case_id]
        assert all(body["model"] == "gpt-5.6-luna" for body in requests)
        assert all(body["reasoning_effort"] == "low" for body in requests)
        assert all("temperature" not in body for body in requests)
