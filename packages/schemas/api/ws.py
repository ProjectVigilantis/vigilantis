# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# WebSocket(/api/v1/ws) 공통 이벤트 봉투입니다. (확정 설계 4.5)
#
# 계약 원칙
#   - PostgreSQL commit 이후에만 전송한다 — WS는 상태 원본이 아니라 전달 채널이다.
#   - 보안 Incident와 Action Execution 상태만 전달한다. 평시 자산 상태는 REST 조회.
#   - Incident 이벤트의 data는 incident_id만 담는다(상세는 REST 재조회).
#     Execution 이벤트의 data는 incident_id·execution_id·status·updated_at.
#   - 같은 event_id 재수신은 중복 반영하지 않는다(수신 측 멱등 키).
#   - 과거 이벤트 재생은 보장하지 않는다 — 재연결 시 REST로 상태를 복구한다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .actions import ExecutionStatus
from .assets import UtcDateTime


@unique
class WsEventType(str, Enum):
    INCIDENT_CREATED = "INCIDENT_CREATED"
    INCIDENT_UPDATED = "INCIDENT_UPDATED"
    EXECUTION_UPDATED = "EXECUTION_UPDATED"


class IncidentEventData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)


class ExecutionEventData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    status: ExecutionStatus
    updated_at: UtcDateTime


class WsEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    event_type: WsEventType
    occurred_at: UtcDateTime
    data: Union[IncidentEventData, ExecutionEventData]

    @model_validator(mode="before")
    @classmethod
    def _bind_data_to_event_type(cls, data):
        # smart-union 오매칭 방지: event_type이 지정한 data 모델로만 검증한다
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            try:
                event_type = WsEventType(data.get("event_type"))
            except (ValueError, TypeError):
                return data  # event_type 오류는 필드 검증이 보고한다
            model = (
                ExecutionEventData
                if event_type is WsEventType.EXECUTION_UPDATED
                else IncidentEventData
            )
            data = dict(data)
            data["data"] = model.model_validate(data["data"])
        return data

    @model_validator(mode="after")
    def _enforce_data_shape(self):
        expected = (
            ExecutionEventData
            if self.event_type is WsEventType.EXECUTION_UPDATED
            else IncidentEventData
        )
        if not isinstance(self.data, expected):
            raise ValueError(
                f"{self.event_type.value} 이벤트의 data는 {expected.__name__}이어야 합니다"
            )
        return self
