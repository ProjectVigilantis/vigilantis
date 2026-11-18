"""Luna InvokeModel 형식과 공통 Bedrock 경계의 결합. AWS·유료 모델 호출 없음."""

import io
import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict

from ai.agent import SecOpsEvidenceDecisionOutput
from ai.bedrock_openai_client import build_bedrock_openai_model_client
from ai.evaluation.secops import runner
from ai.evaluation.secops.dataset import load_cases
from ai.model_client import (
    AIModelAdapterError,
    AIModelContractError,
    AIModelRejectedError,
    AIModelRequest,
    FailureKind,
)
from services.aws.client import TenantAccess


OWNER = TenantAccess(
    "aaaaaaaa-1111-4000-8000-000000000001",
    "arn:aws:iam::111111111111:role/customer",
    "external-id-test",
)
MODEL = "global.openai.gpt-6-luna"
REQUEST = AIModelRequest("판정", {"secret": "AKIAIOSFODNN7EXAMPLE", "account_id": "111111111111"})


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str
    note: str | None = None


def message(*, finish="stop", content='{"verdict":"ok","note":null}', refusal=None):
    return {
        "model": "gpt-6-luna-snapshot",
        "service_tier": "default",
        "choices": [{"finish_reason": finish, "message": {
            "role": "assistant", "content": content, "refusal": refusal,
        }}],
        "usage": {
            "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }


def runtime(outcome):
    value = Mock()
    if isinstance(outcome, Exception):
        value.invoke_model.side_effect = outcome
        stream = None
    else:
        stream = io.BytesIO(outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode())
        value.invoke_model.return_value = {"body": stream}
    return value, stream


def client(factory, **kwargs):
    return build_bedrock_openai_model_client(
        owner=OWNER, consent_check=lambda: True, region="ap-northeast-2", model_id=MODEL,
        max_attempts=1, runtime_factory=factory, **kwargs,
    )


def test_native_request_masks_input_uses_strict_schema_and_disables_implicit_cache():
    sdk, stream = runtime(message())
    factory = Mock(return_value=sdk)
    response = client(factory, reasoning_effort="high").complete(REQUEST, Answer)
    assert response.output == Answer(verdict="ok")
    request = sdk.invoke_model.call_args.kwargs
    assert request["modelId"] == MODEL
    assert request["accept"] == request["contentType"] == "application/json"
    body = json.loads(request["body"])
    assert body["model"] == MODEL and body["max_completion_tokens"] == 8192
    assert body["reasoning_effort"] == "high"
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    assert body["messages"][0] == {"role": "system", "content": "판정"}
    user = json.loads(body["messages"][1]["content"])
    assert user == {"secret": "[REDACTED]", "account_id": "111111111111"}
    assert all(isinstance(item["content"], str) for item in body["messages"])
    assert "store" not in body and "prompt_cache_breakpoint" not in body
    output_format = body["response_format"]
    assert output_format["type"] == "json_schema"
    assert output_format["json_schema"]["strict"] is True
    schema = output_format["json_schema"]["schema"]
    assert schema["required"] == ["verdict", "note"] and schema["additionalProperties"] is False
    assert "default" not in schema["properties"]["note"]
    assert response.usage.prompt_tokens == 12
    assert response.usage.completion_tokens == 8
    assert response.usage.total_tokens == 20
    assert response.usage.cached_prompt_tokens == 4
    assert response.usage.cache_creation_prompt_tokens == 3
    assert response.usage.unreported == ()
    assert response.trace.provider == "bedrock"
    assert response.trace.requested_model == MODEL
    assert response.trace.returned_model == "gpt-6-luna-snapshot"
    factory.assert_called_once_with(
        OWNER, region="ap-northeast-2", connect_timeout=5.0, read_timeout=120.0,
    )
    sdk.close.assert_called_once()
    assert stream.closed


@pytest.mark.parametrize("changes,kind", [
    ({"refusal": "declined", "content": "secret-invalid-json"}, FailureKind.REFUSAL),
    ({"finish": "length", "content": "secret-invalid-json"}, FailureKind.TRUNCATED),
    ({"finish": "content_filter", "content": None}, FailureKind.REFUSAL),
    ({"content": '{"wrong":"secret-response"}'}, FailureKind.OUTPUT_INVALID),
    ({"content": "secret-invalid-json"}, FailureKind.OUTPUT_INVALID),
    ({"content": None}, FailureKind.RESPONSE_SHAPE),
    ({"finish": "tool_calls"}, FailureKind.RESPONSE_SHAPE),
    ({"finish": None}, FailureKind.RESPONSE_SHAPE),
])
def test_failed_native_responses_preserve_usage_without_exposing_content(changes, kind):
    sdk, stream = runtime(message(**changes))
    factory = Mock(return_value=sdk)
    with pytest.raises((AIModelRejectedError, AIModelContractError, AIModelAdapterError)) as caught:
        client(factory).complete(REQUEST, Answer)
    error = caught.value
    assert error.kind is kind
    assert error.usage.prompt_tokens == 12 and error.usage.completion_tokens == 8
    assert "secret-" not in str(error)
    assert error.__context__ is None and error.__cause__ is None
    sdk.invoke_model.assert_called_once()
    sdk.close.assert_called_once()
    assert stream.closed


@pytest.mark.parametrize("choices", [[], None, ["invalid"], [{"message": None}]])
def test_invalid_envelope_retains_reported_usage(choices):
    response = message()
    response["choices"] = choices
    sdk, stream = runtime(response)
    with pytest.raises(AIModelAdapterError) as caught:
        client(Mock(return_value=sdk)).complete(REQUEST, Answer)
    assert caught.value.kind is FailureKind.RESPONSE_SHAPE
    assert caught.value.usage.total_tokens == 20 and stream.closed


def test_partial_usage_keeps_known_counts_and_marks_unknown_fields():
    response = message()
    response["usage"] = {"prompt_tokens": 12}
    sdk, _ = runtime(response)
    result = client(Mock(return_value=sdk)).complete(REQUEST, Answer)
    assert result.usage.prompt_tokens == 12 and result.usage.total_tokens == 12
    assert result.usage.completion_tokens == 0
    assert result.usage.unreported == ("completion_tokens", "total_tokens", "cached_prompt_tokens")
    assert result.usage.cache_creation_prompt_tokens is None
    body = json.loads(sdk.invoke_model.call_args.kwargs["body"])
    assert "reasoning_effort" not in body


@pytest.mark.parametrize("role", [None, "user", "system"])
def test_only_assistant_messages_can_produce_structured_output(role):
    response = message()
    reply = response["choices"][0]["message"]
    if role is None:
        del reply["role"]
    else:
        reply["role"] = role
    sdk, stream = runtime(response)
    with pytest.raises(AIModelAdapterError) as caught:
        client(Mock(return_value=sdk)).complete(REQUEST, Answer)
    assert caught.value.kind is FailureKind.RESPONSE_SHAPE
    assert stream.closed


def test_reported_cache_usage_survives_an_unknown_prompt_count():
    response = message()
    response["usage"] = {
        "completion_tokens": 8,
        "prompt_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 3},
    }
    sdk, _ = runtime(response)
    result = client(Mock(return_value=sdk)).complete(REQUEST, Answer)
    assert result.usage.cached_prompt_tokens == 4
    assert result.usage.cache_creation_prompt_tokens == 3
    assert result.usage.completion_tokens == 8
    assert result.usage.unreported == ("prompt_tokens", "total_tokens")


def test_absent_usage_is_unknown_not_reported_zero():
    response = message()
    del response["usage"]
    sdk, _ = runtime(response)
    result = client(Mock(return_value=sdk)).complete(REQUEST, Answer)
    assert result.usage.unreported == (
        "prompt_tokens", "completion_tokens", "total_tokens", "cached_prompt_tokens",
    )
    assert result.usage.cache_creation_prompt_tokens is None


def test_consent_revocation_blocks_next_gpt_attempt_and_closes_failed_runtime(monkeypatch):
    monkeypatch.setattr("ai.model_retry.time.sleep", lambda _: None)
    sdk, _ = runtime(ClientError({
        "Error": {"Code": "ThrottlingException", "Message": "private-error"},
        "ResponseMetadata": {"HTTPStatusCode": 429},
    }, "InvokeModel"))
    factory = Mock(return_value=sdk)
    consent = Mock(side_effect=[True, False])
    adapter = build_bedrock_openai_model_client(
        owner=OWNER, consent_check=consent, region="ap-northeast-2", model_id=MODEL,
        max_attempts=2, runtime_factory=factory,
    )
    with pytest.raises(AIModelRejectedError) as caught:
        adapter.complete(REQUEST, Answer)
    assert caught.value.phase == "consent" and caught.value.trace.attempt_count == 2
    factory.assert_called_once()
    sdk.invoke_model.assert_called_once()
    sdk.close.assert_called_once()
    assert "private-error" not in str(caught.value)


def test_invalid_reasoning_effort_fails_before_credentials():
    factory = Mock()
    with pytest.raises(ValueError):
        client(factory, reasoning_effort="automatic")
    factory.assert_not_called()


def test_native_gpt_adapter_runs_the_same_secops_evaluation_graph():
    case = load_cases()[0]
    output = SecOpsEvidenceDecisionOutput(
        observation="SSH 실패가 관측됐다.", diagnosis="반복 인증 시도다.",
        rationale="출발지 차단을 제안한다.", decision=[1],
    )
    sdk, stream = runtime(message(content=output.model_dump_json()))
    record = runner.run_one(case, 1, client(Mock(return_value=sdk)))
    assert record["data_source"] == "bedrock_model"
    assert record["output"]["invocation_status"] == "SUCCEEDED"
    assert record["failure_kind"] is None
    assert record["model_calls"] == record["physical_attempts"] == 1
    assert record["reported_prompt_tokens"] == 12
    body = json.loads(sdk.invoke_model.call_args.kwargs["body"])
    schema = body["response_format"]["json_schema"]
    assert schema["name"] == "SecOpsEvidenceDecisionOutput"
    assert set(schema["schema"]["required"]) == {"observation", "diagnosis", "rationale", "decision"}
    assert stream.closed
