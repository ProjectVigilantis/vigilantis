# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# 그래프 입력의 조치 메뉴(capabilities)를 만드는 빌더입니다. (Issue #285)
#
# **메뉴가 곧 조치 공간입니다.** 모델은 여기 실린 Runbook만 고를 수 있고, 벗어난 후보가
# 오면 그 건만 버리는 것이 아니라 호출 전체가 FAILED가 됩니다(ai/agent.py _to_draft).
# 반대로 비어 있으면 FinOpsGraphInput(capabilities min_length=1)을 만들 수 없어 그
# Incident는 AI를 부르지 못합니다. 넓혀도 좁혀도 값이 나가는 자리라 축을 명시합니다.
#
# 거르는 축은 둘입니다. 순서는 RunbookId 선언 순서를 따릅니다 — 목록이 호출마다
# 흔들리면 같은 Incident에 대한 입력이 회차마다 달라져 재현성 계측이 성립하지 않습니다.
#
# ① 런북의 대상 자산 유형 == Incident subject 자산 유형
#    대상이 없는 런북을 메뉴에 올리면 모델이 고를 수 있는 값의 범위가 프로덕션과
#    달라집니다. **관계 자산까지 넓히지 않습니다** — 관계로 딸려오는 자산은 정의상
#    붙어 있는 자산이라(SECURED_BY의 SG·ATTACHED_TO의 EBS), 그 둘에 걸린 FinOps 조치는
#    전부 "미부착이라 지운다"는 조치입니다. services/rule_engine.py의 evaluate_sg는
#    attached is False일 때만, evaluate_ebs는 부착 인스턴스가 없을 때만 UNUSED를 냅니다.
#    부착된 SG·볼륨을 삭제 후보의 대상으로 올리는 것은 그 판정 규칙과 정면으로 어긋납니다.
#
# ② Registry 도메인이 FINOPS이거나, subject 판정이 UNUSED
#    Incident 분류 축(FINOPS·SECOPS)과 Runbook Registry 도메인 축은 별개입니다
#    (SSOT §Action Whitelist "분류 축 주의", schemas/intake.py 헤더). 미부착 SG는
#    위협 이벤트가 없어 FINOPS Incident인데 그 조치인 RUNBOOK_SG_DELETE_ISOLATED는
#    Registry에서 SECOPS라, 도메인만으로 거르면 메뉴가 비어 통째로 막힙니다.
#    그렇다고 도메인 축을 빼 버리면 RUNBOOK_EC2_ISOLATE(SECOPS·대상 EC2)가 저활성 EC2
#    Incident마다 실려, 관제자가 비용 카드에서 격리 제안을 보게 됩니다. 그 후보는 뒤에서도
#    걸러지지 않습니다 — 가드레일 ② Action Whitelist는 AI 추천 7종을 통과시키고,
#    ③ ARN Match는 수집된 자산인지만 보며, ④ AWS Dry-Run이 요구하는 isolation_group_id는
#    채울 원천이 저장소에 없어 거절이 아니라 예외로 끝납니다(ADR-0007 §1 호출 규약은
#    배선 오류를 거절로 기록하지 않습니다).
#    UNUSED를 예외로 두는 근거: SECOPS 조치가 FinOps Incident에서 정당한 경우는 대상이
#    더는 쓰이지 않는 자산일 때뿐이고, 그때 제거는 위협 대응이 아니라 미사용 자원 정리입니다.
#
# 계측 하네스(ai/evaluation/cases.py)도 이 빌더를 씁니다 — 빌더가 두 벌이면 계측이
# 재는 입력과 프로덕션이 만드는 입력이 갈립니다.
# ==============================================================================

from __future__ import annotations

import logging
from typing import Mapping, Optional

from schemas.agents import RunbookCapability
from schemas.api.assets import AssetType, Verdict
from schemas.runbook_parameters import RESOURCE_ID_PARAM
from schemas.runbooks import (
    AI_RECOMMENDABLE_RUNBOOK_IDS,
    RUNBOOK_DOMAIN_BY_ID,
    RunbookDomain,
    RunbookId,
)

logger = logging.getLogger("vigilantis.ai")

