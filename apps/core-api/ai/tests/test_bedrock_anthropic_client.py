"""호출자가 지정한 native InvokeModel 요청·결과·실패·재시도. AWS 전송·과금 없음."""

import io
import json

import pytest
from botocore.exceptions import (
    ClientError,
    HTTPClientError,
    ProxyConnectionError,
    ReadTimeoutError,
    SSLError,
)
from botocore.response import StreamingBody
from pydantic import BaseModel, ConfigDict

from ai.bedrock_anthropic_client import BedrockModelClient, build_bedrock_model_client
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
from services.aws.client import TenantAccess

OWNER = TenantAccess(
    "aaaaaaaa-1111-4000-8000-000000000001",
    "arn:aws:iam::111111111111:role/customer",
    "external-id-test",
)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str


def message(**overrides):
    value = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": '{"verdict":"ok"}'}],
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
    }
    return {**value, **overrides}


class Runtime:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []
        self.closed = False
        self.stream = None

    def invoke_model(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        raw = self.outcome if isinstance(self.outcome, bytes) else json.dumps(self.outcome).encode()
        self.stream = io.BytesIO(raw)
        return {"body": self.stream}

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, *outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.runtimes = []

    def __call__(self, owner, **kwargs):
        self.calls.append((owner, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        runtime = Runtime(outcome)
        self.runtimes.append(runtime)
        return runtime


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    delays = []
    monkeypatch.setattr("ai.model_retry.time.sleep", delays.append)
    return delays


def client(
    factory, *, owner=OWNER, consent=lambda: True, attempts=3,
    region="ap-northeast-2", model_id="global.anthropic.claude-sonnet-5-5",
):
    return BedrockModelClient(
        owner=owner,
        consent_check=consent,
        region=region,
        model_id=model_id,
        policy=RetryPolicy(155, attempts, 0),
        max_tokens=8192,
        connect_timeout=5,
        read_timeout=120,
        runtime_factory=factory,
    )


REQUEST = AIModelRequest("판정", {"account_id": "111111111111", "secret": "AKIAIOSFODNN7EXAMPLE"})


@pytest.mark.parametrize("region,model_id", [
    ("ap-northeast-2", "global.anthropic.claude-sonnet-5-5"),
    ("us-east-1", "global.anthropic.claude-opus-5"),
])
def test_native_request_uses_caller_target_and_masks_credentials_not_identifiers(region, model_id):
    factory = Factory(message())
    response = client(factory, region=region, model_id=model_id).complete(REQUEST, Answer)
    assert response.output.verdict == "ok"
    assert response.usage.total_tokens == 15
    assert response.trace.provider == "bedrock"
    assert response.trace.requested_model == model_id
    assert response.trace.returned_model is None
    assert factory.calls == [(OWNER, {"region": region, "connect_timeout": 5, "read_timeout": 120})]
    runtime = factory.runtimes[0]
    request = runtime.requests[0]
    assert request["modelId"] == model_id
    assert request["contentType"] == request["accept"] == "application/json"
    body = json.loads(request["body"])
    assert set(body) == {"anthropic_version", "max_tokens", "system", "messages", "output_config"}
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert body["max_tokens"] == 8192
    user = body["messages"][0]["content"]
    assert "111111111111" in user and "[REDACTED]" in user
    assert "AKIAIOSFODNN7EXAMPLE" not in user
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert runtime.closed and runtime.stream.closed


@pytest.mark.parametrize("owner,consent", [(None, True), (OWNER, False)])
def test_missing_owner_or_authorization_never_acquires_credentials(owner, consent):
    factory = Factory()
    with pytest.raises(AIModelRejectedError) as caught:
        client(factory, owner=owner, consent=lambda: consent).complete(REQUEST, Answer)
    assert caught.value.phase == "consent"
    assert factory.calls == []


def test_revocation_between_attempts_prevents_another_credential_or_model_call(no_sleep):
    factory = Factory(ClientError(
        {"Error": {"Code": "Throttling"}, "ResponseMetadata": {"HTTPStatusCode": 429}},
        "AssumeRole",
    ))
    consent = iter([True, False])
    with pytest.raises(AIModelRejectedError) as caught:
        client(factory, consent=lambda: next(consent)).complete(REQUEST, Answer)
    assert caught.value.phase == "consent"
    assert len(factory.calls) == 1 and factory.runtimes == []
    assert len(no_sleep) == 1


def test_transient_credential_failure_has_one_engine_budget_and_no_sdk_error_chain(no_sleep):
    secret = "do-not-log-role-and-external-id"
    errors = [
        ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": secret},
             "ResponseMetadata": {"HTTPStatusCode": 503}},
            "AssumeRole",
        )
        for _ in range(3)
    ]
    factory = Factory(*errors)
    with pytest.raises(AIModelUnavailableError) as caught:
        client(factory).complete(REQUEST, Answer)
    error = caught.value
    assert error.phase == "credentials" and error.kind is FailureKind.SERVER_ERROR
    assert len(factory.calls) == 3 and factory.runtimes == []
    assert len(no_sleep) == 2
    assert secret not in str(error)
    assert error.__context__ is None and error.__cause__ is None


def test_runtime_timeout_is_separate_from_credential_failure_and_closes_client():
    runtime = Runtime(ReadTimeoutError(endpoint_url="https://unit.test"))
    calls = []

    def factory(owner, **kwargs):
        calls.append(owner)
        return runtime

    with pytest.raises(AIModelTimeoutError) as caught:
        client(factory, attempts=1).complete(REQUEST, Answer)
    assert caught.value.phase == "transport"
    assert calls == [OWNER] and runtime.closed


@pytest.mark.parametrize("error", [
    SSLError(endpoint_url="https://unit.test", error="secret-network-diagnostic"),
    ProxyConnectionError(proxy_url="https://proxy.unit.test", error="secret-network-diagnostic"),
    HTTPClientError(error="secret-network-diagnostic"),
])
def test_connection_family_errors_are_retried_transport_failures(error, no_sleep):
    runtimes = iter([Runtime(error), Runtime(message())])
    used = []

    def factory(owner, **kwargs):
        used.append(next(runtimes))
        return used[-1]

    response = client(factory).complete(REQUEST, Answer)
    assert response.output.verdict == "ok"
    assert response.trace.attempts[0].outcome == "transport_connection"
    assert "secret-network-diagnostic" not in repr(response.trace)
    assert len(used) == 2 and all(runtime.closed for runtime in used)
    assert len(no_sleep) == 1


@pytest.mark.parametrize("original_status,kind", [
    (503, FailureKind.SERVER_ERROR),
    (429, FailureKind.RATE_LIMITED),
    (400, FailureKind.REQUEST_REJECTED),
])
def test_model_error_retries_only_transient_original_status(original_status, kind, no_sleep):
    secret = "private-provider-diagnostic"
    runtimes = [
        Runtime(ClientError(
            {"Error": {"Code": "ModelErrorException", "Message": secret},
             "ResponseMetadata": {"HTTPStatusCode": 424},
             "originalStatusCode": original_status},
            "InvokeModel",
        )),
        Runtime(message()),
    ]
    calls = []

    def factory(owner, **kwargs):
        runtime = runtimes[len(calls)]
        calls.append(runtime)
        return runtime

    if original_status == 400:
        with pytest.raises(AIModelRejectedError) as caught:
            client(factory).complete(REQUEST, Answer)
        assert caught.value.kind is kind
        assert secret not in str(caught.value)
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        assert len(calls) == 1 and no_sleep == []
    else:
        response = client(factory).complete(REQUEST, Answer)
        assert response.output.verdict == "ok"
        assert response.trace.attempts[0].outcome == kind.value
        assert secret not in repr(response.trace)
        assert len(calls) == 2 and len(no_sleep) == 1
    assert all(runtime.closed for runtime in calls)


@pytest.mark.parametrize("detail,kind,exception", [
    ("output_config.format.schema is invalid", FailureKind.SCHEMA_REJECTED, AIModelContractError),
    ("output_config.effort is invalid", FailureKind.REQUEST_REJECTED, AIModelRejectedError),
])
def test_validation_error_preserves_schema_classification_without_leaking(detail, kind, exception, no_sleep):
    secret = "private-provider-content"
    runtime = Runtime(ClientError(
        {"Error": {"Code": "ValidationException", "Message": f"{detail}: {secret}"},
         "ResponseMetadata": {"HTTPStatusCode": 400}},
        "InvokeModel",
    ))
    with pytest.raises(exception) as caught:
        client(lambda owner, **kwargs: runtime).complete(REQUEST, Answer)
    assert caught.value.kind is kind
    assert secret not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert len(runtime.requests) == 1 and no_sleep == [] and runtime.closed


@pytest.mark.parametrize("interruption", ["truncated", "reset"])
def test_interrupted_response_retries_and_closes_streams_without_leaking_errors(interruption):
    from urllib3.exceptions import ProtocolError

    class ResetStream(io.BytesIO):
        def read(self, *args):
            raise ProtocolError("secret-network-diagnostic")

    raw = io.BytesIO(b"{") if interruption == "truncated" else ResetStream(b"")
    broken = StreamingBody(raw, 20)
    streams = iter([broken, io.BytesIO(json.dumps(message()).encode())])
    runtimes = []

    class StreamRuntime:
        def __init__(self):
            self.closed = False

        def invoke_model(self, **kwargs):
            return {"body": next(streams)}

        def close(self):
            self.closed = True

    def factory(owner, **kwargs):
        runtime = StreamRuntime()
        runtimes.append(runtime)
        return runtime

    response = client(factory).complete(REQUEST, Answer)
    assert response.output.verdict == "ok"
    assert len(runtimes) == 2 and all(runtime.closed for runtime in runtimes)
    assert raw.closed
    assert response.trace.attempts[0].outcome == "transport_connection"
    assert "secret-network-diagnostic" not in repr(response.trace)


@pytest.mark.parametrize("outcome,exception,kind", [
    (message(stop_reason="refusal"), AIModelRejectedError, FailureKind.REFUSAL),
    (message(stop_reason="max_tokens"), AIModelContractError, FailureKind.TRUNCATED),
    (message(content=[{"type": "text", "text": '{"wrong":1}'}]),
     AIModelContractError, FailureKind.OUTPUT_INVALID),
    (b"not-json", AIModelAdapterError, FailureKind.RESPONSE_SHAPE),
])
def test_invalid_or_refused_output_is_not_retried(outcome, exception, kind, no_sleep):
    factory = Factory(outcome)
    with pytest.raises(exception) as caught:
        client(factory).complete(REQUEST, Answer)
    assert caught.value.kind is kind
    assert len(factory.calls) == 1 and no_sleep == []


@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use", "pause_turn", "stop_sequence", None])
def test_only_completed_sonnet55_text_response_succeeds(stop_reason, no_sleep):
    reply = message(
        model="claude-sonnet-5-5",
        stop_reason=stop_reason,
        content=[
            {"type": "thinking", "thinking": "synthetic reasoning"},
            {"type": "text", "text": '{"verdict":"ok"}'},
        ],
    )
    if stop_reason is None:
        del reply["stop_reason"]
    factory = Factory(reply)
    model = client(factory, model_id="global.anthropic.claude-sonnet-5-5")

    if stop_reason == "end_turn":
        result = model.complete(REQUEST, Answer)
        assert result.output.verdict == "ok"
    else:
        with pytest.raises(AIModelAdapterError) as caught:
            model.complete(REQUEST, Answer)
        result = caught.value
        assert result.kind is FailureKind.RESPONSE_SHAPE

    assert result.usage.prompt_tokens == 10
    assert result.usage.completion_tokens == 5
    assert result.usage.total_tokens == 15
    assert result.trace.returned_model == "claude-sonnet-5-5"
    assert result.trace.attempt_count == 1
    assert len(factory.calls) == 1 and no_sleep == []
    assert factory.runtimes[0].closed and factory.runtimes[0].stream.closed


def test_factory_accepts_explicit_arguments_without_app_settings():
    factory = Factory(message())
    model = build_bedrock_model_client(
        owner=OWNER, consent_check=lambda: True, region="ap-northeast-1",
        model_id="anthropic.claude-opus-5", max_tokens=2048, connect_timeout=4,
        read_timeout=130, max_attempts=2, runtime_factory=factory,
    )
    response = model.complete(REQUEST, Answer)
    assert response.output.verdict == "ok"
    assert response.trace.timeout_seconds == 164
    assert response.trace.max_attempts == 2
    assert factory.calls == [(OWNER, {"region": "ap-northeast-1", "connect_timeout": 4, "read_timeout": 130})]
    assert json.loads(factory.runtimes[0].requests[0]["body"])["max_tokens"] == 2048


@pytest.mark.parametrize("missing", ["region", "model_id"])
def test_target_must_be_explicit(missing):
    kwargs = dict(owner=OWNER, consent_check=lambda: True, region="ap-northeast-1",
                  model_id="anthropic.claude-opus-5")
    del kwargs[missing]
    with pytest.raises(TypeError):
        build_bedrock_model_client(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"region": ""}, {"model_id": " "}, {"read_timeout": 119},
    {"connect_timeout": 0}, {"connect_timeout": float("nan")},
    {"read_timeout": float("inf")}, {"max_attempts": 0}, {"max_attempts": 6},
])
def test_invalid_factory_arguments_fail_before_credentials(kwargs):
    factory = Factory()
    arguments = dict(owner=OWNER, consent_check=lambda: True, region="ap-northeast-1",
                     model_id="anthropic.claude-opus-5", runtime_factory=factory)
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        build_bedrock_model_client(**arguments)
    assert factory.calls == []
