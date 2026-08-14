# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# Rule Evaluator → PostgreSQL·Asset Workflow로 전달하는 결정적 판정 결과의
# 내부 계약입니다. (Issue #48)
#
# 계약 원칙
#   - 공개 Enum(EvaluationStatus·Verdict·SkipReasonCode)을 재사용한다 — 중복 정의 금지.
#   - 공개 AssetItem(api/assets.py)과 같은 교차 불변식을 적용한다:
#     COMPLETED일 때만 판정값이 존재, SKIP이면 skip_reason_code 필수, 그 외 null.
#   - health_score는 0~100 정수(소수 거부). "EC2 전용" 규칙은 asset_type을 아는
#     결합 지점(AssetItem 직렬화)이 강제한다 — 이 모델에는 asset_type이 없다.
# ==============================================================================

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api.assets import EvaluationStatus, SkipReasonCode, UtcDateTime, Verdict


class RuleEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_arn: str = Field(min_length=1)
    collection_run_id: str = Field(min_length=1)
    evaluation_status: EvaluationStatus
    verdict: Optional[Verdict] = None
    health_score: Optional[int] = Field(None, ge=0, le=100)
    skip_reason_code: Optional[SkipReasonCode] = None
    # 판정 근거 요약 — 사용자 표시 문구가 아니라 내부 감사·Evidence용 서술
    reason: Optional[str] = Field(None, min_length=1)
    evaluated_at: UtcDateTime

    @model_validator(mode="after")
    def _enforce_contract(self):
        if self.evaluation_status == EvaluationStatus.COMPLETED:
            if self.verdict is None:
                raise ValueError("COMPLETED이면 verdict가 필수입니다")
        else:
            if (
                self.verdict is not None
                or self.health_score is not None
                or self.skip_reason_code is not None
            ):
                raise ValueError(
                    "COMPLETED가 아니면 verdict·health_score·skip_reason_code는 null이어야 합니다"
                )

        if self.verdict == Verdict.SKIP and self.skip_reason_code is None:
            raise ValueError("verdict=SKIP이면 skip_reason_code가 필수입니다")
        if (
            self.verdict is not None
            and self.verdict != Verdict.SKIP
            and self.skip_reason_code is not None
        ):
            raise ValueError("SKIP이 아닌 판정에서 skip_reason_code는 null이어야 합니다")
        return self
