"""ClaudeModelClient 테스트 — 요청 형태·스키마 변환·토큰 정규화·응답/상태 분류·재시도 (#434 §4-2).

실제 API는 부르지 않는다. httpx2.MockTransport로 전송 계층만 바꿔 실제 HTTP 요청(헤더·본문)을
검증하고 준비된 응답·예외를 순서대로 낸다.
"""

import json
import logging
from typing import Optional

import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, Field, RootModel

from ai.claude_client import (
    API_VERSION,
    MESSAGES_PATH,
    ClaudeModelClient,
    _transform_schema,
    build_claude_model_client,
)
from ai.model_client import (
    AIModelAdapterError,
    AIModelContractError,
    AIModelRejectedError,
    AIModelRequest,
    AIModelTimeoutError,
    AIModelUnavailableError,
    FailureKind,
)
from ai.model_retry import RetryPolicy

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
API_KEY = "sk-ant-unit-test-key"
MODEL = "claude-haiku-5-5"
BASE_URL = "https://unit.test"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str


class _Transport:
    """준비된 결과(httpx2.Response 또는 예외)를 순서대로 내고 받은 요청을 기록한다."""

    def __init__(self, results):
        self._results = list(results)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        outcome = self._results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def body(self, index=0) -> dict:
        return json.loads(self.requests[index].content)


def _usage(
    input_tokens=100,
    output_tokens=20,
    cache_read: Optional[int] = 900,
    cache_creation: Optional[int] = 50,
    service_tier: Optional[str] = "standard",
    speed: Optional[str] = None,
) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "service_tier": service_tier,
        "speed": speed,
    }


def _message(
    text='{"verdict": "ok"}',
    stop_reason="end_turn",
    usage="default",
    model="claude-haiku-5-5-20260101",
    content="default",
) -> httpx2.Response:
    if usage == "default":
        usage = _usage()
    if content == "default":
        content = [{"type": "text", "text": text}]
    body = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": model,
        "stop_reason": stop_reason,
        "content": content,
        "usage": usage,
    }
    return httpx2.Response(200, json=body)


def _error(status, error_type="api_error", message="boom", headers=None) -> httpx2.Response:
    return httpx2.Response(
        status,
        headers=headers or {},
        json={"type": "error", "error": {"type": error_type, "message": message}},
    )


def _policy(max_attempts=3, output_retries=0, max_retry_after_seconds=60.0):
    return RetryPolicy(
        timeout_seconds=1.0,
        max_attempts=max_attempts,
        retry_backoff_seconds=0.0,
        max_retry_after_seconds=max_retry_after_seconds,
        output_retries=output_retries,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    delays = []
    monkeypatch.setattr("ai.model_retry.time.sleep", delays.append)
    return delays


@pytest.fixture
def sleeps(_no_sleep):
    return _no_sleep


def _client(results, effort=None, speed=None, max_tokens=512, **policy_kwargs):
    transport = _Transport(results)
    policy = _policy(**policy_kwargs)
    client = build_claude_model_client(
        api_key=API_KEY,
        model=MODEL,
        policy=policy,
        max_tokens=max_tokens,
        effort=effort,
        speed=speed,
        base_url=BASE_URL,
    )
    # 실제 빌더가 만든 헤더·base_url을 그대로 쓰고 전송만 가짜로 바꾼다
    client._client = httpx2.Client(
        base_url=BASE_URL,
        headers=client._client.headers,
        transport=httpx2.MockTransport(transport),
    )
    return client, transport


def _request(payload=None):
    return AIModelRequest(system_prompt="너는 FinOps 분석기다.", user_payload=payload or {})


# --- 요청 형태 ---------------------------------------------------------------------


def test_request_shape_is_masked_and_minimal():
    client, transport = _client([_message()])
    client.complete(_request({"note": f"key {AWS_KEY}", "n": 1}), Answer)

    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == f"{BASE_URL}{MESSAGES_PATH}"
    assert request.headers["x-api-key"] == API_KEY
    assert request.headers["anthropic-version"] == API_VERSION
    assert request.headers["content-type"] == "application/json"

    body = transport.body()
    assert AWS_KEY not in request.content.decode()
    assert body["system"] == "너는 FinOps 분석기다."
    assert body["messages"] == [{"role": "user", "content": body["messages"][0]["content"]}]
    assert "[REDACTED]" in body["messages"][0]["content"]
    assert body["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "title": "Answer",
                "properties": {"verdict": {"type": "string", "title": "Verdict"}},
                "additionalProperties": False,
                "required": ["verdict"],
            },
        }
    }
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config"}
    assert body["model"] == MODEL
    assert body["max_tokens"] == 512


