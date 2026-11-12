"""침해사고 신고 작성 보조 파일. 인용 범위 검사는 서술의 진실성을 보장하지 않는다."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .assets import UtcDateTime
from .evidence_export import EvidenceExportFlags


class ReportFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    source_kind: Literal["incident", "evidence", "action_execution", "audit_event"]
    record_id: str
    json_path: str
    recorded_at: UtcDateTime
    label: str
    value: str | int | float | bool | None
    status: Literal["known", "unknown"]
    reason: Literal[
        "not_recorded", "no_history", "not_collected", "identity_omitted",
        "free_text_omitted", "after_cutoff", "invalid_identifier",
    ] | None = None
    interpretation: str | None = None
    flags: EvidenceExportFlags | None = None

    @model_validator(mode="after")
    def _unknown_shape(self):
        if self.status == "unknown":
            if self.value is not None or self.reason is None:
                raise ValueError("미확인은 값 없이 사유를 갖는다")
        elif self.value is None or self.reason is not None:
            raise ValueError("확인된 값은 사유 없이 값을 갖는다")
        return self


class ReportEvidenceFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    flags: EvidenceExportFlags


class ReportFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[ReportFact]
    evidence_flags: list[ReportEvidenceFlags]
    missing: list[str]


class ReportSentence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: Literal["occurrence", "response"]
    text: str = Field(min_length=1, max_length=1000)
    citations: list[str] = Field(min_length=1, max_length=16)


class ReportNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sentences: list[ReportSentence] = Field(min_length=1, max_length=20)


class ReportHumanField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    kind: Literal["text", "confirmation"] = "text"
    value: None = None
    checked: Literal[False] | None = None


class ReportDraftDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    incident_id: str
    as_of: UtcDateTime
    generated_at: UtcDateTime
    notice: str
    citation_validation: Literal["scope_only"] = "scope_only"
    server_facts: ReportFacts
    ai_narrative: ReportNarrative
    human_input: list[ReportHumanField]
