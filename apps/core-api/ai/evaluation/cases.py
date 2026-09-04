# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# Golden Dataset FinOps 자산을 그래프 입력(FinOpsGraphInput)으로 옮깁니다. (Issue #237)
#
# 그래프를 검증하는 자리가 둘(ai/tests/test_finops_graph.py · scripts/smoke_finops_graph.py)
# 인데 둘 다 합성 입력이라, 같은 값을 반복해서 넣어 볼 고정 세트가 없었다. 재현성 계측과
# 사실 정합성 검사가 전부 이 함수가 내는 케이스 위에서 돈다.
#
# 계약 원칙
#   - **같은 입력을 몇 번 만들어도 값이 같다.** 시각은 인벤토리의 collected_at에서
#     파생하고 벽시계를 쓰지 않는다. 이것이 깨지면 N회 반복 계측 자체가 성립하지 않는다
#     — 매 회차가 서로 다른 입력을 넣은 것이 되기 때문이다.
#   - 판정(verdict·skip_reason_code)의 원천은 정답지다. 이 모듈이 다시 판정하지 않는다.
#     예외는 health_score 하나인데, 정답지가 "실행 시점 변환이라 검증 범위 밖"이라며
#     제외한 값이라(datasets/golden/README.md §정답 형식) rule_engine의 계산을 그대로
#     부른다 — 여기서 식을 베껴 쓰면 원천이 둘이 된다.
#   - **프로덕션이 만들 값과 같은 값을 만든다.** reason 문자열·자산 spec·관계 파생은
#     services/rule_engine.py·services/collector.py를 그대로 따른다. 여기서 더 친절한
#     값을 채우면 모델이 프로덕션에서는 받지 못할 정보를 받게 되고, 그 위에서 잰
#     기준선은 실경로를 대표하지 못한다.
#   - 파일 경로를 모른다. 파싱된 AssetInventory와 정답지 dict를 받는다.
# ==============================================================================

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping, Optional, Sequence

from schemas.agents import FinOpsGraphInput
from schemas.api.assets import AssetType, RelationType, SkipReasonCode, Verdict
from schemas.assets import AssetInventory, Ec2Asset, MetricName
from schemas.runbook_parameters import RESOURCE_ID_PARAM
from schemas.runbooks import RUNBOOK_DOMAIN_BY_ID, RunbookDomain, RunbookId

from services.rule_engine import evaluate_ec2

# ------------------------------------------------------------------------------
# 인시던트로 삼을 판정
# ------------------------------------------------------------------------------
# rule_engine은 COST_CANDIDATE·THREAT·UNUSED를 AI로 넘긴다고 적고 있다
# (services/rule_engine.py 헤더). 그중 FinOps 그래프가 받을 수 있는 것은
# COST_CANDIDATE뿐이다.
#   - THREAT·UNUSED의 대상 런북(EC2_ISOLATE·NACL_*·SG_DELETE_ISOLATED)은 전부
#     RUNBOOK_DOMAIN_BY_ID에서 SECOPS이고, FinOpsGraphInput.domain은 FINOPS 고정이다.
#   - SKIP은 LLM 호출을 아끼려고 판정 단계가 이미 거른 자산이라 인시던트가 되지 않는다.
_INCIDENT_VERDICTS = frozenset({Verdict.COST_CANDIDATE})

# RESOURCE_ID_PARAM(runbook_parameters.py)이 런북마다 "target_arn이 가리켜야 하는
# 자원 ID"를 정하고 있어, 그 파라미터 이름이 곧 대상 자산 유형이다.
_ASSET_TYPE_BY_RESOURCE_ID_PARAM: Mapping[str, AssetType] = {
    "instance_id": AssetType.EC2,
    "group_id": AssetType.SG,
    "network_acl_id": AssetType.NACL,
    "volume_id": AssetType.EBS,
}

# 관계 유형이 곧 상대 자산의 유형이다(api/assets.py RelationType 주석). 아래 둘만 두는
# 것은 골든 인벤토리가 SG와 EBS 볼륨만 담고 있어서다 — NACL·ASG·Launch Template·
# ALB Target Group이 골든에 추가되면 services/collector.py의 파생 규칙을 함께 옮긴다.
_RELATION_TARGET_TYPE: Mapping[RelationType, AssetType] = {
    RelationType.SECURED_BY: AssetType.SG,
    RelationType.ATTACHED_TO: AssetType.EBS,
}

