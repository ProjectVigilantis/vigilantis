# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# Detection → Incident 생성 입력 계약입니다. (Issue #254)
# Rule Engine(FinOps 자산 판정)과 Risk Evaluator(SecOps 위협 판정)의 결과를 Incident
# 1건으로 묶는 입력이며, 생성 Workflow(apps/core-api/incident_intake.py)의 진입 타입이다.
#
# 계약 원칙
#   - **한 시점에서 나온 값만 묶는다.** FinOps Intake는 판정(RuleEvaluationResult)과
#     그 판정이 내려진 회차의 자산 스냅샷을 함께 받고, 둘이 같은 collection_run_id에서
#     나왔는지 계약이 확인한다. 자산 행은 수집 회차마다 최신 관측으로 덮어쓰지만
#     (db/repositories/assets.py upsert_asset) 판정은 회차 단위로 보존되므로
#     (db/models.py RuleEvaluation의 asset_id·collection_run_id 유니크), 스냅샷을 빼면
#     나중에 자산을 다시 읽는 쪽이 **예전 판정 + 최신 자산**을 한 시점인 양 조립하게
#     된다 — t3.xlarge에서 COST_CANDIDATE가 난 뒤 다음 회차에 t3.medium으로 바뀌면
#     이미 줄어든 인스턴스에 그 판정이 붙는다.
#     이 대조는 판정을 만든 쪽이 자산 행과 같은 회차를 쓸 때만 성립한다. 지금은
#     run_rule_engine이 회차 인자 없이 불려 자산 행의 last_collection_run_id를 그대로
#     쓰지만(services/rule_engine.py), 나중에 회차를 명시로 넘기면 그 회차에 관측되지
#     않은 자산에도 그 회차 ID가 찍혀 정상 판정이 여기서 거절된다 — 호출부를 붙이는
#     쪽이 확인할 것.
#   - 보장 둘을 분리한다. 이 계약이 담는 것은 **Incident 생성 근거**(Detection 당시
#     자산 + 같은 회차 판정)이고, **현재 조치 가능성**은 AI 제안이 나온 직후 가드레일
#     ④ AWS Dry-Run이 한 번 본다(precheck — ADR-0007). 최신 상태가 필요해도 Detection
#     스냅샷을 대체하지 않는다.
#   - Incident가 되는 자산 판정은 INCIDENT_TRIGGERING_VERDICTS 2종이다 —
#     COST_CANDIDATE(저활성 EC2)·UNUSED(미부착 SG·미부착 EBS 볼륨). SKIP은 LLM 호출을
#     아끼려 판정 단계가 이미 거른 자산이라 여기 오지 않는다(services/rule_engine.py).
#   - THREAT 판정은 Incident를 만들지 않는다. SECOPS Incident는 DB CHECK
#     category_risk_shape가 title·initial_risk_level·사유 코드 1개 이상을 요구하는데
#     (db/models.py Incident) 자산 판정에는 그 셋을 채울 값이 없다. **차단이지 탐지
#     제거가 아니다** — 전체개방 SG는 OPEN_IP 위협 이벤트로 들어와 Risk Evaluator가
#     위험도를 내고(events.py ThreatEventType.OPEN_IP · security/risk_evaluator.py),
#     SECOPS Incident는 그 경로 하나로 모인다.
#   - Incident 분류 축과 Runbook Registry 도메인 축은 별개다(SSOT §Action Whitelist
#     "분류 축 주의"). UNUSED SG가 부르는 RUNBOOK_SG_DELETE_ISOLATED는 Registry에서
#     SECOPS지만 그 Incident는 FINOPS다 — 위협 이벤트가 없어 SECOPS 형태를 채울 수
#     없기 때문이다. 두 축을 같은 것으로 보면 이 Incident는 어느 쪽으로도 만들 수 없다.
#   - 중복은 계약이 막지 못한다(저장소를 봐야 안다). 생성 Workflow가 수행하며 규칙은
#     둘이다 — FinOps는 같은 subject_arn의 미종료 Incident가 있으면 만들지 않고,
#     SecOps는 deduplication_key로 막는다(db/repositories/incidents.py
#     get_threat_event_by_dedup_key).
# ==============================================================================

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .api.assets import AssetItem, Verdict
from .api.incidents import IncidentCategory
from .events import InitialRiskEvaluationResult, NormalizedThreatEvent
from .rules import RuleEvaluationResult

# 자산 판정 4종 중 Incident가 되는 2종. THREAT·SKIP이 빠진 이유는 파일 헤더에 있다.
INCIDENT_TRIGGERING_VERDICTS: frozenset[Verdict] = frozenset(
    {
        Verdict.COST_CANDIDATE,
        Verdict.UNUSED,
    }
)


def _as_utc(v: datetime) -> datetime:
    return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v


class DetectionAssetSnapshot(BaseModel):
    """판정이 내려진 그 회차의 자산 상태.

    자산 표현은 공개 AssetItem을 그대로 쓴다 — 대상 ARN·유형·상태·Spec·관계를 이미
    담고 있고 spec↔asset_type 정합도 그쪽 계약이 강제한다(그래프 입력의 자산 문맥과
    같은 타입 — schemas/agents.py AgentAssetContext, #49 확정).

    collection_run_id를 따로 받는 것은 공개 AssetItem이 그 값을 담지 않기 때문이다
    (FE 계약이라 여기 필요한 필드를 늘리지 않는다). 자산 행의 last_collection_run_id에서
    채우며, 판정의 collection_run_id와 대조하는 것이 이 필드의 유일한 쓸모다.
    """

    model_config = ConfigDict(extra="forbid")

    collection_run_id: str = Field(min_length=1)
    asset: AssetItem