def test_effort_and_speed_are_sent_only_when_set():
    client, transport = _client([_message()], effort="low", speed="fast")
    client.complete(_request(), Answer)

    body = transport.body()
    assert body["output_config"]["effort"] == "low"
    assert body["speed"] == "fast"


def test_max_tokens_must_be_positive():
    with pytest.raises(ValueError):
        _client([], max_tokens=0)


def test_builder_rejects_missing_api_key():
    with pytest.raises(ValueError):
        build_claude_model_client(api_key=None, model=MODEL, policy=_policy(), max_tokens=64)


def test_builder_uses_policy_timeout_and_has_no_transport_retry():
    client = build_claude_model_client(
        api_key=API_KEY, model=MODEL, policy=_policy(), max_tokens=64
    )

    http = client._client
    assert isinstance(http, httpx2.Client)
    assert str(http.base_url).rstrip("/") == "https://api.anthropic.com"
    assert http.timeout == httpx2.Timeout(1.0)
    # 재시도 소유자는 엔진 하나 — 전송 계층에는 재시도가 없다
    assert http._transport._pool._retries == 0
    assert "x-api-key" in http.headers
    assert API_KEY not in repr(http)


# --- 스키마 변환 -------------------------------------------------------------------


class _Item(BaseModel):
    name: str = Field(description="이름", min_length=1)
    score: int = Field(ge=0, le=10)
    tags: list[str] = Field(default_factory=list, min_length=2)


class _Nested(BaseModel):
    items: list[_Item]
    note: Optional[str] = None
    kind: str = Field(pattern="^[a-z]+$")