# Capability에 싣는 한 줄 설명. ADR-0002는 런북 ID 목록만 담고 설명 문구를 두지 않아
# 여기서 정한다(scripts/smoke_finops_graph.py가 쓰던 문구와 같은 결).
_CAPABILITY_PURPOSE: Mapping[RunbookId, str] = {
    RunbookId.RUNBOOK_EC2_RIGHTSIZING: "과대 스펙 EC2 다운사이징",
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING: "고정 대수 EC2를 Auto Scaling 그룹으로 전환",
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED: "미연결 EBS 볼륨 삭제",
}


@dataclass(frozen=True)
class EvalCase:
    """계측 1건. graph_input은 그래프에 그대로 넣고, 나머지는 결과를 읽을 때 쓴다."""

    case_id: str
    graph_input: FinOpsGraphInput
    verdict: Verdict
    skip_reason_code: Optional[SkipReasonCode]
    purpose: str


def _arn(resource_type: str, resource_id: str, region: str, account_id: str) -> str:
    """services/collector.py의 _arn과 같은 형식이다.

    가드레일 ③ ARN Match가 이 문자열을 그대로 비교하므로, 형식이 갈리면 골든 자산이
    조치 대상 밖으로 떨어진다.
    """
    return f"arn:aws:ec2:{region}:{account_id}:{resource_type}/{resource_id}"


def _relationships(ec2: Ec2Asset, inventory: AssetInventory) -> list[dict[str, str]]:
    """SECURED_BY(SG)·ATTACHED_TO(EBS) 파생 — services/collector.py의 순서 그대로."""
    items = [
        {
            "relation_type": RelationType.SECURED_BY.value,
            "target_arn": _arn("security-group", sg_id, inventory.region, inventory.account_id),
        }
        for sg_id in ec2.security_group_ids
    ]
    items.extend(
        {"relation_type": RelationType.ATTACHED_TO.value, "target_arn": volume.arn}
        for volume in inventory.ebs_volumes
        if ec2.instance_id in volume.attached_instance_ids
    )
    return items


def _capabilities(reachable: set[AssetType]) -> list[dict[str, Any]]:
    """대상 자산이 실제로 있는 FinOps 런북만 싣는다.

    AI 추천 7종을 전부 싣지 않는 것은, 대상이 없는 런북을 메뉴에 올리면 모델이 고를 수
    있는 값의 범위가 프로덕션과 달라지기 때문이다. RunbookId 선언 순서를 따라 목록이
    호출마다 흔들리지 않게 한다.
    """
    capabilities: list[dict[str, Any]] = []
    for runbook_id in RunbookId:
        if RUNBOOK_DOMAIN_BY_ID.get(runbook_id.value) is not RunbookDomain.FINOPS:
            continue
        target_type = _ASSET_TYPE_BY_RESOURCE_ID_PARAM.get(RESOURCE_ID_PARAM.get(runbook_id, ""))
        if target_type is None or target_type not in reachable:
            continue
        capabilities.append(
            {
                "runbook_id": runbook_id.value,
                "purpose": _CAPABILITY_PURPOSE[runbook_id],
                "allowed_target_asset_types": [target_type.value],
            }
        )
    return capabilities


