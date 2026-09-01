"""AIModelClient 경계 테스트 — 전송 페이로드 마스킹·재시도 분류·예외 변환·호출 메타 로그.

마스킹은 로그로 사후 확인할 수 없다(ADR-0005가 Prompt 전문 저장·출력을 금지).
자격증명이 실제로 나가지 않는다는 근거는 여기의 test_secret_never_reaches_the_sdk와
test_outbound_payload_drops_secret_originals다 — SSOT 주차 종료 판정 기준 ⓕ.
"""

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

# httpx2는 openai SDK가 쓰는 전송 라이브러리. SDK 예외를 만들 때 Request/Response가
# 필요해 테스트에서만 참조한다.
import httpx2
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from ai.model_client import (
    AIModelContractError,
    AIModelRejectedError,
    AIModelRequest,
    AIModelTimeoutError,
    AIModelUnavailableError,
    FakeAIModelClient,
    build_outbound_payload,
    mask_outbound,
)
from ai.openai_client import OpenAIModelClient, build_openai_model_client

# --- 픽스처 ---------------------------------------------------------------------

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
TEMP_KEY = "ASIAY34FZKBOKMUTVV7A"
OPENAI_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
INSTANCE_ARN = "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc1234def567890"

_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status_response(code, headers=None):
    return httpx2.Response(code, request=_REQUEST, headers=headers or {})


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: str


