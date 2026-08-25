# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 가드레일 ④ AWS Dry-Run의 호출 계약입니다. (Issue #128, ADR-0007 §1·§3)
# executor.precheck()의 반환 타입이자, ai/guardrails.py가 GuardrailStepResult로
# 옮겨 담는 값이다 — 매핑은 1:1이다.
#
#   passed               → GuardrailStepResult.result
#   reason_code          → GuardrailStepResult.reason_code
#   verification_summary → GuardrailStepResult.verification_summary
#
# 계약 원칙
#   - GuardrailDecision이 PASS/FAIL 2값이라 "확인 불가"라는 제3의 판정은 담을 자리가
#     없다. 조회로도 확인하지 못한 부분이 있으면 FAIL이거나, 확인 범위를 요약에
#     남기고 PASS다.
#   - verification_summary는 PASS·FAIL 모두 필수다. 형식을 코드로 고정하는 이유는
#     FE가 거절 근거로 이 문자열을 그대로 노출하기 때문이다(ADR-0007 §3).
#   - "미확인:" 항목은 비워 두지 않는다. 확인 범위의 한계를 남기는 것이 이 필드의
#     존재 이유이며, 조회 대체 경로는 항상 IAM 권한을 검증하지 못한다.
#
# PrecheckReasonCode는 #125가 packages/schemas/guardrails.py의 단계별 공용 목록으로
# 흡수할 예정이다. 접두 PRECHECK_는 그때도 유지되므로 거절 기록에서 단계를 역산하는
# 성질은 보존된다.
# ==============================================================================

from __future__ import annotations

import re
from enum import Enum, unique
from typing import Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator


@unique
class PrecheckReasonCode(str, Enum):
    """④ 거절 사유. AWS 응답 → 코드 매핑은 ADR-0007 §2 표가 원천이다."""

    PRECHECK_UNAUTHORIZED = "PRECHECK_UNAUTHORIZED"
    PRECHECK_TARGET_NOT_FOUND = "PRECHECK_TARGET_NOT_FOUND"
    PRECHECK_INVALID_STATE = "PRECHECK_INVALID_STATE"
    PRECHECK_NOT_IMPLEMENTED = "PRECHECK_NOT_IMPLEMENTED"
    # #49(런북별 typed 파라미터 계약) 이전의 과도기 코드 — 파라미터 키 누락·형식
    # 위반이 ① Schema Check에서 걸리지 않고 ④에서 처음 드러나는 동안만 쓰인다.
    PRECHECK_PARAM_INVALID = "PRECHECK_PARAM_INVALID"
    PRECHECK_AWS_ERROR = "PRECHECK_AWS_ERROR"


@unique
class VerificationMethod(str, Enum):
    """④가 실제로 사용한 확인 수단. DryRun을 쓸 수 없는 작업은 조회로 대체한다."""

    DRY_RUN = "DRY_RUN"
    DESCRIBE = "DESCRIBE"
    MIXED = "MIXED"


# ADR-0007 §3 형식: "<방식>[(작업)] | 확인: <...> | 미확인: <...>"
# 확인·미확인 본문에는 괄호가 들어올 수 있으므로(예: "IAM 권한(LocalStack iam disabled)")
# 머리 부분만 제약한다. 구분자로 쓰는 "|"는 본문에 넣을 수 없다.
_SUMMARY_RE = re.compile(
    r"^(?P<method>DRY_RUN|DESCRIBE|MIXED)"
    r"(?:\((?P<operations>[^)|]+)\))?"
    r" \| 확인: (?P<verified>[^|]+)"
    r" \| 미확인: (?P<unverified>[^|]+)$"
)

_SUMMARY_FORMAT_HINT = "<방식>[(작업)] | 확인: <...> | 미확인: <...> (방식 ∈ DRY_RUN·DESCRIBE·MIXED)"

# 항목 나열 구분자 — 런북 명세서·ADR 서술과 같은 기호를 쓴다
_ITEM_SEP = "·"


def build_verification_summary(
    method: VerificationMethod,
    *,
    verified: Sequence[str],
    unverified: Sequence[str],
    operations: Sequence[str] = (),
) -> str:
    """ADR-0007 §3 형식의 verification_summary를 조립한다.

    unverified를 비우는 것은 계약 위반이다 — 확인하지 못한 범위가 정말 없더라도
    그 사실을 문자열로 남긴다(예: "없음(DryRun 전 구간 적용)").
    """
    if not verified:
        raise ValueError("verified 항목이 비어 있습니다")
    if not unverified:
        raise ValueError("unverified 항목이 비어 있습니다 — 확인 한계는 반드시 남깁니다")

    head = method.value
    if operations:
        head += f"({_join(operations, 'operations')})"
    return (
        f"{head}"
        f" | 확인: {_join(verified, 'verified')}"
        f" | 미확인: {_join(unverified, 'unverified')}"
    )


def _join(items: Sequence[str], field: str) -> str:
    cleaned = [item.strip() for item in items]
    if any(not item for item in cleaned):
        raise ValueError(f"{field}에 빈 항목이 있습니다")
    if any("|" in item for item in cleaned):
        raise ValueError(f"{field} 항목에는 구분자 '|'를 넣을 수 없습니다")
    if field == "operations" and any(")" in item or "(" in item for item in cleaned):
        raise ValueError("operations 항목에는 괄호를 넣을 수 없습니다")
    return _ITEM_SEP.join(cleaned)


class PrecheckOutcome(BaseModel):
    """executor.precheck()의 반환값. 예외 대신 이 값으로만 판정이 나간다."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    reason_code: Optional[PrecheckReasonCode] = None
    verification_summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def _reason_code_matches_result(self):
        if self.passed and self.reason_code is not None:
            raise ValueError("PASS에는 reason_code를 기록하지 않습니다")
        if not self.passed and self.reason_code is None:
            raise ValueError("FAIL에는 reason_code가 필요합니다")
        return self

    @model_validator(mode="after")
    def _summary_follows_format(self):
        if not _SUMMARY_RE.match(self.verification_summary):
            raise ValueError(f"verification_summary 형식 위반 — {_SUMMARY_FORMAT_HINT}")
        return self
