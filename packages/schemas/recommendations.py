# ==============================================================================
# [파일 설명] 서버 규칙이 후보 생성 시점에 한 번 남기는 격리 추천 근거 계약 (#598)
#
# 후보(RunbookCandidate)에 불변으로 저장되는 규칙·분류·사유·판정 시각만 담는다.
# 사실(대상 주소·역할·증거 ID)과 자유 서술은 여기에 복제하지 않는다 — 증거는 후보의
# evidence_ids가 가리킨다. 분류는 「조건부 추천」이며 침해 증명이나 무조건 추천이 아니다.
# 저장된 판정은 이후 읽기에서 다시 계산하지 않는다.
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, unique
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PlainSerializer, model_validator


def _to_utc_z_micro(v: datetime) -> str:
    # 공용 UtcDateTime은 초 미만을 버린다. 판정 시각은 저장·왕복에서 같은 순간이어야 하므로
    # 이 계약만 마이크로초까지 고정 자릿수로 직렬화한다(naive는 UTC로 본다 — UtcDateTime과 같다).
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


DecisionTimestamp = Annotated[
    datetime, PlainSerializer(_to_utc_z_micro, return_type=str, when_used="json")
]


@unique
class IsolationRecommendationClassification(str, Enum):
    CONDITIONALLY_RECOMMENDED = "CONDITIONALLY_RECOMMENDED"
    OPERATOR_JUDGEMENT_REQUIRED = "OPERATOR_JUDGEMENT_REQUIRED"


@unique
class IsolationRecommendationReason(str, Enum):
    GUARDDUTY_SSH_BRUTE_FORCE_ACTOR = "GUARDDUTY_SSH_BRUTE_FORCE_ACTOR"
    TARGET_ADDRESS_NOT_PROVIDED = "TARGET_ADDRESS_NOT_PROVIDED"
    RESOURCE_ROLE_TARGET = "RESOURCE_ROLE_TARGET"
    RESOURCE_ROLE_UNKNOWN = "RESOURCE_ROLE_UNKNOWN"
    RESOURCE_ROLE_NOT_CAPTURED = "RESOURCE_ROLE_NOT_CAPTURED"
    FINDING_TYPE_NOT_COVERED = "FINDING_TYPE_NOT_COVERED"
    DIRECTION_NOT_CONFIRMED = "DIRECTION_NOT_CONFIRMED"
    DIRECTION_CONFLICT = "DIRECTION_CONFLICT"
    EVIDENCE_TARGET_MISMATCH = "EVIDENCE_TARGET_MISMATCH"


class IsolationRecommendationBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: Literal["SERVER_RULE"] = "SERVER_RULE"
    rule_id: Literal["PROTECTED_EC2_ISOLATION"] = "PROTECTED_EC2_ISOLATION"
    rule_version: Literal[1] = 1
    classification: IsolationRecommendationClassification
    reason_code: IsolationRecommendationReason
    evaluated_at: DecisionTimestamp

    @model_validator(mode="after")
    def _classification_matches_reason(self):
        conditional = (
            self.classification
            == IsolationRecommendationClassification.CONDITIONALLY_RECOMMENDED
        )
        actor = (
            self.reason_code
            == IsolationRecommendationReason.GUARDDUTY_SSH_BRUTE_FORCE_ACTOR
        )
        if conditional != actor:
            raise ValueError(
                "CONDITIONALLY_RECOMMENDED는 GUARDDUTY_SSH_BRUTE_FORCE_ACTOR 사유일 때만, "
                "그 사유는 CONDITIONALLY_RECOMMENDED일 때만 가능합니다"
            )
        return self
