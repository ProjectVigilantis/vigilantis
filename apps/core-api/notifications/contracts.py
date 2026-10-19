"""발송 경계의 값 객체. 자격증명과 공급자 원문은 결과에 담지 않는다."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Channel = Literal["SLACK", "SNS"]
CHANNELS: tuple[Channel, ...] = ("SLACK", "SNS")
FailureCategory = Literal["TRANSIENT", "PERMANENT", "UNKNOWN"]


@dataclass(frozen=True)
class EventSnapshot:
    event_id: str
    incident_id: str
    execution_id: str | None
    related_execution_id: str | None
    target_arn: str
    aws_account_id: str | None
    region: str | None
    kind: str
    phase: str
    outcome: str | None
    trigger_source: str | None
    runbook_id: str | None
    execution_status: str | None
    reason_code: str | None
    occurred_at: datetime
    created_at: datetime

    @classmethod
    def from_row(cls, row) -> EventSnapshot:
        values = {}
        for name in cls.__dataclass_fields__:
            value = getattr(row, name)
            values[name] = getattr(value, "value", value)
        return cls(**values)


@dataclass(frozen=True)
class Route:
    """Fingerprint covers customer/account/channel/destination, never a global revision."""

    tenant_id: str
    aws_account_id: str
    channel: Channel
    fingerprint: str
    destination: str = field(repr=False)
    region: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    route: Route | None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class SendResult:
    receipt_kind: Literal["SLACK_POSTED", "SNS_ACCEPTED"] | None = None
    receipt_ref: str | None = None
    failure_category: FailureCategory | None = None
    error_code: str | None = None
    retry_after_seconds: float | None = None

    @property
    def succeeded(self) -> bool:
        return self.receipt_kind is not None
