"""도메인별 계측에서 재사용하는 모델 경계 사용량·실패 단계 기록."""

from typing import Any, Optional

from ai.model_client import AIModelError, AIModelRequest, AIModelResponse


class UsageRecordingClient:
    """실제 경계 구현에 위임하면서 호출 메타만 모은다.

    경계 예외는 그대로 올려 보낸다 — 그래프가 FAILED로 접는 것이 정상 경로다. 여기서는
    무엇이 실패했는지 사람이 볼 수 있게 예외 **클래스 이름만** 남긴다(ADR-0005).
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_prompt_tokens = 0
        self.calls = 0
        self.errors: list[str] = []
        # 직전 reset 이후 이 회차에서 경계가 세운 예외 클래스. 그래프는 이것을 삼켜
        # FAILED로 접기 때문에, 여기서 잡아 두지 않으면 "모델이 계약을 어겼다"와
        # "왕복이 못 섰다"가 결과에서 같은 값이 된다
        self.last_error: Optional[str] = None
        self.last_error_phase: Optional[str] = None
        # 응답이 밝힌 스냅샷 ID(별칭이 아니라 날짜가 붙은 실제 버전). 별칭으로 부른
        # 모델이 실제로 무엇이었는지 없이는 나중에 같은 표를 재현할 수 없다
        self.model_snapshots: set[str] = set()

    def reset(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_prompt_tokens = 0
        self.calls = 0
        self.last_error: Optional[str] = None
        self.last_error_phase: Optional[str] = None

    def complete(self, request: AIModelRequest, response_model) -> AIModelResponse:
        self.calls += 1
        try:
            response = self._inner.complete(request, response_model)
        except AIModelError as exc:
            self.errors.append(type(exc).__name__)
            self.last_error = type(exc).__name__
            self.last_error_phase = exc.phase
            # 응답을 받은 실패(refusal·응답 계약 위반)는 토큰이 이미 발생했다 —
            # 예외에 실려 온 usage를 집계에 보존한다. 버리면 비용이 적게 잡힌다
            if exc.usage is not None:
                self.prompt_tokens += exc.usage.prompt_tokens
                self.completion_tokens += exc.usage.completion_tokens
                self.cached_prompt_tokens += exc.usage.cached_prompt_tokens
            raise
        self.prompt_tokens += response.usage.prompt_tokens
        self.completion_tokens += response.usage.completion_tokens
        self.cached_prompt_tokens += response.usage.cached_prompt_tokens
        self.model_snapshots.add(response.model)
        return response