def test_transform_schema_moves_unsupported_keywords_into_description():
    schema = _transform_schema(_Nested.model_json_schema())

    item = schema["$defs"]["_Item"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["name", "score"]
    assert item["properties"]["name"]["description"] == "이름\n\n{minLength: 1}"
    assert item["properties"]["score"] == {
        "type": "integer",
        "title": "Score",
        "description": "{maximum: 10, minimum: 0}",
    }
    assert item["properties"]["tags"]["description"] == "{minItems: 2}"
    assert "minItems" not in item["properties"]["tags"]
    assert schema["properties"]["items"]["items"] == {"$ref": "#/$defs/_Item"}
    assert schema["properties"]["note"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert schema["properties"]["kind"]["description"] == "{pattern: ^[a-z]+$}"
    assert schema["additionalProperties"] is False


def test_transform_schema_keeps_root_ref_definitions():
    class _Root(RootModel[list[_Item]]):
        pass

    schema = _transform_schema(_Root.model_json_schema())

    assert schema["items"] == {"$ref": "#/$defs/_Item"}
    assert "_Item" in schema["$defs"]


def test_transform_schema_keeps_defs_when_root_is_a_ref():
    # pydantic RootModel[Model]은 루트가 {"$ref": ..., "$defs": ...}다 — $ref 조기 반환 전에
    # $defs를 변환해야 정의를 잃지 않는다
    class _Root(RootModel[_Item]):
        pass

    schema = _transform_schema(_Root.model_json_schema())

    assert schema["$ref"] == "#/$defs/_Item"
    assert schema["$defs"]["_Item"]["additionalProperties"] is False


def test_transform_schema_requires_a_type():
    with pytest.raises(ValueError):
        _transform_schema({"description": "no type"})


def test_schema_transform_failure_is_adapter_error(monkeypatch):
    import ai.claude_client as module

    class _Unconvertible(BaseModel):
        value: int

    def boom(_schema):
        raise TypeError("cannot convert")

    monkeypatch.setattr(module, "_transform_schema", boom)
    client, transport = _client([_message()])
    with pytest.raises(AIModelAdapterError) as info:
        client.complete(_request(), _Unconvertible)

    assert info.value.kind is FailureKind.ADAPTER_ERROR
    assert info.value.trace is not None
    assert transport.requests == []
    assert info.value.__context__ is None


# --- 성공 · 사용량 ------------------------------------------------------------------


def test_success_returns_output_model_and_normalized_usage():
    client, _ = _client([_message(usage=_usage(speed="fast"))], speed="fast")
    response = client.complete(_request(), Answer)

    assert response.output == Answer(verdict="ok")
    assert response.model == "claude-haiku-5-5-20260101"
    assert response.usage.prompt_tokens == 1050
    assert response.usage.cached_prompt_tokens == 900
    assert response.usage.cache_creation_prompt_tokens == 50
    assert response.usage.completion_tokens == 20
    assert response.usage.total_tokens == 1070
    assert response.usage.unreported == ()
    assert response.trace.provider == "anthropic"
    assert response.trace.requested_service_tier == "fast"
    assert response.trace.returned_service_tier == "fast"
    assert response.trace.attempt_count == 1


def test_service_tier_is_used_when_speed_is_missing():
    client, _ = _client([_message()])
    response = client.complete(_request(), Answer)

    assert response.trace.returned_service_tier == "standard"


def test_missing_cache_fields_are_marked_unreported():
    usage = {"input_tokens": 10, "output_tokens": 5}
    client, _ = _client([_message(usage=usage)])
    response = client.complete(_request(), Answer)

    assert response.usage.prompt_tokens == 10
    assert response.usage.cached_prompt_tokens == 0
    assert response.usage.cache_creation_prompt_tokens is None
    assert response.usage.unreported == ("cached_prompt_tokens",)
    assert response.trace.returned_service_tier is None


def test_missing_usage_is_all_unreported():
    client, _ = _client([_message(usage=None)])
    response = client.complete(_request(), Answer)

    assert response.usage.total_tokens == 0
    assert response.usage.unreported == (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_prompt_tokens",
    )


# --- 응답 판정 ---------------------------------------------------------------------


def test_refusal_keeps_usage_and_is_not_retried():
    client, transport = _client([_message(stop_reason="refusal", text="")])
    with pytest.raises(AIModelRejectedError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.REFUSAL
    assert info.value.phase == "response"
    assert info.value.usage.prompt_tokens == 1050
    assert len(transport.requests) == 1


@pytest.mark.parametrize("reason", ["max_tokens", "model_context_window_exceeded"])
def test_truncation_is_a_contract_error_with_usage(reason):
    client, _ = _client([_message(stop_reason=reason, text='{"verd')])
    with pytest.raises(AIModelContractError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.TRUNCATED
    assert info.value.usage.completion_tokens == 20


def test_invalid_json_is_not_retried_by_default():
    client, transport = _client([_message(text="not json SECRET-ECHO")])
    with pytest.raises(AIModelContractError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.OUTPUT_INVALID
    assert info.value.usage is not None
    assert "SECRET-ECHO" not in str(info.value)
    assert len(transport.requests) == 1


def test_invalid_json_is_retried_within_output_budget_and_usage_is_summed():
    client, transport = _client([_message(text="bad"), _message()], output_retries=1)
    response = client.complete(_request(), Answer)

    assert response.output == Answer(verdict="ok")
    assert len(transport.requests) == 2
    assert response.trace.attempt_count == 2
    assert response.trace.attempts[0].usage.total_tokens == 1070
    assert response.usage.total_tokens == 2140


def test_schema_violation_is_output_invalid():
    client, _ = _client([_message(text='{"verdict": "ok", "extra": 1}')])
    with pytest.raises(AIModelContractError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.OUTPUT_INVALID


def test_message_without_text_block_is_response_shape():
    client, _ = _client([_message(content=[{"type": "thinking", "thinking": "..."}])])
    with pytest.raises(AIModelAdapterError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.RESPONSE_SHAPE
    assert info.value.usage.prompt_tokens == 1050


def test_non_json_200_body_is_response_shape_with_unknown_usage():
    client, _ = _client([httpx2.Response(200, text="<html>upstream</html>")])
    with pytest.raises(AIModelAdapterError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.RESPONSE_SHAPE
    assert "prompt_tokens" in info.value.usage.unreported
    assert "upstream" not in str(info.value)


# --- 전송 예외 · 상태 코드 분류 ---------------------------------------------------


def _timeout():
    return httpx2.ReadTimeout("timed out")


def _connection():
    return httpx2.ConnectError("refused")


@pytest.mark.parametrize(
    "make, expected_cls, kind",
    [
        (_timeout, AIModelTimeoutError, FailureKind.TRANSPORT_TIMEOUT),
        (_connection, AIModelUnavailableError, FailureKind.TRANSPORT_CONNECTION),
        (
            lambda: httpx2.RemoteProtocolError("closed"),
            AIModelUnavailableError,
            FailureKind.TRANSPORT_CONNECTION,
        ),
        (lambda: _error(429, "rate_limit_error"), AIModelUnavailableError, FailureKind.RATE_LIMITED),
        (lambda: _error(500, "api_error"), AIModelUnavailableError, FailureKind.SERVER_ERROR),
        (lambda: _error(529, "overloaded_error"), AIModelUnavailableError, FailureKind.SERVER_ERROR),
        (lambda: _error(503), AIModelUnavailableError, FailureKind.SERVER_ERROR),
        (lambda: _error(504), AIModelUnavailableError, FailureKind.SERVER_ERROR),
        (lambda: _error(409), AIModelUnavailableError, FailureKind.RATE_LIMITED),
        (lambda: _error(408), AIModelUnavailableError, FailureKind.RATE_LIMITED),
    ],
)
def test_transient_errors_are_retried_then_raised_without_transport_chain(
    make, expected_cls, kind
):
    client, transport = _client([make(), make(), make()], max_attempts=3)
    with pytest.raises(expected_cls) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is kind
    assert len(transport.requests) == 3
    assert info.value.trace.attempt_count == 3
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert API_KEY not in repr(info.value) + repr(info.value.trace)


def test_error_class_is_known_provider_type_or_status_code():
    client, _ = _client([_error(529, "overloaded_error"), _error(500, "<script>")], max_attempts=2)
    with pytest.raises(AIModelUnavailableError) as info:
        client.complete(_request(), Answer)

    attempts = info.value.trace.attempts
    assert attempts[0].error_class == "overloaded_error"
    assert attempts[1].error_class == "http_500"  # 알려지지 않은 문자열은 남기지 않는다


def test_retry_after_header_is_honored(sleeps):
    client, _ = _client([_error(429, "rate_limit_error", headers={"retry-after": "20"}), _message()])
    response = client.complete(_request(), Answer)

    assert sleeps == [20.0]
    assert response.trace.attempts[0].server_directed is True


def test_retry_after_above_cap_is_not_followed(sleeps):
    err = _error(429, "rate_limit_error", headers={"retry-after": "300"})
    client, _ = _client([err, _message()], max_retry_after_seconds=30.0)
    response = client.complete(_request(), Answer)

    assert sleeps == [0.0]  # backoff(0초)로 간다
    assert response.trace.attempts[0].server_directed is False


@pytest.mark.parametrize(
    "error_type, code",
    [
        ("authentication_error", 401),
        ("permission_error", 403),
        ("not_found_error", 404),
        ("request_too_large", 413),
        ("invalid_request_error", 422),
    ],
)
def test_request_errors_are_rejected_without_retry(error_type, code):
    client, transport = _client([_error(code, error_type)])
    with pytest.raises(AIModelRejectedError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.REQUEST_REJECTED
    assert info.value.trace.attempts[0].status_code == code
    assert info.value.trace.attempts[0].error_class == error_type
    assert len(transport.requests) == 1


def test_billing_error_is_quota_exhausted():
    client, transport = _client([_error(402, "billing_error")])
    with pytest.raises(AIModelRejectedError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.QUOTA_EXHAUSTED
    assert len(transport.requests) == 1


def test_bad_request_about_schema_is_schema_rejected():
    err = _error(400, "invalid_request_error", "output_config.format.schema: unsupported keyword")
    client, transport = _client([err])
    with pytest.raises(AIModelContractError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.SCHEMA_REJECTED
    assert len(transport.requests) == 1
    assert "unsupported keyword" not in str(info.value)


def test_other_bad_request_is_request_rejected():
    client, _ = _client([_error(400, "invalid_request_error", "bad")])
    with pytest.raises(AIModelRejectedError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.REQUEST_REJECTED


def test_effort_only_bad_request_is_request_rejected():
    err = _error(400, "invalid_request_error", "output_config.effort: unsupported effort value")
    client, _ = _client([err])
    with pytest.raises(AIModelRejectedError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.REQUEST_REJECTED


def test_error_without_json_body_is_classified_by_status():
    client, _ = _client([httpx2.Response(502, text="bad gateway")] * 3)
    with pytest.raises(AIModelUnavailableError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.SERVER_ERROR
    assert info.value.trace.attempts[0].error_class == "http_502"


def test_unclassified_exception_is_adapter_error():
    client, transport = _client([AttributeError("boom")])
    with pytest.raises(AIModelAdapterError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.ADAPTER_ERROR
    assert len(transport.requests) == 1
    assert info.value.__context__ is None


# --- 로그 -------------------------------------------------------------------------


def test_transport_debug_logs_are_silenced(caplog):
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("httpx2").info("HTTP Request: POST %s", "https://api.anthropic.com/v1/messages")
        logging.getLogger("httpx2").debug("request body")
        logging.getLogger("httpcore2.http11").debug("send_request_headers.failed")

    assert not caplog.records


def test_malformed_key_never_reaches_logs_through_real_transport(caplog):
    # 실제 HTTPTransport + 가짜 소켓: 헤더 값이 비정상(개행)이면 h11이 전송 단계에서 거절하고
    # httpcore2가 DEBUG로 그 헤더 값을 통째로 남긴다 — 로거 차단이 없으면 키가 로그에 찍힌다
    import httpcore2
    from httpcore2._backends.mock import MockBackend

    bad_key = "sk-ant-malformed-key\n"
    transport = httpx2.HTTPTransport()
    transport._pool = httpcore2.ConnectionPool(network_backend=MockBackend([]))
    client = build_claude_model_client(
        api_key=bad_key, model=MODEL, policy=_policy(max_attempts=1), max_tokens=64,
        base_url="http://unit.test",
    )
    client._client = httpx2.Client(
        base_url="http://unit.test", headers=client._client.headers, transport=transport
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(AIModelUnavailableError) as info:
        client.complete(_request(), Answer)

    assert info.value.kind is FailureKind.TRANSPORT_CONNECTION
    assert info.value.__context__ is None
    assert "sk-ant-malformed" not in caplog.text
    assert "sk-ant-malformed" not in repr(info.value) + repr(info.value.trace)
