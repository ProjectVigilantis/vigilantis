# ==============================================================================
# [파일 설명]
# AI 분석을 기다리는 Incident를 LangGraph 호출로 넘기는 모듈입니다. (Issue #254)
# incident_intake.py가 만든 Incident와 ai/agent.py의 그래프 사이에 서며, 실행 쪽의
# dispatcher.py와 짝입니다 — 그쪽은 접수된 조치를 AWS 실행으로, 이쪽은 만들어진
# Incident를 AI 호출로 넘깁니다.
#
# 계층 경계 — AI 호출 대상 스캔은 이 모듈 하나가 소유합니다. 스캔이 둘이면 같은
# Incident를 두 주체가 선점합니다. 실제 일은 아래로 내려보냅니다.
#   agent_dispatcher → db/repositories/incidents.py  선점(Claim)·결과 저장
#                    → ai/agent.py                   그래프 호출
#
# 한 건이 가는 순서는 아래 하나입니다.
#   1. 스캔      status=ANALYZING · agent_invocation_status=PENDING
#   2. 선점      claim_agent_invocation — PENDING→IN_PROGRESS 조건부 UPDATE 1건.
#                성공한 호출자만 그래프를 부릅니다(ADR-0005 결정 2). 실패는 다른
#                주체가 이미 가져간 것이라 건너뜁니다.
#   3. 입력 빌드 DB에서 읽어 typed snapshot을 만듭니다. Graph Node는 DB를 직접
#                조회하지 않습니다(schemas/agents.py 계약 원칙). **자산 문맥은 최신
#                자산 행이 아니라 Detection 당시 스냅샷입니다** — 아래 별도 항.
#   4. 그래프 호출 **여기서 DB 트랜잭션을 열어 두지 않습니다.** 모델 호출은 초 단위라
#                트랜잭션을 걸치면 그 시간만큼 행 잠금이 살아 있습니다.
#   5. 검증      출력 모델 단독으로 볼 수 없어 계약이 Workflow 몫으로 못 박은 둘을
#                여기서 봅니다(schemas/agents.py 계약 원칙) —
#                ⓐ 후보 evidence_ids ⊆ 입력 Evidence  ⓑ FINOPS의 reviewed_risk_level=null
#   6. 저장      finish_agent_invocation(Terminal 3종)과 후보 저장을 한 트랜잭션에.
#                IN_PROGRESS로 남은 채 프로세스가 죽으면 reset_agent_invocation으로
#                회수하며, 회수 대상 판단은 이 계층 몫입니다(Repository docstring).
#
# **입력 빌드의 불변식 둘. 어느 쪽도 "최신 값을 다시 읽는" 것으로 대신할 수 없습니다.**
#
# ⓐ 최상위 rule_evaluation은 RULE 근거 행에서 읽습니다. (Issue #243)
#    다른 원천에서 읽으면 두 값이 한 글자만 달라도 ai/agent.py의 _incident_payload가
#    중복 제거 조건(완전 일치)을 빗나가, 같은 판정이 모델 입력에 두 번 실립니다.
#    로그도 예외도 없어 드러나지 않습니다 — 근거와 최상위는 같은 객체에서 나옵니다.
# ⓑ 자산 문맥은 Detection 당시 스냅샷입니다. 자산 행은 수집 회차마다 최신 관측으로
#    덮어써지므로(db/repositories/assets.py upsert_asset) 여기서 최신 행을 읽으면
#    **예전 판정 + 최신 자산**이 한 시점인 양 조립됩니다 — t3.xlarge에서 난 저활성
#    판정이 이미 t3.medium으로 줄어든 인스턴스에 붙습니다. 최신 상태를 보는 자리는
#    여기가 아니라 제안이 나온 직후의 가드레일 ④ AWS Dry-Run입니다(precheck —
#    ADR-0007). 그 판정은 실행 시점에 다시 돌지 않고, 실행 직전 대상 자산 재확인은
#    아직 붙지 않았습니다(workflows.py 헤더).
#    스냅샷을 어디에 보존할지는 아직 정해지지 않았습니다(incident_intake.py
#    [남은 작업] 2번). 그 결정이 이 모듈의 선행 조건입니다.
#
# capabilities를 거르는 축은 **대상 자산의 존재 여부 하나**입니다 — 대상이 없는
# 런북을 메뉴에 올리면 모델이 고를 수 있는 값의 범위가 프로덕션과 달라집니다.
# **Registry 도메인으로는 거르지 않습니다.** UNUSED 대상은 미부착 SG와 미부착 EBS
# 볼륨 둘인데(services/rule_engine.py evaluate_sg·evaluate_ebs), SG 쪽 조치인
# RUNBOOK_SG_DELETE_ISOLATED는 Registry에서 SECOPS입니다. 도메인으로 거르면 그
# Incident의 capabilities가 비어 FinOpsGraphInput(min_length=1)을 만들 수 없습니다.
# 두 축은 별개입니다(SSOT §Action Whitelist "분류 축 주의", schemas/intake.py 헤더).
# 계측 하네스의 `_capabilities`(ai/evaluation/cases.py)는 두 축으로 다 거르는데,
# 그쪽 고정 입력 세트가 COST_CANDIDATE(EC2)뿐이라 도메인 축이 결과를 바꾸지 않기
# 때문입니다. 이 자리에서 그대로 가져오면 UNUSED Incident가 통째로 막힙니다.
#
# [남은 작업]
# 1. 본문 구현 — 위 6단계와 회수. 기동 worker 1개를 전제합니다(dispatcher.py 헤더와
#    같은 전제 — ADR-0005가 다중 worker 토폴로지를 별도 결정 대상으로 남겼습니다).
# 2. SecOps 경로 — SecOpsGraphInput을 만들려면 reassess_risk 노드가 필요한데 아직
#    없습니다(ai/agent.py 헤더). 그때까지 SECOPS Incident는 넘기지 않고 남깁니다.
# 3. 후보 저장 이후 — 가드레일 4단계 1회 수행과 EXECUTABLE 전이, ANALYZING →
#    AWAITING_APPROVAL 전이, Medium·Low의 승인 대기 시작(set_agent_wait)의 소유자를
#    정합니다. 가드레일은 그래프 밖이고(ADR-0005 설계 원칙 3) 실행 시점에는 다시
#    부르지 않습니다(workflows.py 헤더).
# 4. 주기 잡 등록 — dispatcher.py의 start_dispatcher와 같은 자리. 설정 키를 새로
#    만들기 전에 DISPATCH_* 3종(config.py)과의 관계를 함께 정합니다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy.orm import Session, sessionmaker

