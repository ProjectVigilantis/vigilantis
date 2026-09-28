"""구조화 답지를 기계적으로 대조하고 요약 의미 검토의 근거를 확인한다.

요약의 의미는 이름을 남긴 사람 또는 assistant 검토자가 판정한다.
이 도구는 검토 대상·인용의 연결을 확인하며 의미 판정의 정확성을 보증하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from schemas.agents import AgentGraphOutput, SecOpsGraphInput
from schemas.incidents import AgentInvocationStatus

from ai.agent import (
    CandidateProposalOutput,
    EvidenceSummaryOutput,
    RiskReassessmentOutput,
)

from .dataset import ROOT, EvalCase, digest, read_json

Verdict = Literal["PASS", "FAIL", "PENDING", "TOOL_ERROR"]


class CandidateAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runbook_id: str
    target_arn: str
    evidence_ids: list[str] = Field(min_length=1)
    cidr_block: str
    protocol: Literal["tcp"]
    rule_number_min: int = Field(ge=1)
    rule_number_max: int = Field(le=32766)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    reviewed_risk_levels: list[Literal["LOW", "MEDIUM", "HIGH"]] = Field(min_length=1)
    allowed_statuses: list[Literal["SUCCEEDED", "NO_PROPOSAL"]]
    action_policy: Literal["NACL_PROPOSAL", "OBSERVE", "EITHER", "PENDING"]
    risk_basis: str
    action_basis: str
    candidate: CandidateAnswer | None


def load_answers(cases: list[EvalCase], path: Path = ROOT / "answers.json") -> dict[str, Answer]:
    answers = [Answer.model_validate(value) for value in read_json(path)["cases"]]
    ids = [answer.case_id for answer in answers]
    if len(ids) != len(set(ids)) or set(ids) != {case.case_id for case in cases}:
        raise ValueError("Answer IDs must match the full fixed input set exactly")
    policies = {
        "NACL_PROPOSAL": {"SUCCEEDED"}, "OBSERVE": {"NO_PROPOSAL"},
        "EITHER": {"SUCCEEDED", "NO_PROPOSAL"}, "PENDING": set(),
    }
    for answer in answers:
        if set(answer.allowed_statuses) != policies[answer.action_policy]:
            raise ValueError(f"{answer.case_id}: action policy and allowed statuses disagree")
        if answer.action_policy in {"NACL_PROPOSAL", "EITHER"} and answer.candidate is None:
            raise ValueError(f"{answer.case_id}: proposal policy requires a candidate answer")
        if answer.action_policy == "OBSERVE" and answer.candidate is not None:
            raise ValueError(f"{answer.case_id}: observation requires no candidate answer")
        if answer.candidate and answer.candidate.rule_number_min > answer.candidate.rule_number_max:
            raise ValueError(f"{answer.case_id}: invalid rule number interval")
    return {answer.case_id: answer for answer in answers}


@dataclass(frozen=True)
class Score:
    verdict: Verdict
    reasons: tuple[str, ...] = ()


def score_structured(output: AgentGraphOutput, answer: Answer) -> Score:
    reasons: list[str] = []
    if output.invocation_status is AgentInvocationStatus.FAILED:
        return Score("FAIL", ("graph_or_service_contract_failed",))
    if not output.reviewed_risk_level or output.reviewed_risk_level.value not in answer.reviewed_risk_levels:
        reasons.append("reviewed_risk_outside_answer")
    if any(not line.strip() for line in output.summary_lines):
        reasons.append("empty_summary_line")
    if answer.action_policy != "PENDING" and output.invocation_status.value not in answer.allowed_statuses:
        reasons.append("inappropriate_proposal_or_absence")

    if output.candidates:
        if len(output.candidates) != 1:
            reasons.append("candidate_count")
        expected = answer.candidate
        for candidate in output.candidates:
            if expected is None:
                reasons.append("unexpected_candidate")
                continue
            actual = candidate.model_dump(mode="json")
            for field in ("runbook_id", "target_arn"):
                if actual[field] != getattr(expected, field):
                    reasons.append(field)
            if set(actual["evidence_ids"]) != set(expected.evidence_ids):
                reasons.append("evidence_ids")
            for field in ("cidr_block", "protocol"):
                if actual["parameters"].get(field) != getattr(expected, field):
                    reasons.append(field)
            number = actual["parameters"].get("rule_number")
            if type(number) is not int or not expected.rule_number_min <= number <= expected.rule_number_max:
                reasons.append("rule_number")
    if reasons:
        return Score("FAIL", tuple(dict.fromkeys(reasons)))
    if answer.action_policy == "PENDING":
        return Score("PENDING", ("action_expectation_pending",))
    return Score("PASS")


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_id: str
    verdict: Literal["PASS", "FAIL", "UNSURE"]
    # 요약 한 줄에서 그대로 인용한다. 필수 정보 누락 판정에는 인용이 없을 수 있다.
    quote: str
    reason: str = Field(min_length=1)


class SummaryReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    repeat: int = Field(ge=1)
    output_sha256: str
    items: list[ReviewItem]


class ReviewFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    inputs_sha256: str
    answers_sha256: str
    rubric_sha256: str
    reviewer: str = Field(min_length=1)
    reviewer_kind: Literal["human", "assistant"]
    reviews: list[SummaryReview]


@lru_cache(maxsize=1)
def _internal_identifier_pattern() -> re.Pattern[str]:
    """계약의 필드·enum과 내부 식별자 표기를 찾되 관제용 기술 약어는 허용한다."""
    terms = {"action_targets", "asset_context_at", "reviewed_risk_label"}

    def collect(value):
        if isinstance(value, dict):
            terms.update(value.get("properties", {}))
            terms.update(item for item in value.get("enum", []) if isinstance(item, str))
            if isinstance(value.get("const"), str):
                terms.add(value["const"])
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for model in (SecOpsGraphInput, AgentGraphOutput, CandidateProposalOutput,
                  EvidenceSummaryOutput, RiskReassessmentOutput):
        collect(model.model_json_schema())
    terms.difference_update({"EC2", "SG", "NACL", "EBS", "S3", "ASG", "TCP", "UDP", "ICMP", "tcp", "udp", "icmp"})
    identifiers = [re.escape(term) for term in sorted(terms) if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", term)]
    # SSH_BRUTEFORCE처럼 정식 enum을 잘못 옮긴 내부 표기도 잡는다. 한국어 조사는 경계로 인정한다.
    alternatives = "|".join([*identifiers, r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"])
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + alternatives + r")(?![A-Za-z0-9_])")


def score_summary_identifiers(output: AgentGraphOutput) -> Score:
    # ARN은 관측 대상의 값이므로 내부 필드명 arn과 구분한다.
    lines = [re.sub(r"\barn:[a-z0-9-]+:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:[^\s,;]+", "", line)
             for line in output.summary_lines]
    matches = {match.group() for line in lines for match in _internal_identifier_pattern().finditer(line)}
    return (Score("FAIL", tuple(f"internal_identifier:{term}" for term in sorted(matches)))
            if matches else Score("PASS"))


def score_summary(output: AgentGraphOutput, review: SummaryReview | None, rubric: dict) -> Score:
    deterministic = (score_summary_identifiers(output)
                     if any(item["check_id"] == "internal_identifiers"
                            for item in rubric.get("deterministic_checks", [])) else Score("PASS"))
    semantic = _score_summary_review(output, review, rubric)
    if deterministic.verdict == "FAIL":
        return Score("FAIL", deterministic.reasons + semantic.reasons)
    return semantic


def _score_summary_review(output: AgentGraphOutput, review: SummaryReview | None, rubric: dict) -> Score:
    if review is None:
        return Score("PENDING", ("summary_review_missing",))
    if review.output_sha256 != digest(output.model_dump(mode="json")):
        return Score("TOOL_ERROR", ("review_output_mismatch",))
    checks = {item["check_id"]: item for item in rubric["checks"]}
    ids = [item.check_id for item in review.items]
    if len(ids) != len(set(ids)) or set(ids) != set(checks):
        return Score("TOOL_ERROR", ("review_check_ids_mismatch",))
    failed, pending = [], []
    for item in review.items:
        needs_quote = (
            checks[item.check_id]["kind"] == "required" and item.verdict == "PASS"
            or checks[item.check_id]["kind"] == "forbidden" and item.verdict == "FAIL"
        )
        if not item.reason.strip() or (needs_quote and not item.quote.strip()):
            return Score("TOOL_ERROR", (f"review_evidence_missing:{item.check_id}",))
        if item.quote and not any(item.quote in line for line in output.summary_lines):
            return Score("TOOL_ERROR", (f"review_quote_not_in_output:{item.check_id}",))
        if item.verdict == "FAIL":
            failed.append(item.check_id)
        elif item.verdict == "UNSURE":
            pending.append(item.check_id)
    # 다른 항목의 불확실성을 이유로 확인된 결함을 지우지 않는다.
    return (Score("FAIL", tuple(failed)) if failed else
            Score("PENDING", tuple(pending)) if pending else Score("PASS"))
