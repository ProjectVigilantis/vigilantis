# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# FinOps 그래프 응답 품질 계측 도구입니다. (Issue #237)
#
# 앱 실행 경로가 import하지 않는다 — 사람이 손으로 돌리는 계측용이며, 진입점은
# scripts/finops_eval.py다. 여기 두는 이유는 import 방향 때문이다: tests/에서
# ai.*를 부르는 경로는 열려 있고 반대는 안 되므로, 골든 정답지 회귀(#234)도 같은
# 함수를 쓸 수 있는 자리가 여기뿐이다.
#
# 이 패키지의 모듈은 파일 경로를 모른다. 파싱된 계약 객체만 받아 계약 객체를 낸다 —
# 골든 JSON을 읽는 것은 호출부 몫이다.
# ==============================================================================

from .cases import EvalCase, finops_cases, input_fingerprint
from .factcheck import FactCheckResult, check_summary_facts
from .judge import (
    JUDGE_VERSION,
    DefectJudgement,
    RestorationOutput,
    RestorationScore,
    defect_request,
    expected_deciding_values,
    fact_sheet,
    judge_fingerprint,
    proposal_cards,
    restoration_request,
    score_restoration,
)
from .readback import (
    ReadbackResult,
    check_readback,
    cites_input,
    observation_cites_input,
)
from .report import CaseRun, ColumnReport, build_column_report
from .reproducibility import (
    FieldAgreement,
    field_agreement,
    output_fields,
    unstable_fields,
)

__all__ = [
    "JUDGE_VERSION",
    "CaseRun",
    "ColumnReport",
    "DefectJudgement",
    "EvalCase",
    "FactCheckResult",
    "FieldAgreement",
    "ReadbackResult",
    "RestorationOutput",
    "RestorationScore",
    "build_column_report",
    "check_readback",
    "check_summary_facts",
    "cites_input",
    "defect_request",
    "expected_deciding_values",
    "fact_sheet",
    "field_agreement",
    "finops_cases",
    "input_fingerprint",
    "judge_fingerprint",
    "observation_cites_input",
    "output_fields",
    "proposal_cards",
    "restoration_request",
    "score_restoration",
    "unstable_fields",
]