class Other(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str


class _FakeCompletions:
    """chat.completions.parse 스텁 — 준비된 결과를 순서대로 내고 호출 인자를 기록한다."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self._results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _completion(parsed, refusal=None, usage=(11, 7, 18), model="gpt-4o", cached=None):
    message = SimpleNamespace(parsed=parsed, refusal=refusal)
    usage_obj = SimpleNamespace(
        prompt_tokens=usage[0], completion_tokens=usage[1], total_tokens=usage[2]
    )
    if cached is not None:
        usage_obj.prompt_tokens_details = SimpleNamespace(cached_tokens=cached)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)], usage=usage_obj, model=model
    )


def _client(
    results,
    max_attempts=3,
    max_retry_after_seconds=60.0,
    temperature=None,
    reasoning_effort=None,
):
    completions = _FakeCompletions(results)
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = OpenAIModelClient(
        client=sdk,
        model="gpt-4o",
        timeout_seconds=1.0,
        max_attempts=max_attempts,
        retry_backoff_seconds=0.0,  # 테스트에서 실제로 대기하지 않는다
        max_retry_after_seconds=max_retry_after_seconds,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
    )
    return client, completions


def _request(payload=None):
    return AIModelRequest(system_prompt="너는 FinOps 분석기다.", user_payload=payload or {})


# --- 마스킹 대상 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "secret", [AWS_KEY, TEMP_KEY, OPENAI_KEY, "ghp_0123456789abcdefghij"]
)
def test_credential_shaped_strings_are_masked(secret):
    masked = mask_outbound("배포 노트: " + secret + " 사용")
    assert secret not in masked
    assert "[REDACTED]" in masked


@pytest.mark.parametrize(
    ("text", "label", "value"),
    [
        (
            "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "aws_secret_access_key",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        ),
        ("password: hunter2", "password", "hunter2"),
        ("API-Key : abcd1234efgh5678", "API-Key", "abcd1234efgh5678"),
        ("token=ghp_0123456789abcdef", "token", "ghp_0123456789abcdef"),
        ("db_password=hunter2", "db_password", "hunter2"),  # 접두 변형
        (
            "Authorization: Bearer ghp_0123456789abcdefghij",
            "Authorization",
            "ghp_0123456789abcdefghij",
        ),
        # 인증 방식과 무관하게 헤더 값 전체가 가려져야 한다 — Basic은 스킴 뒤
        # base64가 자격증명 본체다
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization", "dXNlcjpwYXNz"),
        ("Authorization: Digest username=admin", "Authorization", "username=admin"),
    ],
)
def test_labeled_secrets_keep_label_and_drop_value(text, label, value):
    masked = mask_outbound(text)
    assert label in masked  # 무엇이 지워졌는지는 읽을 수 있어야 한다
    assert value not in masked
    assert "[REDACTED]" in masked


def test_secret_named_dict_key_masks_its_string_value():
    # 태그 키는 자유 문자열이라 값이 아니라 키가 비밀 라벨을 담는다
    masked = mask_outbound(
        {
            "tags": {"db_password": "hunter2", "Owner": "finops"},
            "headers": {"Authorization": "Bearer abc12345"},
        }
    )
    assert masked["tags"]["db_password"] == "[REDACTED]"
    assert masked["tags"]["Owner"] == "finops"
    assert masked["headers"]["Authorization"] == "[REDACTED]"


def test_json_text_inside_a_string_is_masked():
    masked = mask_outbound('로그 원문: {"password": "hunter2"}')
    assert "hunter2" not in masked
    assert "password" in masked


def test_authorization_masking_stops_at_line_end():
    masked = mask_outbound("Authorization: Basic dXNlcjpwYXNz\nHost: api.example.com")
    assert "dXNlcjpwYXNz" not in masked
    assert "Host: api.example.com" in masked  # 다음 줄은 판단 근거로 보존


def test_masking_keeps_the_rest_of_a_single_line_json_record():
    # CloudTrail·VPC flow log는 한 줄 JSON이다 — 비밀값만 지우고 판단 재료는 남겨야 한다
    record = (
        '{"authorization": "Bearer abc12345", "password": "hunter2", '
        '"src_ip": "203.0.113.9", "errorCode": "AccessDenied"}'
    )
    masked = mask_outbound(record)

    assert "abc12345" not in masked
    assert "hunter2" not in masked
    assert '"src_ip": "203.0.113.9"' in masked
    assert '"errorCode": "AccessDenied"' in masked


# --- 마스킹 비대상 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        INSTANCE_ARN,
        "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-0abc1234",
        "i-0abc1234def567890",
        "123456789012",
        "ap-northeast-2",
        "sk-prod-cluster",  # sk-로 시작하는 사람 이름표 — 키가 아니다
        "subnet-0f1e2d3c",
    ],
)
def test_contract_identifiers_are_not_masked(value):
    assert mask_outbound(value) == value


def test_masking_walks_nested_structures_and_keeps_keys():
    payload = {
        "asset_context": {
            "arn": INSTANCE_ARN,
            "account_id": "123456789012",
            "spec": {"tags": {"Owner": "finops", "Note": "key " + AWS_KEY}},
        },
        "evidences": [
            {"content": {"raw": "login with password: hunter2"}},
            {"content": {"raw": "정상 트래픽"}},
        ],
    }
    masked = mask_outbound(payload)

    assert masked["asset_context"]["arn"] == INSTANCE_ARN
    assert masked["asset_context"]["account_id"] == "123456789012"
    assert masked["asset_context"]["spec"]["tags"]["Owner"] == "finops"
    assert AWS_KEY not in masked["asset_context"]["spec"]["tags"]["Note"]
    assert "hunter2" not in masked["evidences"][0]["content"]["raw"]
    assert masked["evidences"][1]["content"]["raw"] == "정상 트래픽"
    # 키는 계약 필드명이라 건드리지 않는다
    assert set(masked["asset_context"]) == {"arn", "account_id", "spec"}


def test_non_serializable_payload_is_a_contract_error():
    # AssetItem.collected_at의 UtcDateTime은 when_used="json"이라 python 모드
    # model_dump()에서 datetime으로 남는다 — 경계 밖으로 TypeError가 나가면 안 된다
    request = AIModelRequest(
        system_prompt="분석",
        user_payload={"collected_at": datetime(2026, 8, 25, tzinfo=timezone.utc)},
    )
    with pytest.raises(AIModelContractError) as exc_info:
        build_outbound_payload(request)

    assert "직렬화" in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_fake_client_also_rejects_non_serializable_payload():
    # 주입 구현이 이 검사를 건너뛰면 Fake로 통과한 배선이 실제 호출에서 터진다
    fake = FakeAIModelClient([Answer(verdict="ok")])
    request = AIModelRequest(
        system_prompt="분석",
        user_payload={"collected_at": datetime(2026, 8, 25, tzinfo=timezone.utc)},
    )
    with pytest.raises(AIModelContractError):
        fake.complete(request, Answer)


def test_outbound_payload_drops_secret_originals():
    request = AIModelRequest(
        system_prompt="참고 키 " + OPENAI_KEY,
        user_payload={"arn": INSTANCE_ARN, "note": AWS_KEY + " 로 접근"},
    )
    blob = json.dumps(build_outbound_payload(request), ensure_ascii=False)

    assert OPENAI_KEY not in blob
    assert AWS_KEY not in blob
    assert INSTANCE_ARN in blob


# --- 주입용 구현 -----------------------------------------------------------------


def test_fake_client_returns_prepared_output_and_records_masked_payload():
    fake = FakeAIModelClient([Answer(verdict="downsize")])
    request = _request({"note": AWS_KEY + " 로 접근", "arn": INSTANCE_ARN})

    response = fake.complete(request, Answer)

    assert response.output.verdict == "downsize"
    sent = json.dumps(fake.sent[0], ensure_ascii=False)
    assert AWS_KEY not in sent  # 주입 구현도 같은 마스킹 지점을 지난다
    assert INSTANCE_ARN in sent


def test_fake_client_rejects_output_of_another_type():
    fake = FakeAIModelClient([Other(note="wrong")])
    with pytest.raises(AIModelContractError):
        fake.complete(_request(), Answer)


def test_fake_client_runs_out_of_prepared_outputs():
    fake = FakeAIModelClient([])
    with pytest.raises(AIModelUnavailableError):
        fake.complete(_request(), Answer)


def test_builder_rejects_missing_api_key():
    from config import Settings

    # 명시 인자가 .env·환경변수보다 우선하므로 None 주입이 안전하다
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://t:t@localhost:5432/t", OPENAI_API_KEY=None
    )
    with pytest.raises(ValueError):
        build_openai_model_client(settings)


def test_builder_disables_sdk_retries():
    from config import Settings

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://t:t@localhost:5432/t",
        OPENAI_API_KEY="sk-test",
    )
    client = build_openai_model_client(settings)

    # 재시도 소유자는 래퍼 하나 — SDK 기본 2회가 살아 있으면 시도 수가 곱으로 는다
    assert client._client.max_retries == 0


def test_sdk_debug_logs_are_silenced(caplog):
    # ADR-0005: SDK가 DEBUG에서 요청 본문(messages 전문)을 로그로 남긴다 —
    # 앱 LOG_LEVEL과 무관하게 SDK 로거가 막혀 있어야 한다
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("openai._base_client").debug("Request options: %s", "PROMPT")
        logging.getLogger("httpx2").debug("request body")

    assert not caplog.records


# --- 캐시된 입력 토큰 (#237) ------------------------------------------------------
# 캐시분은 prompt_tokens에 포함돼 오지만 단가가 다르다. 기록하지 않으면 계측이 낸
# 비용이 실제 청구와 어긋나고, 반복 계측(같은 프롬프트 N회)과 실경로(인시던트마다
# 다른 입력)의 비용을 구분해 말할 수 없다.


def test_cached_prompt_tokens_are_recorded():
    client, _ = _client([_completion(Answer(verdict="ok"), usage=(1200, 40, 1240), cached=1024)])

    response = client.complete(_request(), Answer)

    assert response.usage.prompt_tokens == 1200
    assert response.usage.cached_prompt_tokens == 1024


def test_missing_cache_details_fall_back_to_zero():
    # 필드를 주지 않는 응답(구버전·타 제공자)에서도 서야 한다
    client, _ = _client([_completion(Answer(verdict="ok"))])

    assert client.complete(_request(), Answer).usage.cached_prompt_tokens == 0


def test_cached_tokens_cannot_exceed_prompt_tokens():
    # 캐시분이 입력보다 크면 비용이 음수가 된다 — 상한을 건다
    client, _ = _client([_completion(Answer(verdict="ok"), usage=(100, 5, 105), cached=999)])

    assert client.complete(_request(), Answer).usage.cached_prompt_tokens == 100


# --- 실패 위상·usage 보존 (#237) --------------------------------------------------
# 예외 클래스만으로는 실패가 왕복의 어느 자리에서 났는지 갈리지 않는다(계약 위반은
# 요청·응답 양쪽에 쓰인다). 응답을 받은 실패는 토큰이 이미 발생했으므로 usage를
# 예외에 실어 보존한다 — 버리면 계측 비용이 실제 청구보다 적게 잡힌다.


def test_response_side_failures_carry_phase_and_usage():
    client, _ = _client([_completion(None, refusal="거절", usage=(1200, 0, 1200), cached=1024)])

    with pytest.raises(AIModelRejectedError) as excinfo:
        client.complete(_request(), Answer)

    assert excinfo.value.phase == "response"
    assert excinfo.value.usage.prompt_tokens == 1200
    assert excinfo.value.usage.cached_prompt_tokens == 1024


def test_contract_failure_after_response_preserves_usage():
    empty = SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=900, completion_tokens=0, total_tokens=900),
        model="gpt-4o",
    )
    client, _ = _client([empty])

    with pytest.raises(AIModelContractError) as excinfo:
        client.complete(_request(), Answer)

    assert excinfo.value.phase == "response"
    assert excinfo.value.usage.prompt_tokens == 900


def test_transport_failures_carry_transport_phase():
    client, _ = _client([APITimeoutError(_REQUEST)], max_attempts=1)

    with pytest.raises(AIModelTimeoutError) as excinfo:
        client.complete(_request(), Answer)

    assert excinfo.value.phase == "transport"
    assert excinfo.value.usage is None


# --- 모델 동작 노브 (#237) --------------------------------------------------------
# 켠 노브만 요청에 실려야 한다. 모델 계열마다 받는 파라미터가 달라(gpt-4o는
# temperature, gpt-5 계열 추론 모델은 reasoning_effort) 안 쓰는 노브가 요청 본문에
# 나타나면 그 호출 자체가 400으로 거절된다.


@pytest.mark.parametrize(
    ("temperature", "reasoning_effort", "expected"),
    [
        (None, None, {}),
        # temperature=0은 falsy다 — 값의 참거짓이 아니라 None 여부로 갈라야 한다.
        # 재현성 측정에서 가장 먼저 쓸 값이 0이라 이 칸이 비면 측정이 통째로 어긋난다
        (0.0, None, {"temperature": 0.0}),
        (None, "low", {"reasoning_effort": "low"}),
        (0.2, "high", {"temperature": 0.2, "reasoning_effort": "high"}),
    ],
)
def test_only_configured_model_params_reach_the_sdk(temperature, reasoning_effort, expected):
    client, completions = _client(
        [_completion(Answer(verdict="ok"))],
        temperature=temperature,
        reasoning_effort=reasoning_effort,
    )

    client.complete(_request(), Answer)

    sent = completions.calls[0]
    assert {k: sent[k] for k in ("temperature", "reasoning_effort") if k in sent} == expected


def test_model_params_are_carried_into_retries():
    # 재시도는 같은 호출의 재실행이다 — 1회차와 다른 파라미터로 가면 재현성 측정에서
    # 같은 케이스가 두 설정으로 돈 셈이 된다
    client, completions = _client(
        [APITimeoutError(_REQUEST), _completion(Answer(verdict="ok"))],
        max_attempts=2,
        temperature=0.0,
    )

    client.complete(_request(), Answer)

    assert [call["temperature"] for call in completions.calls] == [0.0, 0.0]


def test_builder_carries_model_params_only_when_set():
    from config import Settings

    base = {
        "DATABASE_URL": "postgresql+psycopg://t:t@localhost:5432/t",
        "OPENAI_API_KEY": "sk-test",
    }
    # 명시 인자가 .env·환경변수보다 우선하므로 None 주입이 "미설정"을 재현한다
    unset = build_openai_model_client(
        Settings(**base, OPENAI_TEMPERATURE=None, OPENAI_REASONING_EFFORT=None)
    )
    tuned = build_openai_model_client(
        Settings(**base, OPENAI_TEMPERATURE=0, OPENAI_REASONING_EFFORT="low")
    )

    assert unset._model_params == {}
    assert tuned._model_params == {"temperature": 0.0, "reasoning_effort": "low"}


def test_unknown_reasoning_effort_is_rejected_at_startup():
    from config import Settings

    # 오타를 호출 시점 400이 아니라 기동 시점 검증 오류로 드러낸다
    with pytest.raises(ValidationError):
        Settings(
            DATABASE_URL="postgresql+psycopg://t:t@localhost:5432/t",
            OPENAI_REASONING_EFFORT="lowest",
        )


# --- 성공 경로 -------------------------------------------------------------------


def test_successful_call_returns_parsed_output_and_usage():
    client, completions = _client([_completion(Answer(verdict="isolate"))])

    response = client.complete(_request({"arn": INSTANCE_ARN}), Answer)

    assert response.output.verdict == "isolate"
    assert response.model == "gpt-4o"
    assert (response.usage.prompt_tokens, response.usage.total_tokens) == (11, 18)
    assert completions.calls[0]["response_format"] is Answer
    assert completions.calls[0]["timeout"] == 1.0


def test_secret_never_reaches_the_sdk():
    client, completions = _client([_completion(Answer(verdict="ok"))])
    payload = {"arn": INSTANCE_ARN, "tags": {"Note": "key " + AWS_KEY}}

    client.complete(_request(payload), Answer)

    blob = json.dumps(completions.calls[0]["messages"], ensure_ascii=False)
    assert AWS_KEY not in blob
    assert "[REDACTED]" in blob
    assert INSTANCE_ARN in blob  # 대상 지목에 필요한 값은 그대로 나간다


# --- 재시도 분류 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (APITimeoutError(_REQUEST), AIModelTimeoutError),
        (APIConnectionError(request=_REQUEST), AIModelUnavailableError),
        (
            RateLimitError("429", response=_status_response(429), body=None),
            AIModelUnavailableError,
        ),
        (
            InternalServerError("500", response=_status_response(500), body=None),
            AIModelUnavailableError,
        ),
        # 전용 예외 클래스가 없는 408·409 — SDK 재시도 정책(_should_retry) 승계
        (
            APIStatusError("408", response=_status_response(408), body=None),
            AIModelUnavailableError,
        ),
        (
            APIStatusError("409", response=_status_response(409), body=None),
            AIModelUnavailableError,
        ),
    ],
)
def test_transient_errors_retry_up_to_the_limit(error, expected):
    client, completions = _client([error, error, error], max_attempts=3)

    with pytest.raises(expected) as exc_info:
        client.complete(_request(), Answer)

    assert len(completions.calls) == 3
    # SDK 예외는 request(Authorization 헤더)를 물고 있다 — 체인 어디에도 남기지 않는다
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_transient_error_then_success():
    client, completions = _client(
        [APITimeoutError(_REQUEST), _completion(Answer(verdict="ok"))], max_attempts=3
    )

    response = client.complete(_request(), Answer)

    assert response.output.verdict == "ok"
    assert len(completions.calls) == 2


def test_server_directed_retry_after_is_honored(monkeypatch):
    # SDK 재시도를 껐으므로 Retry-After 존중은 래퍼가 승계한다
    error = RateLimitError(
        "429", response=_status_response(429, {"retry-after": "20"}), body=None
    )
    client, _ = _client([error, _completion(Answer(verdict="ok"))])
    slept = []
    monkeypatch.setattr("ai.openai_client.time.sleep", slept.append)

    client.complete(_request(), Answer)

    assert slept == [20.0]


def test_retry_after_ms_header_is_honored(monkeypatch):
    error = RateLimitError(
        "429", response=_status_response(429, {"retry-after-ms": "1500"}), body=None
    )
    client, _ = _client([error, _completion(Answer(verdict="ok"))])
    slept = []
    monkeypatch.setattr("ai.openai_client.time.sleep", slept.append)

    client.complete(_request(), Answer)

    assert slept == [1.5]


def test_retry_after_beyond_cap_falls_back_to_backoff(monkeypatch):
    # 요청 경로에서 부르는 호출이라 서버가 과하게 길게 지시하면 따르지 않는다
    error = RateLimitError(
        "429", response=_status_response(429, {"retry-after": "600"}), body=None
    )
    client, _ = _client(
        [error, _completion(Answer(verdict="ok"))], max_retry_after_seconds=60.0
    )
    slept = []
    monkeypatch.setattr("ai.openai_client.time.sleep", slept.append)

    client.complete(_request(), Answer)

    assert slept != [600.0]


def test_backoff_carries_jitter(monkeypatch):
    # 지터가 없으면 동시에 실패한 호출이 같은 시각에 다시 몰린다
    client, _ = _client(
        [APITimeoutError(_REQUEST), _completion(Answer(verdict="ok"))]
    )
    client._retry_backoff_seconds = 4.0
    slept = []
    monkeypatch.setattr("ai.openai_client.time.sleep", slept.append)
    monkeypatch.setattr("ai.openai_client.random.random", lambda: 1.0)

    client.complete(_request(), Answer)

    assert slept == [3.0]  # 4.0 * 1회 * (1 - 0.25)


def test_connection_error_without_response_uses_backoff(monkeypatch):
    client, _ = _client(
        [APIConnectionError(request=_REQUEST), _completion(Answer(verdict="ok"))]
    )
    slept = []
    monkeypatch.setattr("ai.openai_client.time.sleep", slept.append)

    client.complete(_request(), Answer)

    assert slept == [0.0]  # retry_backoff_seconds=0 → 지터를 곱해도 0


def test_max_attempts_one_means_no_retry():
    client, completions = _client([APITimeoutError(_REQUEST)], max_attempts=1)

    with pytest.raises(AIModelTimeoutError):
        client.complete(_request(), Answer)

    assert len(completions.calls) == 1


def test_auth_error_is_not_retried():
    error = AuthenticationError("401", response=_status_response(401), body=None)
    client, completions = _client([error, error, error], max_attempts=3)

    with pytest.raises(AIModelRejectedError) as exc_info:
        client.complete(_request(), Answer)

    assert len(completions.calls) == 1
    # 진단용으로 SDK 예외의 타입 이름만 문자열로 남는다 — 예외 객체 자체는 체인에 없다
    assert "AuthenticationError" in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


# --- 계약 위반 -------------------------------------------------------------------


def test_sdk_validation_error_becomes_contract_error_without_retry():
    # SDK parse는 응답을 pydantic으로 검증한다 — 실패가 OpenAIError 계열이 아니라
    # ValidationError로 올라오므로 경계가 별도로 변환해야 한다
    try:
        Answer()  # verdict 누락
    except ValidationError as exc:
        error = exc

    client, completions = _client([error, error], max_attempts=3)

    with pytest.raises(AIModelContractError) as exc_info:
        client.complete(_request(), Answer)

    assert len(completions.calls) == 1
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_unparsed_response_is_a_contract_error_and_is_not_retried():
    client, completions = _client([_completion(None)], max_attempts=3)

    with pytest.raises(AIModelContractError):
        client.complete(_request(), Answer)

    assert len(completions.calls) == 1


def test_output_of_another_type_is_a_contract_error():
    client, _ = _client([_completion(Other(note="wrong"))])

    with pytest.raises(AIModelContractError):
        client.complete(_request(), Answer)


def test_refusal_is_rejected_not_parsed():
    client, _ = _client([_completion(None, refusal="거절")])

    with pytest.raises(AIModelRejectedError):
        client.complete(_request(), Answer)


def test_empty_choices_is_a_contract_error():
    client, _ = _client([SimpleNamespace(choices=[], usage=None, model="gpt-4o")])

    with pytest.raises(AIModelContractError):
        client.complete(_request(), Answer)


# --- 호출 메타 로그 --------------------------------------------------------------


def test_token_usage_is_logged_without_cost_or_prompt(caplog):
    client, _ = _client([_completion(Answer(verdict="ok"))])

    with caplog.at_level(logging.INFO, logger="vigilantis.ai"):
        client.complete(_request({"note": "무해한 값"}), Answer)

    records = [r for r in caplog.records if r.getMessage() == "ai_model_call"]
    assert len(records) == 1
    fields = records[0].__dict__

    assert fields["total_tokens"] == 18
    assert fields["attempts"] == 1
    # ADR-0005 미보존 대상 — 단가·비용과 Prompt·원문 응답은 로그에 남기지 않는다
    assert not {"cost", "price", "usd", "prompt", "messages", "response"} & set(fields)


def test_usage_is_logged_even_on_refusal(caplog):
    # refusal·계약 위반 응답도 토큰을 쓴다 — 사용량 집계는 판정과 무관하게 남는다
    client, _ = _client([_completion(None, refusal="거절")])

    with caplog.at_level(logging.INFO, logger="vigilantis.ai"):
        with pytest.raises(AIModelRejectedError):
            client.complete(_request(), Answer)

    records = [r for r in caplog.records if r.getMessage() == "ai_model_call"]
    assert len(records) == 1
    assert records[0].__dict__["total_tokens"] == 18
