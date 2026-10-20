# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# WebSocket(/api/v1/ws) 공통 이벤트 봉투입니다. (확정 설계 4.5)
#
# 계약 원칙
#   - PostgreSQL commit 이후에만 전송한다 — WS는 상태 원본이 아니라 전달 채널이다.
#   - 보안·FinOps Incident와 Action Execution 상태를 전달한다. 평시 자산 상태는 REST 조회.
#   - Incident 이벤트의 data는 incident_id만 담는다(상세는 REST 재조회). INCIDENT_CREATED만
#     category를 함께 싣는다 — 받자마자 트랙별 알림을 띄우므로 재조회를 기다리지 않는다.
#     Execution 이벤트의 data는 incident_id·execution_id·status·updated_at.
#   - 같은 event_id 재수신은 중복 반영하지 않는다(수신 측 멱등 키).
#   - 과거 이벤트 재생은 보장하지 않는다 — 재연결 시 REST로 상태를 복구한다.
#
# 접근 보호 (#492 PR 2) — 연결은 세션 쿠키(__Host-vg_session)로 승인한다. 연결도 세션 확인도
# 유휴 시간을 늘리지 않는다(연장은 POST /api/v1/auth/activity 뿐).
#   - 연결 거절: 허용 목록 밖 Origin · 세션 없음 · 무효 세션은 accept 전에 close 1008 — 브라우저에는
#     Handshake HTTP 403 으로 보인다. 사유는 구분해 알리지 않는다.
#   - 연결 중 세션 무효: 서버가 WS_SESSION_CHECK_SECONDS(기본 30초)마다, 그리고 세션의 유휴 ·
#     최대 수명 만료 시각에 다시 확인한다. 로그아웃 · 재로그인 회수 · 사용자 비활성 · 만료면
#     close 4401(WS_CLOSE_AUTH_REQUIRED), reason "AUTH_REQUIRED". 재연결하지 말고 로그인으로 보낸다.
#     반영 지연의 상한은 확인 주기 1회(+ 확인 조회 시간)다.
#   - 세션 확인 자체가 실패(DB 오류 등): close 1011. 세션 무효가 아니다 — 재연결한다.
#   - 그 밖의 종료(1000 · 1001 · 1006 등): 재연결 후 REST로 상태를 복구한다.
#   - close reason 에는 세션 · 사용자 정보를 싣지 않는다.
# ==============================================================================

from __future__ import annotations

from enum import Enum, unique
from typing import Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .actions import ExecutionStatus
from .assets import UtcDateTime
from .incidents import IncidentCategory

#: 연결 중 세션이 무효가 됐다(로그아웃 · 회수 · 사용자 비활성 · 만료). 4000번대는 앱 정의 범위다.
WS_CLOSE_AUTH_REQUIRED = 4401


@unique
class WsEventType(str, Enum):
    INCIDENT_CREATED = "INCIDENT_CREATED"
    INCIDENT_UPDATED = "INCIDENT_UPDATED"
    EXECUTION_UPDATED = "EXECUTION_UPDATED"


class IncidentEventData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)


class IncidentCreatedData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    category: IncidentCategory


class ExecutionEventData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    status: ExecutionStatus
    updated_at: UtcDateTime


# 이벤트 종류마다 data 모델이 하나다 — 봉투 검증이 이 표로만 고른다
_DATA_MODELS = {
    WsEventType.INCIDENT_CREATED: IncidentCreatedData,
    WsEventType.INCIDENT_UPDATED: IncidentEventData,
    WsEventType.EXECUTION_UPDATED: ExecutionEventData,
}


class WsEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    event_type: WsEventType
    occurred_at: UtcDateTime
    data: Union[IncidentCreatedData, IncidentEventData, ExecutionEventData]

    @model_validator(mode="before")
    @classmethod
    def _bind_data_to_event_type(cls, data):
        # smart-union 오매칭 방지: event_type이 지정한 data 모델로만 검증한다
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            try:
                event_type = WsEventType(data.get("event_type"))
            except (ValueError, TypeError):
                return data  # event_type 오류는 필드 검증이 보고한다
            data = dict(data)
            data["data"] = _DATA_MODELS[event_type].model_validate(data["data"])
        return data

    @model_validator(mode="after")
    def _enforce_data_shape(self):
        expected = _DATA_MODELS[self.event_type]
        if not isinstance(self.data, expected):
            raise ValueError(
                f"{self.event_type.value} 이벤트의 data는 {expected.__name__}이어야 합니다"
            )
        return self
