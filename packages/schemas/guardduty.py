# GuardDuty 탐지가 적은 활동 사실의 공유 계약(#598).
# events.py(탐지 payload)와 api/incidents.py(추천 항목)가 함께 쓴다. events.py 가 api/incidents.py 를
# 이미 import 하므로 이 타입을 events.py 에 두면 순환이 된다 — 그래서 프로젝트 모듈을 import 하지 않는 이 모듈에 둔다(표준 라이브러리 · pydantic 만 쓴다).

from __future__ import annotations

from enum import Enum, unique
from ipaddress import IPv4Address, IPv6Address
from typing import Generic, Optional, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: UNRECOGNIZED 로 남기는 원문 문자열의 최대 길이
GUARDDUTY_SOURCE_VALUE_MAX = 64


@unique
class GuardDutyFactStatus(str, Enum):
    """탐지 원문의 한 칸을 읽은 결과. 값이 없는 세 경우(ABSENT·UNRECOGNIZED·INVALID)를 서로 섞지 않는다."""

    RECORDED = "RECORDED"            # 원문 값을 읽어 value 에 담았다
    ABSENT = "ABSENT"                # 원문에 그 키가 없다(생략)
    UNRECOGNIZED = "UNRECOGNIZED"    # 형식은 맞지만 아는 값이 아니다(source_value 에 원문 일부)
    INVALID = "INVALID"              # 키는 있으나 쓸 수 없다(null · 문자열이 아님 · 해당 칸의 주소 체계가 아닌 IP)


@unique
class GuardDutyResourceRole(str, Enum):
    """Service.ResourceRole — 이 탐지에서 자원이 한 역할. 자산의 ResourceRole 과 다른 값이다."""

    ACTOR = "ACTOR"
    TARGET = "TARGET"


@unique
class GuardDutyConnectionDirection(str, Enum):
    """NetworkConnectionAction.ConnectionDirection 이 문서화한 값."""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    UNKNOWN = "UNKNOWN"              # GuardDuty 가 방향을 알 수 없다고 적은 값(우리가 채운 기본값이 아니다)


_FactValue = TypeVar("_FactValue")


class GuardDutyFact(BaseModel, Generic[_FactValue]):
    """한 칸의 값과 그 읽힘 상태. value 는 status 가 RECORDED 일 때만 있다."""

    model_config = ConfigDict(extra="forbid")

    status: GuardDutyFactStatus
    value: Optional[_FactValue] = None
    # UNRECOGNIZED 일 때만, 알 수 없는 원문 문자열(잘라서)
    source_value: Optional[str] = Field(None, max_length=GUARDDUTY_SOURCE_VALUE_MAX)

    @model_validator(mode="after")
    def _value_matches_status(self):
        if (self.value is not None) != (self.status is GuardDutyFactStatus.RECORDED):
            raise ValueError("value 는 status 가 RECORDED 일 때만 있어야 합니다")
        if (self.source_value is not None) != (self.status is GuardDutyFactStatus.UNRECOGNIZED):
            raise ValueError("source_value 는 status 가 UNRECOGNIZED 일 때만 있어야 합니다")
        return self


class GuardDutyActivity(BaseModel):
    """탐지가 적은 활동 사실 — 같은 탐지 원문에서 같은 시점에 읽은 값이다(first/last_seen_at 과 같은 문서).

    GuardDutyFindingThreatPayload.activity 가 None 이면 이 값을 읽기 전에 저장된 탐지(수집 안 됨)이고,
    이 객체가 있으면 읽었다 — 칸마다 status 가 ABSENT 인지 UNRECOGNIZED·INVALID 인지 알린다.
    역할을 유형에서 추정하거나 기본값(OUTBOUND · Blocked=false)을 채우지 않는다. 이 값은 대응 판단 결과가 아니라
관측 사실이다 — 판단이 근거로 쓸 수는 있다."""

    model_config = ConfigDict(extra="forbid")

    resource_role: GuardDutyFact[GuardDutyResourceRole]               # Service.ResourceRole
    action_type: GuardDutyFact[str]                                    # Service.Action.ActionType
    # 아래 셋은 Service.Action.NetworkConnectionAction 에서 읽는다. 그 블록이 없으면 셋 다 ABSENT 다
    connection_direction: GuardDutyFact[GuardDutyConnectionDirection]
    remote_ip: GuardDutyFact[Union[IPv4Address, IPv6Address]]
    blocked: GuardDutyFact[bool]
