"""도메인별 계측에서 재사용하는 모델 경계 사용량·실패 단계 기록."""

from dataclasses import asdict, dataclass
from typing import Any, Optional

from ai.model_client import (
    AIModelError,
    AIModelRequest,
    AIModelResponse,
    CallTrace,
    TokenUsage,
)
from ai.model_retry import USAGE_PARTIAL_ATTEMPTS

_TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "cached_prompt_tokens")


@dataclass(frozen=True)
class CallRecord:
    """논리 호출(complete() 1회) 1건의 기록. 원문 출력·예외 메시지·요청은 담지 않는다.

    토큰은 제공자가 실제로 보고한 값뿐이다. TokenUsage는 제공자가 주지 않은 필드를 0으로 채우고
    그 이름을 unreported에 적으므로, unreported에 든 필드는 0이 아니라 None(모름)으로 옮긴다.
    usage_status: "complete"(세 필드 모두 보고, 모든 시도의 사용량 확인) · "partial"(일부 필드
    미보고이거나 사용량을 모르는 시도가 섞여 알려진 값이 소계·하한) · "unknown"(사용량 객체 없음).
    usage_known은 status가 complete일 때만 True다.
    attempts는 SDK 호출(물리 시도) 수이며 trace가 있을 때만 관측된다. attempt_details는 시도별
    결과·오류 클래스·HTTP 상태·대기·사용량이다(메시지·응답·요청 없음). trace가 없으면 None이다.
    """

    index: int  # 이 클라이언트에서 1부터 센 논리 호출 순번(reset과 무관)
    run_call_index: int  # 직전 reset 이후 순번
    response_model: str  # 요청한 구조화 출력 모델 이름 = 호출 단계(step)
    outcome: str  # "ok" 또는 "error"
    error_class: Optional[str]
    error_phase: Optional[str]
    error_kind: Optional[str]
    returned_model: Optional[str]
    usage_known: bool
    usage_status: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    cached_prompt_tokens: Optional[int]
    unreported_usage_fields: tuple[str, ...]
    attempts: Optional[int]
    attempts_without_usage: Optional[int]
    attempt_details: Optional[tuple[dict[str, Any], ...]] = None


def _usage_view(usage: Optional[TokenUsage]) -> tuple[str, dict[str, Optional[int]]]:
    """(상태, 필드별 보고값). 미보고 필드는 0이 아니라 None이다."""
    if usage is None:
        return "unknown", {name: None for name in _TOKEN_FIELDS}
    missing = set(usage.unreported)
    values = {name: None if name in missing else getattr(usage, name) for name in _TOKEN_FIELDS}
    complete = not (missing & set(_TOKEN_FIELDS)) and USAGE_PARTIAL_ATTEMPTS not in missing
    return ("complete" if complete else "partial"), values


def _attempt_details(trace: Optional[CallTrace]) -> Optional[tuple[dict[str, Any], ...]]:
    if trace is None:
        return None
    details = []
    for item in trace.attempts:
        status, values = _usage_view(item.usage)
        details.append(
            {
                "attempt": item.attempt,
                "outcome": item.outcome,
                "error_class": item.error_class,
                "status_code": item.status_code,
                "elapsed_seconds": item.elapsed_seconds,
                "retry_delay_seconds": item.retry_delay_seconds,
                "server_directed": item.server_directed,
                "usage_status": status,
                **values,
                "unreported_usage_fields": list(item.usage.unreported) if item.usage else [],
            }
        )
    return tuple(details)