from schemas.agents import AgentGraphInput
from schemas.api.ws import WsEvent

logger = logging.getLogger("vigilantis.agent_dispatcher")

Publish = Callable[[WsEvent], None]


@dataclass
class AgentDispatchReport:
    """스캔 1회 요약 — 로그와 테스트가 읽는 값이다."""

    scanned: int = 0
    claimed: int = 0       # 선점에 성공해 그래프를 부른 Incident
    succeeded: int = 0     # SUCCEEDED — 요약 3줄 + 후보 1개 이상
    no_proposal: int = 0   # NO_PROPOSAL — 요약 3줄 + 후보 0개
    failed: int = 0        # FAILED — 그래프 오류이거나 Workflow 검증에서 걸린 출력
    skipped: int = 0       # 선점 실패(다른 주체가 이미 가져감)
    unsupported: int = 0   # 그래프가 아직 없는 분류(SECOPS)
    errored: int = 0


def build_graph_input(db: Session, incident_id: str) -> AgentGraphInput:
    """Incident 1건 → 그래프 입력 1건. 최상위 rule_evaluation은 RULE 근거 행에서 읽는다."""
    raise NotImplementedError("Issue #254 — 입력 불변식은 파일 헤더 참조")


def dispatch_pending_analysis(
    db: Session, publish: Optional[Publish] = None
) -> AgentDispatchReport:
    """AI 분석 대기 Incident 스캔 1회. **세션 수명은 호출부가 소유한다.**

    목록을 행이 아니라 식별자로만 받아 둔다 — 처리 중에 커밋이 일어나므로 들고 있던
    행 상태는 곧 낡고, 그 값을 믿으면 선점 재확인이 무의미해진다(dispatcher.py와 같다).
    """
    raise NotImplementedError("Issue #254 — 처리 순서는 파일 헤더 참조")


def run_agent_dispatch_cycle(
    session_factory: sessionmaker[Session], publish: Optional[Publish] = None
) -> AgentDispatchReport:
    """주기 잡의 본체이자 수동 호출 진입점 — 스캔 1회에 세션 1개를 쓰고 닫는다."""
    db = session_factory()
    try:
        return dispatch_pending_analysis(db, publish)
    finally:
        db.close()
