"""고객 역할의 Bedrock InvokeModel 어댑터. 앱의 모델 경로를 선택하거나 바꾸지 않는다.

Claude Sonnet 5.5의 Anthropic Messages와 native structured output 계약을 따른다.
모델 ID는 호출자가 지정한다. 다른 공급자의 요청 형식으로 변환하지 않는다.
region은 접속·서명 리전이며, 교차 리전
추론 프로필의 실제 처리 위치를 보장하지 않는다. 모델·리전·계정 폴백은 없다.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

from botocore import exceptions as aws_errors

from ai.claude_client import _classify_message, _schema_for, _token_usage
from ai.model_client import (
    AIModelRequest,
    AIModelResponse,
    FailureKind,
    StructuredOutputT,
    build_outbound_payload,
)
from ai.model_retry import (
    AttemptFailure, AttemptOutcome, AttemptSuccess, ModelCallEngine, RetryPolicy,
    retry_after_seconds,
)
from services.aws.client import (
    BEDROCK_CREDENTIAL_BUDGET_SECONDS,
    BedrockBoundaryError,
    TenantAccess,
    tenant_bedrock_runtime_client,
)


def bedrock_retry_policy(
    *, connect_timeout: float, read_timeout: float, max_attempts: int,
) -> RetryPolicy:
    """인증·연결·응답 대기의 명목 합계. DNS 등을 강제하는 벽시계 deadline은 아니다."""
    if not math.isfinite(connect_timeout) or connect_timeout <= 0:
        raise ValueError("connect_timeout은 유한한 양수여야 합니다")
    if not math.isfinite(read_timeout) or read_timeout < 120:
        raise ValueError("read_timeout은 스키마 컴파일을 위해 120초 이상이어야 합니다")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
        raise ValueError("max_attempts는 1 이상 5 이하 정수여야 합니다")
    return RetryPolicy(
        timeout_seconds=BEDROCK_CREDENTIAL_BUDGET_SECONDS + connect_timeout + read_timeout,
        max_attempts=max_attempts,
        retry_backoff_seconds=1.0,
        max_retry_after_seconds=60.0,
    )


def _failure(exc: Exception, *, phase: str) -> AttemptFailure:
    name = type(exc).__name__
    status = None
    retry_after = None
    if isinstance(exc, aws_errors.ClientError):
        code = exc.response.get("Error", {}).get("Code")
        metadata = exc.response.get("ResponseMetadata", {})
        status = metadata.get("HTTPStatusCode")
        if code in ("Throttling", "ThrottlingException", "TooManyRequestsException"):
            kind = FailureKind.RATE_LIMITED
        elif code == "ModelTimeoutException":
            kind = FailureKind.TRANSPORT_TIMEOUT
        elif code == "ServiceQuotaExceededException":
            kind = FailureKind.QUOTA_EXHAUSTED
        elif code == "ModelErrorException":
            original_status = exc.response.get("originalStatusCode")
            if original_status in (408, 409, 429):
                kind = FailureKind.RATE_LIMITED
            elif isinstance(original_status, int) and original_status >= 500:
                kind = FailureKind.SERVER_ERROR
            else:
                kind = FailureKind.REQUEST_REJECTED
        elif code == "ModelNotReadyException" or (isinstance(status, int) and status >= 500):
            kind = FailureKind.SERVER_ERROR
        elif code == "ValidationException" and status == 400:
            message = exc.response.get("Error", {}).get("Message", "")
            lowered = message.lower() if isinstance(message, str) else ""
            kind = (
                FailureKind.SCHEMA_REJECTED
                if "schema" in lowered or "output_config.format" in lowered
                else FailureKind.REQUEST_REJECTED
            )
        else:
            kind = FailureKind.REQUEST_REJECTED
        retry_after = retry_after_seconds(metadata.get("HTTPHeaders", {}))
    elif isinstance(exc, (aws_errors.ConnectTimeoutError, aws_errors.ReadTimeoutError)):
        kind = FailureKind.TRANSPORT_TIMEOUT
    elif isinstance(exc, (
        aws_errors.EndpointConnectionError,
        aws_errors.ConnectionClosedError,
        aws_errors.ResponseStreamingError,
        aws_errors.IncompleteReadError,
        aws_errors.CredentialRetrievalError,
    )):
        kind = FailureKind.TRANSPORT_CONNECTION
    elif isinstance(exc, (
        BedrockBoundaryError,
        aws_errors.NoCredentialsError,
        aws_errors.PartialCredentialsError,
        aws_errors.ParamValidationError,
    )):
        kind = FailureKind.REQUEST_REJECTED
    else:
        kind = FailureKind.ADAPTER_ERROR
    return AttemptFailure(
        kind,
        "고객 계정 Bedrock 호출을 완료하지 못했습니다",
        error_class=name,
        status_code=status,
        retry_after_seconds=retry_after,
        phase=phase,
    )


class BedrockModelClient:
    """명시한 owner와 호출자 소유의 인가 콜백을 매 시도 확인한다. DB 설정을 읽지 않는다."""

    provider = "bedrock"

    def __init__(
        self,
        *,
        owner: TenantAccess | None,
        consent_check: Callable[[], bool],
        region: str,
        model_id: str,
        policy: RetryPolicy,
        max_tokens: int,
        connect_timeout: float,
        read_timeout: float,
        runtime_factory: Callable[..., Any] = tenant_bedrock_runtime_client,
    ) -> None:
        if not region.strip() or not model_id.strip():
            raise ValueError("region과 model_id는 비어 있을 수 없습니다")
        if max_tokens < 1 or connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("토큰·제한시간은 0보다 커야 합니다")
        self._owner = owner
        self._consent_check = consent_check
        self._region = region
        self._model_id = model_id
        self._policy = policy
        self._max_tokens = max_tokens
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._runtime_factory = runtime_factory

    def _request_body(
        self, payload: dict[str, Any], response_model: type[StructuredOutputT]
    ) -> dict[str, Any]:
        return {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._max_tokens,
            "system": payload["system_prompt"],
            "messages": [{"role": "user", "content": payload["user_json"]}],
            "output_config": {
                "format": {"type": "json_schema", "schema": _schema_for(response_model)}
            },
        }

    def _classify_body(
        self, message: dict[str, Any], response_model: type[StructuredOutputT]
    ) -> AttemptOutcome[StructuredOutputT]:
        outcome = _classify_message(message, response_model)
        if isinstance(outcome, AttemptSuccess) and message.get("stop_reason") != "end_turn":
            return AttemptFailure(
                FailureKind.RESPONSE_SHAPE,
                "완료된 텍스트 응답이 아닙니다",
                usage=outcome.usage,
                returned_model=outcome.returned_model,
                returned_service_tier=outcome.returned_service_tier,
            )
        return outcome

    def complete(
        self, request: AIModelRequest, response_model: type[StructuredOutputT]
    ) -> AIModelResponse[StructuredOutputT]:
        payload = build_outbound_payload(request)

        def attempt() -> AttemptOutcome[StructuredOutputT]:
            if self._owner is None or not self._consent_check():
                return AttemptFailure(
                    FailureKind.REQUEST_REJECTED,
                    "고객 역할과 호출자의 인가가 필요합니다",
                    phase="consent",
                )
            body = self._request_body(payload, response_model)
            # 정적 임시 자격을 받으므로 InvokeModel 서명 도중 STS 갱신이 섞이지 않는다.
            try:
                runtime = self._runtime_factory(
                    self._owner,
                    region=self._region,
                    connect_timeout=self._connect_timeout,
                    read_timeout=self._read_timeout,
                )
            except Exception as exc:
                return _failure(exc, phase="credentials")
            try:
                try:
                    response = runtime.invoke_model(
                        modelId=self._model_id,
                        contentType="application/json",
                        accept="application/json",
                        body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    )
                    stream = response["body"]
                    try:
                        raw = stream.read()
                    finally:
                        stream.close()
                except Exception as exc:
                    return _failure(exc, phase="transport")
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeError):
                    message = None
                if not isinstance(message, dict):
                    return AttemptFailure(
                        FailureKind.RESPONSE_SHAPE,
                        "Bedrock 응답 본문이 JSON 객체가 아닙니다",
                        usage=_token_usage(None),
                    )
                return self._classify_body(message, response_model)
            finally:
                runtime.close()

        return ModelCallEngine(
            provider=self.provider, model=self._model_id, policy=self._policy,
        ).run(attempt)


def build_bedrock_model_client(
    *,
    owner: TenantAccess | None,
    consent_check: Callable[[], bool],
    region: str,
    model_id: str,
    max_tokens: int = 8192,
    connect_timeout: float = 5.0,
    read_timeout: float = 120.0,
    max_attempts: int = 3,
    runtime_factory: Callable[..., Any] = tenant_bedrock_runtime_client,
) -> BedrockModelClient:
    """앱 설정이 필요 없다. 8192는 추론+응답 초기 한도이며 품질 실측값이 아니다."""
    return BedrockModelClient(
        owner=owner,
        consent_check=consent_check,
        region=region,
        model_id=model_id,
        policy=bedrock_retry_policy(
            connect_timeout=connect_timeout, read_timeout=read_timeout, max_attempts=max_attempts,
        ),
        max_tokens=max_tokens,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        runtime_factory=runtime_factory,
    )