def _build_record(
    index: int,
    run_index: int,
    step: str,
    usage: Optional[TokenUsage],
    trace: Optional[CallTrace],
    returned_model: Optional[str],
    exc: Optional[AIModelError],
) -> CallRecord:
    status, values = _usage_view(usage)
    return CallRecord(
        index=index,
        run_call_index=run_index,
        response_model=step,
        outcome="ok" if exc is None else "error",
        error_class=None if exc is None else type(exc).__name__,
        error_phase=None if exc is None else exc.phase,
        error_kind=None if exc is None or exc.kind is None else exc.kind.value,
        returned_model=returned_model,
        usage_known=status == "complete",
        usage_status=status,
        unreported_usage_fields=usage.unreported if usage else (),
        attempts=None if trace is None else trace.attempt_count,
        attempts_without_usage=(
            None if trace is None else sum(1 for item in trace.attempts if item.usage is None)
        ),
        attempt_details=_attempt_details(trace),
        **values,
    )


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
        # reset과 무관하게 누적되는 논리 호출 기록. 회차 몫은 run_records()로 읽는다
        self.records: list[CallRecord] = []
        self._run_start = 0

    @property
    def all_call_records(self) -> list[CallRecord]:
        """reset()에도 지워지지 않는 전체 논리 호출 기록(읽기용 사본).

        호출자 사용법: 회차마다 reset() → 그래프 실행 → run_records()/run_records_json()으로 그
        회차 몫을 가져가고, 마지막에 all_call_records(_json)로 전체를 확인한다. calls·토큰
        속성은 기존대로 reset마다 0이 되므로 합계는 이 기록이나 호출자 누적으로 낸다.
        """
        return list(self.records)

    def all_call_records_json(self) -> list[dict[str, Any]]:
        return [asdict(record) for record in self.records]

    def run_records(self) -> list[CallRecord]:
        """직전 reset 이후의 논리 호출 기록."""
        return self.records[self._run_start :]

    def run_records_json(self) -> list[dict[str, Any]]:
        return [asdict(record) for record in self.run_records()]

    @property
    def unknown_usage_calls(self) -> int:
        """사용량 객체가 없는 논리 호출 수(전체 누적)."""
        return sum(1 for record in self.records if record.usage_status == "unknown")

    @property
    def partial_usage_calls(self) -> int:
        """사용량이 일부 필드·시도만 확인된 논리 호출 수(전체 누적)."""
        return sum(1 for record in self.records if record.usage_status == "partial")

    def usage_totals(self) -> dict[str, dict[str, int]]:
        """필드별 확인된 소계와, 그 필드를 확인하지 못한 호출 수.

        소계는 보고된 값만 더한 하한이다. calls_not_fully_known가 0이 아니면 실제 값은 더 클 수
        있다. 위 calls·prompt_tokens 속성은 미보고 필드를 0으로 채운 기존 합계라 이 값과 다르다.
        """
        totals: dict[str, dict[str, int]] = {}
        for name in _TOKEN_FIELDS:
            values = [getattr(record, name) for record in self.records]
            totals[name] = {
                "known_subtotal": sum(value for value in values if value is not None),
                "calls_missing_field": sum(1 for value in values if value is None),
                "calls_not_fully_known": sum(
                    1
                    for record in self.records
                    if getattr(record, name) is None or record.usage_status != "complete"
                ),
            }
        return totals

    @property
    def physical_attempts(self) -> Optional[int]:
        """trace로 관측된 SDK 시도 합. trace 없는 호출이 있으면 총 시도 수를 모르므로 None이다."""
        if any(record.attempts is None for record in self.records):
            return None
        return sum(record.attempts for record in self.records)

    def reset(self) -> None:
        self._run_start = len(self.records)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_prompt_tokens = 0
        self.calls = 0
        self.last_error: Optional[str] = None
        self.last_error_phase: Optional[str] = None

    def complete(self, request: AIModelRequest, response_model) -> AIModelResponse:
        self.calls += 1
        step = getattr(response_model, "__name__", str(response_model))
        index = len(self.records) + 1
        run_index = index - self._run_start
        try:
            response = self._inner.complete(request, response_model)
        except AIModelError as exc:
            self.records.append(
                _build_record(
                    index, run_index, step, exc.usage, exc.trace,
                    exc.trace.returned_model if exc.trace else None, exc,
                )
            )
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
        self.records.append(
            _build_record(
                index, run_index, step, response.usage, response.trace, response.model, None
            )
        )
        self.prompt_tokens += response.usage.prompt_tokens
        self.completion_tokens += response.usage.completion_tokens
        self.cached_prompt_tokens += response.usage.cached_prompt_tokens
        self.model_snapshots.add(response.model)
        return response
