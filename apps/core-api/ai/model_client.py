# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# GPT-4o 호출 경계입니다. (Issue #115)
# LangGraph 노드와 도메인 코드는 이 경계만 보고, OpenAI SDK 타입·자격증명·SDK 예외는
# 경계 밖으로 나가지 않습니다 (ADR-0005 설계 원칙 3).
#
# 계약 원칙
#   - 동기 호출. 저장소의 다른 외부 호출 경계(Repository·services/aws)와 같은 방식이다.
#   - 실패는 이 모듈의 예외로만 나간다. SDK 예외를 그대로 전파하지 않는다.
#   - 모델로 나가는 값은 build_outbound_payload()를 반드시 거친다. 구현체가 이 함수를
#     우회하면 마스킹이 적용되지 않으므로, 전송 페이로드는 여기서만 만든다.
#   - Prompt 전문과 모델 원문 응답은 저장·로그 어디에도 남기지 않는다(ADR-0005
#     미보존 대상). 남기는 것은 토큰 사용량 같은 호출 메타뿐이며 단가·비용은 아니다.
# ==============================================================================

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Optional, Protocol, TypeVar

from pydantic import BaseModel

StructuredOutputT = TypeVar("StructuredOutputT", bound=BaseModel)


# ------------------------------------------------------------------------------
# 경계 예외 — 호출부는 SDK 예외가 아니라 이 4종만 다룬다
# ------------------------------------------------------------------------------


class AIModelError(Exception):
    """모델 호출 실패의 최상위 타입."""


class AIModelTimeoutError(AIModelError):
    """제한시간 초과. 재시도 상한을 소진한 뒤에 나온다."""


class AIModelUnavailableError(AIModelError):
    """일시적 실패(연결 오류·408·409·429·5xx). 재시도 상한을 소진한 뒤에 나온다."""


class AIModelRejectedError(AIModelError):
    """호출이 거절됨(인증·요청 오류·모델 refusal). 재시도하지 않는다."""


class AIModelContractError(AIModelError):
    """계약 위반. 재시도하지 않는다.

    양방향이다 — 요청 페이로드가 JSON으로 직렬화되지 않는 경우와, 응답이 요구한
    구조화 출력으로 파싱되지 않는 경우.
    """


# ------------------------------------------------------------------------------
# 요청·응답
# ------------------------------------------------------------------------------


