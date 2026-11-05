# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# GET /api/v1/threat-events — 위협 이벤트를 한 건씩 돌려주는 목록(#572 10번) 외부 DTO입니다.
# 고객사 대시보드의 「위협 이벤트」 칸과 고객사 계정의 「로그」 화면(/logs)이 부릅니다.
#
# 원천은 DB `threat_events`(정규화된 위협 이벤트, events.NormalizedThreatEvent 와 대응)다. 같은 이벤트를
# 시간 칸별 건수로 센 것이 메트릭 축 6(`GET /metrics/timeseries` 의 `threat_events`)이고, 이 목록은 같은
# 범위 · 관제 리전 · 기간 조건으로 거른다 — 그래프와 목록의 건수가 같은 근거로 맞는다.
#
# 계약 원칙
#   - snake_case, UTC ISO 8601("Z") 문자열, extra 금지.
#   - 항목은 `event_type` 으로 갈리는 닫힌 합이다. `payload` 는 정규화 이벤트의 유형별 모델 그대로다
#     (SSH = source_ip · failed_attempt_count · window_seconds, 개방 포트 = protocol · from_port · to_port · source_cidr).
#
# 경로 (/api/v1)
#   GET /threat-events?days=1|7|30 → ThreatEventsResponse. days 는 셋 중 하나만 받는다(그 밖은 422).
#     기간: occurred_at ∈ [window_start, window_end], window_end = 조회 시각, window_start = 그 days 일 전.
#           미래 시각(원천 시계 어긋남)의 이벤트는 빠진다.
#     순서: occurred_at 내림차순, 같은 시각은 threat_event_id 내림차순. 최대 THREAT_EVENTS_LIMIT 건.
#     `total` 은 기간 안의 실제 건수다 — `items` 가 상한에 잘렸으면 `items` 길이보다 크다. 커서는 없다.
#   범위: 인시던트 목록 · 메트릭과 같은 고객사 범위(tenant_scope_ids). 고객사 계정은 자기 고객사,
#     담당 직원은 배정 고객사, 중앙 관리계정은 전체(소속 없는 이벤트 포함)다. DB 만 읽으므로 정지된
#     고객사도 조회된다.
# ==============================================================================

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..events import OpenIpThreatPayload, SshBruteForceThreatPayload, ThreatEventType
from .assets import UtcDateTime

#: 한 번에 돌려주는 최대 건수. 넘치면 `total` 이 실제 건수를 알린다.
THREAT_EVENTS_LIMIT = 500

#: 받는 기간(일). 하루 · 일주일 · 한 달.
THREAT_EVENT_RANGE_DAYS: tuple[int, ...] = (1, 7, 30)


class _ThreatEventItemBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threat_event_id: str = Field(min_length=1)
    target_arn: str = Field(min_length=1)
    occurred_at: UtcDateTime


class OpenIpThreatEventItem(_ThreatEventItemBase):
    event_type: Literal[ThreatEventType.OPEN_IP]
    payload: OpenIpThreatPayload


class SshBruteForceThreatEventItem(_ThreatEventItemBase):
    event_type: Literal[ThreatEventType.SSH_BRUTE_FORCE]
    payload: SshBruteForceThreatPayload


ThreatEventItem = Annotated[
    Union[OpenIpThreatEventItem, SshBruteForceThreatEventItem],
    Field(discriminator="event_type"),
]


class ThreatEventsResponse(BaseModel):
    """기간 안 위협 이벤트를 최근 것부터. 머리말 「경로」."""

    model_config = ConfigDict(extra="forbid")

    window_start: UtcDateTime
    window_end: UtcDateTime
    total: int = Field(ge=0)
    items: list[ThreatEventItem] = Field(max_length=THREAT_EVENTS_LIMIT)

    @model_validator(mode="after")
    def _consistent(self):
        if self.window_start >= self.window_end:
            raise ValueError("window_start 는 window_end 보다 앞서야 합니다")
        if self.total < len(self.items):
            raise ValueError("total 은 실은 항목 수보다 작을 수 없습니다")
        if self.total > len(self.items) and len(self.items) < THREAT_EVENTS_LIMIT:
            raise ValueError("상한에 닿지 않았으면 기간 안 이벤트를 모두 실어야 합니다")
        for item in self.items:
            if not self.window_start <= item.occurred_at <= self.window_end:
                raise ValueError("항목의 occurred_at 은 기간 안이어야 합니다")
        keys = [(i.occurred_at, i.threat_event_id) for i in self.items]
        if keys != sorted(keys, reverse=True):
            raise ValueError("항목은 occurred_at · threat_event_id 내림차순이어야 합니다")
        return self