class FinOpsIncidentIntake(BaseModel):
    """자산 판정 1건 → FINOPS Incident 생성 입력.

    title을 받지 않는다 — FINOPS 제목은 진단명이라 분석 전에는 null이고, 그 경우
    화면이 분류와 대상 ARN 축약으로 표시한다(api/incidents.py title 계약).
    """

    model_config = ConfigDict(extra="forbid")

    category: Literal[IncidentCategory.FINOPS] = IncidentCategory.FINOPS
    asset_snapshot: DetectionAssetSnapshot
    rule_evaluation: RuleEvaluationResult

    @property
    def subject_arn(self) -> str:
        return self.rule_evaluation.asset_arn

    @model_validator(mode="after")
    def _enforce_contract(self):
        # verdict가 이 집합에 들면 evaluation_status는 COMPLETED다 —
        # RuleEvaluationResult가 "COMPLETED가 아니면 verdict는 null"을 이미 강제한다.
        if self.rule_evaluation.verdict not in INCIDENT_TRIGGERING_VERDICTS:
            raise ValueError(
                "FINOPS Incident가 되는 판정은 COST_CANDIDATE·UNUSED뿐입니다 "
                f"(받은 값: {self.rule_evaluation.verdict})"
            )

        asset = self.asset_snapshot.asset
        if asset.arn != self.rule_evaluation.asset_arn:
            raise ValueError(
                "자산 스냅샷과 판정이 서로 다른 자산을 가리킵니다 "
                f"({asset.arn} vs {self.rule_evaluation.asset_arn})"
            )

        # 같은 회차에서 나왔는지 — 이 대조가 "예전 판정 + 최신 자산" 조립을 막는다.
        if self.asset_snapshot.collection_run_id != self.rule_evaluation.collection_run_id:
            raise ValueError(
                "자산 스냅샷과 판정의 collection_run_id가 다릅니다 "
                f"({self.asset_snapshot.collection_run_id} vs "
                f"{self.rule_evaluation.collection_run_id})"
            )

        # 같은 회차라면 자산에 실린 판정 표기도 그 판정과 같아야 한다.
        rendered = (asset.evaluation_status, asset.verdict, asset.health_score,
                    asset.skip_reason_code)
        judged = (self.rule_evaluation.evaluation_status, self.rule_evaluation.verdict,
                  self.rule_evaluation.health_score, self.rule_evaluation.skip_reason_code)
        if rendered != judged:
            raise ValueError(
                "자산 스냅샷에 실린 판정 표기가 rule_evaluation과 다릅니다 "
                f"({rendered} vs {judged})"
            )

        # 판정은 관측보다 앞설 수 없다(수집 → 판정 순서, services/scheduler.py 파이프라인).
        if _as_utc(self.rule_evaluation.evaluated_at) < _as_utc(asset.collected_at):
            raise ValueError(
                "evaluated_at이 자산 스냅샷의 collected_at보다 앞섭니다 — 판정이 관측한 적 "
                "없는 자산 상태와 묶였습니다"
            )
        return self


class SecOpsIncidentIntake(BaseModel):
    """위협 이벤트 1건 + 초기 위험 판정 → SECOPS Incident 생성 입력.

    자산 스냅샷을 받지 않는다 — 위협 판정은 자산 문맥에 의존하지 않고 들어온 위협
    정보만 본다(security/risk_evaluator.py 판정 규칙 ②). 대상 자산은 target_arn이
    가리키며, 그래프 입력의 자산 문맥은 SecOps 경로를 구현할 때 함께 정한다.

    title은 필수다 — 카드 제목이 곧 위협 이름이고, 비면 제목이 자원 ID가 된다
    (Issue #200, api/incidents.py). 위협 이름은 만드는 시점에 이미 정해져 있어
    AI 분석을 기다리지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    category: Literal[IncidentCategory.SECOPS] = IncidentCategory.SECOPS
    title: str = Field(min_length=1)
    threat_event: NormalizedThreatEvent
    initial_risk: InitialRiskEvaluationResult

    @property
    def subject_arn(self) -> str:
        return self.threat_event.target_arn

    @model_validator(mode="after")
    def _enforce_contract(self):
        # 두 값이 다른 이벤트를 가리키면 Incident가 A의 위험도로 B를 대응하게 된다.
        if self.initial_risk.threat_event_id != self.threat_event.threat_event_id:
            raise ValueError(
                "initial_risk.threat_event_id는 threat_event.threat_event_id와 같아야 합니다"
            )
        return self


IncidentIntake = Annotated[
    Union[FinOpsIncidentIntake, SecOpsIncidentIntake],
    Field(discriminator="category"),
]

# discriminated union 검증용 어댑터 — 생성 Workflow 진입점에서 사용한다.
INCIDENT_INTAKE_ADAPTER: TypeAdapter = TypeAdapter(IncidentIntake)