@dataclass(frozen=True)
class AIModelRequest:
    """모델 호출 1회의 입력. user_payload는 JSON 직렬화 가능한 값만 담는다."""

    system_prompt: str
    user_payload: Mapping[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    """호출 메타. 단가·계산된 비용은 담지 않는다(ADR-0005 미보존 대상)."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class AIModelResponse(Generic[StructuredOutputT]):
    """구조화 출력 1건 + 호출 메타. output 타입은 호출부가 넘긴 모델과 같다."""

    output: StructuredOutputT
    usage: TokenUsage
    model: str


class AIModelClient(Protocol):
    """모델 호출 경계. 구현체는 이 시그니처만 노출한다."""

    def complete(
        self,
        request: AIModelRequest,
        response_model: type[StructuredOutputT],
    ) -> AIModelResponse[StructuredOutputT]:
        ...


# ------------------------------------------------------------------------------
# 전송 직전 마스킹 (#115 확정 범위)
# ------------------------------------------------------------------------------

_MASK = "[REDACTED]"

# 가리는 것은 자격증명 형태 문자열뿐이다. ARN·리소스 ID·계정 ID·리전은 대상이 아니다 —
# 가리면 모델이 조치 대상을 지목할 수 없고(RunbookCandidateDraft.target_arn), 이미
# GET /api/v1/assets로 대시보드에 나가는 값이라 감출 대상도 아니다.
_PREFIXED_SECRETS: tuple[re.Pattern[str], ...] = (
    # AWS 액세스 키 ID(장기·임시)
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # OpenAI 형태 키. 꼬리에 하이픈·밑줄을 허용하지 않아 sk-로 시작하는 사람 이름표
    # (예: sk-prod-cluster)와 구분된다.
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}\b"),
    # GitHub 토큰(ghp_·gho_·ghu_·ghs_·ghr_)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)

# `Bearer <토큰>` 형태 — 값이 8자 미만이면 영어 일반 명사("bearer of ...")로 본다
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")

# Authorization 헤더는 값 전체(스킴 포함)를 가린다 — 인증 방식을 열거하면 목록에 없는
# 방식이 올 때마다 구멍이 생기고, 라벨 규칙만으로는 스킴 한 단어만 가려 자격증명 본체가
# 남는다(예: "Basic dXNlcjpwYXNz"). 값은 따옴표·줄바꿈에서 끊는다 — 줄 끝까지 먹으면
# 단일 라인 JSON 근거(CloudTrail·VPC flow log)의 뒤따르는 판단 재료까지 사라진다.
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)\b(authorization\b[\"']?\s*[=:]\s*[\"']?)([^\r\n\"']+)"
)

# 비밀값 라벨 목록 — 문자열 안 표기(`password: x`·`"password": "x"`)와 dict 키
# (태그처럼 키가 자유 문자열인 자리) 판정이 같은 목록을 쓴다.
_SECRET_LABELS = (
    r"aws_secret_access_key|aws_session_token|secret[_-]?key"
    r"|api[_-]?key|password|passwd|token|authorization"
)
# 라벨이 붙은 비밀값. 라벨은 남기고 값만 가려서 무엇이 지워졌는지 읽을 수 있게 한다.
# 라벨 앞 [\w-]* 는 db_password 같은 접두 변형용, 따옴표 허용은 JSON 표기용이다.
# 값은 공백·따옴표에서 끊는다 — 닫는 따옴표까지 먹으면 JSON 근거의 나머지 항목이
# 마스킹 안으로 딸려 들어가 판단 재료가 사라진다.
_LABELED_SECRET = re.compile(
    r"(?i)\b([\w-]*(?:" + _SECRET_LABELS + r"))([\"']?\s*[=:]\s*[\"']?)([^\s\"']+)"
)
# 비밀 라벨로 끝나는 dict 키 — 그 키의 문자열 값은 통째로 가린다
_SECRET_KEY_NAME = re.compile(r"(?i)[\w-]*(?:" + _SECRET_LABELS + r")")


def _mask_text(text: str) -> str:
    # Authorization 헤더 → Bearer → 라벨 순서다. 헤더 규칙이 줄 전체를 먼저 가려야
    # 뒤 규칙들이 스킴 한 단어만 가리고 자격증명을 남기는 일이 없다
    masked = _AUTHORIZATION_HEADER.sub(r"\1" + _MASK, text)
    masked = _BEARER_TOKEN.sub(_MASK, masked)
    masked = _LABELED_SECRET.sub(r"\1\2" + _MASK, masked)
    for pattern in _PREFIXED_SECRETS:
        masked = pattern.sub(_MASK, masked)
    return masked


def mask_outbound(value: Any) -> Any:
    """모델로 나가는 값에서 자격증명 형태 문자열을 가린다.

    dict·list를 재귀로 훑는다. 키 이름은 바꾸지 않되, 키가 비밀 라벨로 끝나면
    (예: 태그 {"db_password": ...}) 그 문자열 값을 통째로 가린다. 라벨 없는
    40자짜리 시크릿 문자열은 대상이 아니다 — 해시·base64 데이터와 구분되지 않아
    오탐으로 판단 근거를 지운다.
    """
    if isinstance(value, str):
        return _mask_text(value)
    if isinstance(value, Mapping):
        return {
            key: (
                _MASK
                if isinstance(key, str)
                and isinstance(item, str)
                and _SECRET_KEY_NAME.fullmatch(key)
                else mask_outbound(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask_outbound(item) for item in value]
    return value


def build_outbound_payload(request: AIModelRequest) -> dict[str, Any]:
    """실제로 모델에 보낼 값. 이 함수를 지나지 않은 값은 전송하지 않는다.

    system_prompt도 마스킹을 지난다 — 프롬프트 본문에 `token:` 같은 라벨 표기를 쓰면
    지침 자체가 조용히 잘리므로, 프롬프트에는 비밀값 라벨 표기를 쓰지 않는다.
    """
    masked = mask_outbound(dict(request.user_payload))
    # 직렬화 불가 값은 여기서 계약 위반으로 세운다. 이 검사가 없으면 호출부가 python
    # 모드 model_dump()를 넘겼을 때 TypeError가 경계 밖으로 그대로 나간다 —
    # AssetItem.collected_at의 UtcDateTime은 when_used="json"이라 python 모드에서
    # datetime 객체로 남는다.
    # raise는 except 밖에서 한다 — 안에서 던지면 from None으로도 __context__에 원본이
    # 남는다. 경계 예외에 다른 예외를 매달지 않는 규칙을 SDK 예외와 같게 지킨다.
    user_json = ""
    serialize_error: Optional[str] = None
    try:
        user_json = json.dumps(masked, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:
        serialize_error = str(exc)
    if serialize_error is not None:
        raise AIModelContractError(
            f"user_payload를 JSON으로 직렬화하지 못했습니다 ({serialize_error}) — "
            'model_dump(mode="json")으로 넘기세요'
        )
    return {
        "system_prompt": _mask_text(request.system_prompt),
        "user_payload": masked,
        # 구현체가 다시 직렬화하지 않도록 여기서 만든 것을 함께 넘긴다
        "user_json": user_json,
    }


# ------------------------------------------------------------------------------
# 테스트 주입용 구현 (ADR-0005 — 실제 API 없이 두 경로 검증)
# ------------------------------------------------------------------------------


class FakeAIModelClient:
    """미리 준비한 출력을 순서대로 돌려주고, 전송됐을 페이로드를 sent에 기록한다.

    마스킹을 실제 구현과 같은 지점에서 적용한다 — 주입 구현이 마스킹을 우회하면
    주입 테스트가 검증하는 경로가 실제 경로와 달라진다.
    """

    def __init__(
        self,
        outputs: Sequence[BaseModel],
        *,
        model: str = "fake-model",
    ) -> None:
        self._outputs = list(outputs)
        self._model = model
        self.sent: list[dict[str, Any]] = []

    def complete(
        self,
        request: AIModelRequest,
        response_model: type[StructuredOutputT],
    ) -> AIModelResponse[StructuredOutputT]:
        self.sent.append(build_outbound_payload(request))
        if not self._outputs:
            raise AIModelUnavailableError("준비된 응답이 남아 있지 않습니다")
        output = self._outputs.pop(0)
        if not isinstance(output, response_model):
            raise AIModelContractError(
                f"{response_model.__name__}이 아닌 출력이 준비돼 있습니다"
            )
        return AIModelResponse(
            output=output,
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            model=self._model,
        )
