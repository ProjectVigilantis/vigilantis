# ==============================================================================
# [파일 설명]
# MVP 합성 SSH 로그의 출처·집계·발췌 계약입니다. (Issue #350)
# 생성한 모의 자료의 전체 집계와 최대 12행 발췌를 위협 관측과 대조합니다.
# 저장·전달 계약: datasets/secops-log-corpus/MVP_CONNECTION.md
# ==============================================================================

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class MockLogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    source_time: AwareDatetime
    received_at: AwareDatetime
    host: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    process_id: int = Field(gt=0, strict=True)
    message: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _single_line(self):
        if any(c in self.message for c in ("\x00", "\r", "\n")):
            raise ValueError("log messages must be single lines without NUL")
        return self


class MockSshLogEvidence(BaseModel):
    """모의 자료 전체의 집계와 제한된 발췌이며, 실시간 수집 범위를 보장하지 않는다.

    전체 자료는 버전 관리하며 정규화된 JSON 지문으로 식별한다.
    PostgreSQL에는 발췌를 보존한다. 모의 입력 생성기가 제공한 집계값은 위협 관측과 대조한다.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    data_kind: Literal["synthetic"] = "synthetic"
    coverage: Literal["complete_generated_fixture"] = "complete_generated_fixture"
    case_id: str = Field(pattern=r"^C0[1-7]$")
    corpus_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: Literal["aws-directory-service-password-example"]
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_policy: Literal["ssh-observed-password-results-v1"]
    excerpt_policy: Literal["first_6_last_6"] = "first_6_last_6"
    host: str = Field(pattern=r"^mock-ec2-C0[1-7]$")
    target_arn: str = Field(min_length=1, max_length=512, pattern=r"^[^\x00\r\n]+$")
    mapping_kind: Literal["explicit_mock_target"] = "explicit_mock_target"
    time_shift_seconds: int = Field(default=0, strict=True)
    source_ip: str = Field(min_length=1, max_length=45)
    window_start: AwareDatetime
    window_end: AwareDatetime
    record_count: int = Field(ge=1, le=2000, strict=True)
    failed_attempt_count: int = Field(ge=1, le=1000, strict=True)
    accepted_count: int = Field(ge=0, le=2000, strict=True)
    auxiliary_count: int = Field(ge=0, le=2000, strict=True)
    records: list[MockLogRecord] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def _scope(self):
        if self.window_end <= self.window_start:
            raise ValueError("log window must be positive")
        if self.record_count != self.failed_attempt_count + self.accepted_count + self.auxiliary_count:
            raise ValueError("log aggregate counts do not cover the fixture")
        if len(self.records) != min(12, self.record_count):
            raise ValueError("excerpt count does not match first_6_last_6 policy")
        if len({r.record_id for r in self.records}) != len(self.records):
            raise ValueError("duplicate excerpt record_id")
        if any(r.host != self.host or not self.window_start <= r.source_time < self.window_end
               for r in self.records):
            raise ValueError("excerpt outside selected host/window")
        if self.records != sorted(self.records, key=lambda r: r.source_time):
            raise ValueError("excerpts must use source-time order")
        return self

    def matches_observation(self, observation) -> bool:
        return (
            observation.target_arn == self.target_arn
            and getattr(observation, "source_ip", None) == self.source_ip
            and observation.occurred_at == self.window_end
            and getattr(observation, "failed_attempt_count", None) == self.failed_attempt_count
            and getattr(observation, "window_seconds", None)
            == (self.window_end - self.window_start).total_seconds()
        )