def finops_cases(inventory: AssetInventory, expected: Mapping[str, Any]) -> list[EvalCase]:
    """인벤토리 1개 + 그 정답지 1개 → 계측 케이스 목록.

    정답지에 없는 자산이 인벤토리에 있으면 세운다 — 조용히 건너뛰면 고정 세트가 몇 건인지
    모르는 채로 줄어든다.
    """
    evaluations = {item["asset_arn"]: item for item in expected["evaluations"]}
    # 수집 회차 식별자. 프로덕션은 실행 시점에 발급하지만 여기서는 인벤토리의 수집일에서
    # 파생한다 — 회차마다 값이 다르면서 몇 번을 만들어도 같아야 하기 때문이다.
    collection_run_id = f"run-golden-{inventory.collected_at:%Y%m%d}"
    window_start = inventory.collected_at - timedelta(days=inventory.lookback_days)

    cases: list[EvalCase] = []
    for ec2 in inventory.ec2_instances:
        evaluation = evaluations.get(ec2.arn)
        if evaluation is None:
            raise ValueError(f"정답지에 없는 자산입니다: {ec2.arn}")

        verdict = Verdict(evaluation["verdict"])
        if verdict not in _INCIDENT_VERDICTS:
            continue

        summary = ec2.metric_summary
        engine_verdict, _, health = evaluate_ec2(
            summary.cpu_avg, summary.cpu_max, summary.cpu_datapoints, ec2.tags
        )
        # 정답지와 현재 판정 코드가 갈리면 세운다. 어긋난 채로 두면 "COST_CANDIDATE라서
        # 골랐다"는 고정 세트의 전제가 사실이 아니게 된다.
        if engine_verdict.value != verdict.value:
            raise ValueError(
                f"{ec2.arn}: 정답지 {verdict.value}와 rule_engine {engine_verdict.value}가 다릅니다"
            )
        health_score = int(round(health)) if health is not None else None

        case_id = evaluation["case_id"]
        relationships = _relationships(ec2, inventory)
        reachable = {AssetType.EC2} | {
            _RELATION_TARGET_TYPE[RelationType(item["relation_type"])] for item in relationships
        }
        rule_evaluation = {
            "asset_arn": ec2.arn,
            "collection_run_id": collection_run_id,
            "evaluation_status": evaluation["evaluation_status"],
            "verdict": verdict.value,
            "health_score": health_score,
            "skip_reason_code": evaluation["skip_reason_code"],
            # services/rule_engine.py가 실제로 넣는 문자열이다. 메트릭 수치를 여기 풀어
            # 쓰면 모델이 프로덕션에서는 받지 못할 숫자를 받는다 — 수치는 아래 METRIC
            # 근거로만 들어간다.
            "reason": f"{AssetType.EC2.value} rule evaluation: verdict={verdict.value}",
            "evaluated_at": inventory.collected_at,
        }

        graph_input = FinOpsGraphInput.model_validate(
            {
                "domain": "FINOPS",
                "incident_id": f"inc-golden-{case_id.lower()}",
                "asset_context": {
                    "arn": ec2.arn,
                    "resource_id": ec2.instance_id,
                    "asset_type": AssetType.EC2.value,
                    "resource_role": "PRIMARY",
                    "name": ec2.name,
                    "account_id": inventory.account_id,
                    "region": inventory.region,
                    "state": ec2.state,
                    # services/collector.py가 만드는 ec2_spec과 같은 키 구성이다
                    "spec": {
                        "instance_type": ec2.instance_type,
                        "availability_zone": ec2.availability_zone,
                        "vpc_id": ec2.vpc_id,
                        "subnet_id": ec2.subnet_id,
                        "private_ip": ec2.private_ip,
                        "tags": ec2.tags or {},
                    },
                    "relationships": relationships,
                    "evaluation_status": evaluation["evaluation_status"],
                    "health_score": health_score,
                    "verdict": verdict.value,
                    "skip_reason_code": evaluation["skip_reason_code"],
                    "collected_at": inventory.collected_at,
                },
                "rule_evaluation": rule_evaluation,
                "evidences": [
                    {
                        "evidence_id": f"ev-{case_id.lower()}-rule",
                        "evidence_type": "RULE",
                        "content": {"evaluation": rule_evaluation},
                    },
                    {
                        "evidence_id": f"ev-{case_id.lower()}-metric",
                        "evidence_type": "METRIC",
                        "content": {
                            "metric_name": MetricName.CPU_UTILIZATION.value,
                            "window_start": window_start,
                            "window_end": inventory.collected_at,
                            "summary": summary.model_dump(mode="json"),
                        },
                    },
                ],
                "capabilities": _capabilities(reachable),
            }
        )

        cases.append(
            EvalCase(
                case_id=case_id,
                graph_input=graph_input,
                verdict=verdict,
                skip_reason_code=(
                    SkipReasonCode(evaluation["skip_reason_code"])
                    if evaluation["skip_reason_code"]
                    else None
                ),
                purpose=evaluation["purpose"],
            )
        )
    return cases


def input_fingerprint(cases: Sequence[EvalCase]) -> str:
    """고정 입력 세트의 지문 — 그래프 입력(자산·판정·근거·capabilities) 전체의 sha256.

    스냅샷에 적어 두고 계측 도구가 대조한다 — CI가 아니다. 골든이 바뀌는 것은 골든 담당의
    일이라 막지 않고, 세트가 달라진 원자료로 이전 판과 짝 비교를 하려 할 때 도구가 알리고
    거절한다(docs/AI_SUMMARY_BASELINE.md §기준선).
    """
    digest = hashlib.sha256()
    for case in cases:
        digest.update(
            json.dumps(
                case.graph_input.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
            ).encode("utf-8")
        )
    return digest.hexdigest()