# RESOURCE_ID_PARAM(runbook_parameters.py)이 런북마다 "target_arn이 가리켜야 하는
# 자원 ID"를 정하고 있어, 그 파라미터 이름이 곧 대상 자산 유형이다.
ASSET_TYPE_BY_RESOURCE_ID_PARAM: Mapping[str, AssetType] = {
    "instance_id": AssetType.EC2,
    "group_id": AssetType.SG,
    "network_acl_id": AssetType.NACL,
    "volume_id": AssetType.EBS,
}

# Capability에 싣는 한 줄 설명. ADR-0002·SSOT §Action Whitelist 표는 런북 ID와 위험도·
# 승인 축만 담고 설명 문구를 두지 않아 여기서 정한다. 문구의 근거는 각 런북이 실제로
# 부르는 AWS 작업이다(services/aws/executor.py의 precheck·실행 핸들러).
#
# **AI 추천 7종을 전부 채운다.** 지금 필터가 내보내지 못하는 셋(격리·NACL 2종)까지 두는
# 것은, 필터를 고쳤을 때 빠진 항목이 런타임 KeyError로 나타나지 않게 하기 위해서다 —
# 이 빌더는 주기 스캔 안에서 도므로 그 예외는 Incident 1건을 조용히 멈춰 세운다.
CAPABILITY_PURPOSE: Mapping[RunbookId, str] = {
    RunbookId.RUNBOOK_EC2_ISOLATE: "EC2를 격리용 보안 그룹으로 바꾸고 로드밸런서 대상에서 제외",
    RunbookId.RUNBOOK_NACL_ADD_DENY: "출발지 CIDR을 NACL 거부 규칙으로 차단",
    RunbookId.RUNBOOK_NACL_RESTORE: "NACL 거부 규칙 제거",
    RunbookId.RUNBOOK_SG_DELETE_ISOLATED: "미부착 보안 그룹 삭제",
    RunbookId.RUNBOOK_EC2_RIGHTSIZING: "과대 스펙 EC2 다운사이징",
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING: "고정 대수 EC2를 Auto Scaling 그룹으로 전환",
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED: "미연결 EBS 볼륨 삭제",
}


def target_asset_type(runbook_id: RunbookId) -> Optional[AssetType]:
    """런북이 조치할 자산 유형. 대상 자원 ID 파라미터가 없는 런북은 None이다."""
    return ASSET_TYPE_BY_RESOURCE_ID_PARAM.get(RESOURCE_ID_PARAM.get(runbook_id, ""))


def build_capabilities(
    *, asset_type: AssetType, verdict: Verdict
) -> list[RunbookCapability]:
    """Incident subject 자산 1건 → 그 Incident의 조치 메뉴. 축 둘은 파일 헤더 참조.

    빈 목록을 돌려줄 수 있다 — 판정이 조치 가능하다고 본 자산에 걸 조치가 메뉴에 하나도
    없는 경우다(조치 공간의 공백). 그 처분은 호출부가 정한다: FinOpsGraphInput은 빈
    capabilities를 거절하므로 입력을 만들 수 없고, agent_dispatcher는 그것을 분석 실패로
    닫는다. 여기서 임의의 런북을 채워 넣지 않는다 — 대상이 없는 조치를 메뉴에 올리는 것이
    ①이 막으려는 것 그대로다.
    """
    capabilities: list[RunbookCapability] = []
    for runbook_id in RunbookId:
        if runbook_id.value not in AI_RECOMMENDABLE_RUNBOOK_IDS:
            continue
        # ① 대상 자산 유형이 subject와 같은가
        if target_asset_type(runbook_id) is not asset_type:
            continue
        # ② FinOps 조치이거나, 미사용 자산 정리인가
        is_finops = RUNBOOK_DOMAIN_BY_ID.get(runbook_id.value) is RunbookDomain.FINOPS
        if not is_finops and verdict is not Verdict.UNUSED:
            continue
        capabilities.append(
            RunbookCapability(
                runbook_id=runbook_id,
                purpose=CAPABILITY_PURPOSE[runbook_id],
                allowed_target_asset_types=[asset_type],
            )
        )
    if not capabilities:
        logger.warning(
            "empty_capabilities",
            extra={"asset_type": asset_type.value, "verdict": verdict.value},
        )
    return capabilities
