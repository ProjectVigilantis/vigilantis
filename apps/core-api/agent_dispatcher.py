# ==============================================================================
# [파일 설명]
# AI 분석을 기다리는 Incident를 LangGraph 호출로 넘기는 모듈입니다. (Issue #254·#285)
# incident_intake.py가 만든 Incident와 ai/agent.py의 그래프 사이에 서며, 실행 쪽의
# dispatcher.py와 짝입니다 — 그쪽은 접수된 조치를 AWS 실행으로, 이쪽은 만들어진
# Incident를 AI 호출로 넘깁니다.
#
# 계층 경계 — AI 호출 대상 스캔은 이 모듈 하나가 소유합니다. 스캔이 둘이면 같은
# Incident를 두 주체가 선점합니다. 실제 일은 아래로 내려보냅니다.
#   agent_dispatcher → db/repositories/incidents.py  스캔·선점·회수
#                    → ai/capabilities.py            조치 메뉴 조립
#                    → ai/agent.py                   그래프 호출
#                    → workflows.py                  후보 저장·가드레일·상태 전이
# 마지막 줄이 dispatcher.py와 같은 경계입니다 — 상태 전이와 트랜잭션은 그 계층이
# 소유하고, 이 모듈은 무엇을 언제 넘길지만 정합니다.
#
# 한 건이 가는 순서는 아래 하나입니다.
#   1. 스캔      status=ANALYZING · agent_invocation_status=PENDING
#   2. 선점      claim_agent_invocation — PENDING→IN_PROGRESS 조건부 UPDATE 1건.
#                성공한 호출자만 그래프를 부릅니다(ADR-0005 결정 2). 실패는 다른
#                주체가 이미 가져간 것이라 건너뜁니다. **선점에 성공하면 곧바로
#                commit해 행 잠금을 놓습니다** — Repository는 commit하지 않으므로
#                (db/repositories/incidents.py) 여기서 끊지 않으면 선점한 행이 잠긴
#                채 4번의 모델 호출 시간을 통과합니다. 4번이 트랜잭션 밖에서 도는
#                전제가 이 commit입니다.
#   3. 입력 빌드 DB에서 읽어 typed snapshot을 만듭니다. Graph Node는 DB를 직접
#                조회하지 않습니다(schemas/agents.py 계약 원칙). **자산 문맥은 최신
#                자산 행이 아니라 Detection 당시 스냅샷입니다** — 아래 별도 항.
#                **조립을 마치면 읽기 트랜잭션도 닫습니다.** 2번에서 commit했더라도
#                여기서 조회를 하면 같은 Session이 트랜잭션을 다시 엽니다(SQLAlchemy
#                autobegin). 조립 후 rollback으로 닫습니다.
#   4. 그래프 호출 **트랜잭션이 닫힌 상태에서만 부릅니다.** 모델 호출은 초 단위라,
#                걸친 채로 부르면 커넥션 1개가 그 시간만큼 트랜잭션에 묶인 채
#                남습니다(선점 행 잠금 자체는 2번의 commit이 이미 놓았습니다).
#   5. 검증      출력 모델 단독으로 볼 수 없어 계약이 Workflow 몫으로 못 박은 둘과,
#                저장 앞단에서만 볼 수 있는 하나를 여기서 봅니다(schemas/agents.py 계약
#                원칙) —
#                ⓐ 후보 evidence_ids ⊆ 입력 Evidence  ⓑ FINOPS의 reviewed_risk_level=null
#                ⓒ PostgreSQL text·jsonb가 담을 수 있는 값인가(NUL 거절)
#                어기면 후보 하나가 아니라 **출력 전체를 FAILED로** 바꿉니다. 어긋난
#                출력에서 성한 후보만 골라 남기면, 서버가 하지 않은 판단이 관제 화면에
#                남습니다(ai/agent.py _validate_output_contract와 같은 처분).
#   6. 저장      workflows.record_agent_analysis 한 번. 후보 적재·가드레일 4단계 1회·
#                Terminal 기록·ANALYZING 이탈이 그 안에서 한 트랜잭션으로 끝납니다.
#                IN_PROGRESS로 남은 채 프로세스가 죽으면 reset_agent_invocation으로
#                회수하며, 회수 대상 판단은 이 계층 몫입니다(Repository docstring).
#
# **FinOps 입력 빌드의 불변식 둘. 최신 값을 다시 읽는 것으로 대신할 수 없다.**
# ⓐ 최상위 rule_evaluation은 RULE 근거 행에서 읽는다(#243). 근거와 다른 원천이면
#    _incident_payload의 완전 일치 중복 제거를 빗나가 같은 판정이 모델에 두 번 실린다.
# ⓑ 자산 문맥은 ASSET 근거 행의 Detection 스냅샷에서 읽는다(#265). 자산 테이블은
#    수집마다 덮어쓰므로 최신 행을 쓰면 예전 판정과 최신 자산이 한 시점처럼 조립된다.
#    예를 들어 t3.xlarge의 저활성 판정이 이미 t3.medium인 자산에 붙을 수 있다.
#    현재 AWS 상태를 보는 곳은 제안 직후 가드레일 ④ precheck다(ADR-0007).
#    실행 직전 재확인 여부는 workflows.py의 실행 계약을 따른다.
#    ASSET은 asset_context로 전달하며 evidences에 중복해서 싣지 않는다. 후보가 ASSET
#    근거를 인용하지 않는 보장은 그래프 이후 5번 ⓐ의 evidence_ids 대조에서 성립한다.
# 조치 메뉴는 ai/capabilities.py의 빌더를 서비스와 계측이 함께 써 입력 차이를 막는다.
#
# FinOps는 그래프 오류·출력 검증 위반·NO_PROPOSAL·가드레일 전부 거절을 FAILED로 닫는다.
# 뒤 둘은 관제자에게 보여 줄 실행 가능한 조치가 없어 요약을 로그로만 남긴다(#285).
# 입력 불가도 같은 처분이다. 근거 누락·빈 메뉴를 PENDING으로 남기면 다음 스캔에서도
# 같은 입력으로 실패를 반복하기 때문이다(_GraphInputUnavailable).
#
# SecOps는 고정 THREAT 근거·초기 판정과 분석 시점 수집 자산을 조립한다.
# 후보는 입력 근거·조치 대상·저장 가능성을 검증한 뒤 Workflow에 넘긴다.
# SecOps의 위험도 재평가는 초기 위험도·사유·대응 모드를 덮어쓰지 않는다.
#
# SecOps도 FAILED에서는 요약·재평가를 비운다. 선행 실행이 진행 중이면 ACTION_IN_PROGRESS,
# 성공·원복 완료 실행이 있고 제안이 없으면 AWAITING_CLOSURE다. 그 밖에 실행 가능한
# 제안이 없으면 FAILED다(workflows.record_agent_analysis).
# 승인 대기 시각·60초 기한을 기록하되 자동 격리 발동 엔진은 이 모듈 범위 밖이다.
#
# 기동 worker 개수는 dispatcher.py와 같은 전제입니다 — worker 1개. 선점의 잠금 수명이
# 2번의 commit에서 끝나므로, "그래프 진입은 한 주체뿐"이라는 보장은 그 전제 + 스캔
# 비중첩(max_instances=1)에서 성립합니다. ADR-0005가 다중 worker 토폴로지를 별도 결정
# 대상으로 남겼고, 그때는 commit을 넘어 사는 선점(lease 컬럼 등)이 함께 와야 합니다.
# ==============================================================================

from __future__ import annotations

import logging
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker
from pydantic import ValidationError

from schemas.agents import (
    AgentEvidenceInput,
    AgentGraphInput,
    AgentGraphOutput,
    FinOpsGraphInput,
    SecOpsGraphInput,
    AgentExecutionContext,
)
from schemas.api.assets import AssetType, RelationType
from schemas.api.actions import ExecutionStatus
from schemas.api.incidents import IncidentCategory
from schemas.events import InitialRiskEvaluationResult, expected_mode_for
from schemas.runbooks import RunbookId, TriggerSource
from schemas.api.ws import WsEvent, WsEventType
from schemas.evidence import EvidenceItem, EvidenceType
from schemas.incidents import AgentInvocationStatus

import workflows
from ai.agent import run_finops_graph, run_secops_graph, FINOPS_MODEL_CALLS, SECOPS_MODEL_CALLS
from ai.capabilities import (
    build_finops_capabilities,
    build_secops_capabilities,
    secops_action_targets,
)
from asset_mapping import to_asset_item
from ai.model_client import AIModelClient
from ai.openai_client import build_openai_model_client
from config import Settings, get_settings
from db import mappers
from db.repositories import incidents as incidents_repo
from db.repositories import assets as assets_repo, executions as executions_repo
from db.session import get_session_factory
from realtime import incident_event

logger = logging.getLogger("vigilantis.agent_dispatcher")

Publish = Callable[[WsEvent], None]

JOB_ID = "agent_dispatch"

# FinOps 2회·SecOps 3회 중 최대값으로 회수 상한을 계산한다.
_MODEL_CALLS_PER_GRAPH = max(FINOPS_MODEL_CALLS, SECOPS_MODEL_CALLS)


class _UnsupportedIncident(Exception):
    """그래프가 아직 없는 분류 — 선점하지 않고 남긴다."""


class _GraphInputUnavailable(Exception):
    """그래프 입력을 만들 수 없다 — 다시 시도해도 같으므로 분석 실패로 닫는다."""


@dataclass
class AgentDispatchReport:
    """스캔 1회 요약 — 로그와 테스트가 읽는 값이다."""

    scanned: int = 0
    claimed: int = 0       # 선점에 성공해 그래프를 부른 Incident
    succeeded: int = 0     # SUCCEEDED — 요약 3줄 + 후보 1개 이상
    no_proposal: int = 0   # NO_PROPOSAL — 요약 3줄 + 후보 0개
    failed: int = 0        # FAILED — 그래프 오류이거나 Workflow 검증에서 걸린 출력
    skipped: int = 0       # 선점 실패(다른 주체가 이미 가져감)
    unsupported: int = 0   # 지원하지 않는 분류
    reclaimed: int = 0     # 상한을 넘겨 PENDING으로 되돌린 IN_PROGRESS Claim
    errored: int = 0


# ------------------------------------------------------------------------------
# 회수 — IN_PROGRESS로 남은 Claim
# ------------------------------------------------------------------------------


def stale_claim_ceiling_seconds(settings: Optional[Settings] = None) -> float:
    """IN_PROGRESS Claim을 고아로 볼 시간 상한. 새 설정 키를 만들지 않고 파생한다.

    **모델 호출 1회의 최악값 × 그래프가 부르는 호출 수**다. 최악값은 매 시도가
    제한시간을 다 쓰고(OPENAI_TIMEOUT_SECONDS × OPENAI_MAX_ATTEMPTS) 시도 사이마다
    서버가 지시한 최대 대기를 따르는 경우다(OPENAI_MAX_RETRY_AFTER_SECONDS ×
    (시도 수 − 1) — ai/openai_client.py _retry_delay는 상한 이내의 Retry-After를
    backoff보다 우선한다). 기본값으로 (30×3 + 60×2) × 3 = 630초다.

    **짧게 잡으면 안 된다.** 상한이 실제 최악값보다 짧으면 아직 살아 있는 호출이
    PENDING으로 되돌아가 같은 Incident에 과금되는 그래프 호출이 한 번 더 나가고,
    원래 호출의 finish_agent_invocation은 조건 불일치로 실패한다. 반대로 길게 잡을 때의
    대가는 죽은 건의 재시도가 그만큼 늦어지는 것뿐이라 비대칭이다.
    """
    settings = settings or get_settings()
    attempts = settings.OPENAI_MAX_ATTEMPTS
    per_call = (
        settings.OPENAI_TIMEOUT_SECONDS * attempts
        + settings.OPENAI_MAX_RETRY_AFTER_SECONDS * (attempts - 1)
    )
    return per_call * _MODEL_CALLS_PER_GRAPH


def _reclaim_stale_claims(db: Session, report: AgentDispatchReport) -> None:
    """상한을 넘긴 IN_PROGRESS를 PENDING으로 되돌린다. 다음 주기가 다시 집어 간다."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=stale_claim_ceiling_seconds()
    )
    for incident_id in incidents_repo.list_stale_agent_claims(
        db, started_before=cutoff
    ):
        if incidents_repo.reset_agent_invocation(db, incident_id):
            report.reclaimed += 1
            logger.warning("agent_claim_reclaimed", extra={"incident_id": incident_id})
    db.commit()


# ------------------------------------------------------------------------------
# 입력 빌드
# ------------------------------------------------------------------------------


def _sole_evidence(
    evidences: list[EvidenceItem], evidence_type: EvidenceType
) -> EvidenceItem:
    """그 유형의 근거 1건. 0건이거나 2건 이상이면 입력을 만들 수 없다.

    2건 이상을 거절하는 것은 최상위 rule_evaluation·asset_context가 어느 쪽을 비추는지
    정할 근거가 없기 때문이다. Incident 1건은 판정 1건에서 나오므로
    (incident_intake.py 저장 순서) 지금 그 조합이 생길 경로는 없다.
    """
    found = [item for item in evidences if item.evidence_type is evidence_type]
    if len(found) != 1:
        raise _GraphInputUnavailable(
            f"{evidence_type.value} 근거가 {len(found)}건입니다 (1건이어야 합니다)"
        )
    return found[0]


def build_graph_input(db: Session, incident_id: str) -> AgentGraphInput:
    """Incident 1건 → 그래프 입력 1건. 최상위 rule_evaluation은 RULE 근거 행에서 읽는다."""
    incident = incidents_repo.get_incident(db, incident_id)
    if incident is None:
        raise _GraphInputUnavailable(f"Incident를 찾을 수 없습니다: {incident_id}")
    if incident.category is IncidentCategory.SECOPS:
        return _build_secops_input(db, incident)
    if incident.category is not IncidentCategory.FINOPS:
        raise _UnsupportedIncident(incident.category.value)

    evidences = [
        mappers.to_evidence_item(row)
        for row in incidents_repo.list_evidence(db, incident_id)
    ]
    rule_evaluation = _sole_evidence(evidences, EvidenceType.RULE).content.evaluation
    asset = _sole_evidence(evidences, EvidenceType.ASSET).content.asset

    if rule_evaluation.verdict is None:
        raise _GraphInputUnavailable("RULE 근거에 판정이 없습니다")
    capabilities = build_finops_capabilities(
        asset_type=asset.asset_type, verdict=rule_evaluation.verdict
    )
    if not capabilities:
        # 조치 공간의 공백 — 판정이 조치 가능하다고 본 자산에 걸 조치가 메뉴에 없다.
        # 앞단(rule 제외·whitelist)이 고칠 문제이고 이 계층은 실패로 닫는다(Issue #285 범위 밖)
        raise _GraphInputUnavailable(
            f"조치 메뉴가 비었습니다: {asset.asset_type.value}/{rule_evaluation.verdict.value}"
        )

    return FinOpsGraphInput(
        incident_id=incident_id,
        asset_context=asset,
        rule_evaluation=rule_evaluation,
        # ASSET은 asset_context로 이미 들어갔다 — 근거로도 실으면 같은 값이 두 번 간다
        evidences=[
            AgentEvidenceInput(
                evidence_id=item.evidence_id,
                evidence_type=item.evidence_type,
                content=item.content,
            )
            for item in evidences
            if item.evidence_type is not EvidenceType.ASSET
        ],
        capabilities=capabilities,
    )


def _build_secops_input(db: Session, incident) -> SecOpsGraphInput:
    """관측은 고정 THREAT 근거, 자산은 분석 시점의 최신 수집 행에서 읽는다.

    SecOps Intake에는 Detection 자산 스냅샷이 없다. 자산 collected_at을 모델에
    함께 보내 시점을 구분한다. 직접 PROTECTED_BY 관계 중 같은 계정·리전의
    수집된 NACL만 남긴다. SG 역방향·VPC 전체로 대상 범위를 추측하지 않는다.
    """
    try:
        evidences = [mappers.to_evidence_item(row)
                     for row in incidents_repo.list_evidence(db, incident.incident_id)]
        threat = _sole_evidence(evidences, EvidenceType.THREAT)
        event = threat.content.event
        if (event.target_arn != incident.subject_arn
                or event.threat_event_id != incident.threat_event_id):
            raise _GraphInputUnavailable("위협 근거와 Incident 대상이 다릅니다")
        row = assets_repo.get_asset_by_arn(db, incident.subject_arn)
        if row is None:
            raise _GraphInputUnavailable("위협 대상 자산이 수집되지 않았습니다")
        relations = []
        for relation in assets_repo.list_relationships_by_source(db, row.asset_id):
            if relation.relation_type is not RelationType.PROTECTED_BY:
                continue
            target = assets_repo.get_asset_by_arn(db, relation.target_arn)
            if (target is not None and target.asset_type is AssetType.NACL
                    and target.account_id == row.account_id and target.region == row.region):
                relations.append(relation)
        asset = to_asset_item(row, relations, None)
        capabilities = build_secops_capabilities(asset=asset, event_type=event.event_type)
        if not capabilities:
            raise _GraphInputUnavailable("이 위협에 제공할 조치·조회 의존성이 없습니다")
        # response_mode는 이후 타이머가 바꿀 수 있다. 초기 입력에는 초기 위험도에
        # 대응하는 원래 모드를 복원하고 현재 Incident 대응 모드는 수정하지 않는다.
        initial = InitialRiskEvaluationResult(
            threat_event_id=event.threat_event_id,
            initial_risk_level=incident.initial_risk_level,
            response_mode=expected_mode_for(incident.initial_risk_level),
            reason_codes=incident.initial_risk_reason_codes,
        )
        prior = [execution for execution in executions_repo.list_by_incident(db, incident.incident_id)
                 if execution.trigger_source is TriggerSource.PRE_MITIGATION_0_5S
                 and execution.runbook_id in (RunbookId.RUNBOOK_EC2_ISOLATE,
                                              RunbookId.RUNBOOK_NACL_ADD_DENY)]
        isolation = None
        if prior:
            execution = prior[-1]
            isolation = AgentExecutionContext(
                execution_id=execution.execution_id, runbook_id=execution.runbook_id,
                status=execution.status,
                # 실패·부분 적용은 대상과 영향을 동일시하지 않는다. 성공만 확정한다.
                affected_arns=([execution.target_arn]
                               if execution.status is ExecutionStatus.SUCCESS else []),
                result_summary=execution.error_summary or None,
            )
        return SecOpsGraphInput(
            incident_id=incident.incident_id, asset_context=asset, initial_risk=initial,
            evidences=[AgentEvidenceInput(evidence_id=threat.evidence_id,
                                         evidence_type=threat.evidence_type,
                                         content=threat.content)],
            isolation_execution=isolation, capabilities=capabilities,
        )
    except ValidationError as exc:
        raise _GraphInputUnavailable("저장된 SecOps 입력이 계약과 다릅니다") from exc


# ------------------------------------------------------------------------------
# 검증 — 계약이 Workflow 몫으로 못 박은 둘
# ------------------------------------------------------------------------------


def _unstorable_field(output: AgentGraphOutput) -> Optional[str]:
    """PostgreSQL text·jsonb가 담지 못하는 값이 실린 자리. 없으면 None.

    NUL(0x00)은 text·jsonb 어느 쪽에도 담기지 않아 저장이 **거절이 아니라 예외**로 끝난다.
    가드레일 ① Schema Check가 같은 제약을 갖고 있지만(ai/guardrails.py _reject_nul) 그쪽은
    명령이 AWS로 나가기 전 관문이고, 요약 3줄은 아예 보지 않는다. 저장이 먼저 일어나므로
    저장 앞단에도 관문이 필요하다 — 두 관문은 지키는 자산이 다르다.

    막지 않으면 출력이 DB에 닿는 순간 DataError로 터지고, 그 건은 ANALYZING·IN_PROGRESS에
    남아 고아 회수를 거쳐 **같은 출력을 다시 받는다**(모델 호출만 되풀이된다).
    """
    for index, line in enumerate(output.summary_lines):
        if "\x00" in line:
            return f"요약 {index + 1}번째 줄"
    for candidate in output.candidates:
        if "\x00" in candidate.target_arn:
            return f"{candidate.runbook_id.value} 후보의 target_arn"
        if any("\x00" in value for value in candidate.evidence_ids):
            return f"{candidate.runbook_id.value} 후보의 evidence_ids"
        if any(
            isinstance(value, str) and "\x00" in value
            for value in candidate.parameters.model_dump().values()
        ):
            return f"{candidate.runbook_id.value} 후보의 parameters"
    return None


def _contract_violation(
    graph_input: AgentGraphInput, output: AgentGraphOutput
) -> Optional[str]:
    """ⓐ·ⓑ·ⓒ 위반 사유 한 줄. 위반이 없으면 None."""
    if (
        graph_input.domain is IncidentCategory.FINOPS
        and output.reviewed_risk_level is not None
    ):
        return "FINOPS 출력에 reviewed_risk_level이 실렸습니다"

    if (graph_input.domain is IncidentCategory.SECOPS
            and output.invocation_status is not AgentInvocationStatus.FAILED
            and output.reviewed_risk_level is None):
        return "SECOPS 분석 결과에 재평가 위험도가 없습니다"

    offered = {item.evidence_id for item in graph_input.evidences}
    for candidate in output.candidates:
        unknown = sorted(set(candidate.evidence_ids) - offered)
        if unknown:
            return (
                f"{candidate.runbook_id.value} 후보가 입력 밖 evidence_id를 인용했습니다: "
                f"{unknown}"
            )

    if isinstance(graph_input, SecOpsGraphInput):
        targets = secops_action_targets(graph_input.asset_context)
        menu = {item.runbook_id for item in graph_input.capabilities}
        for candidate in output.candidates:
            if (candidate.runbook_id not in menu or candidate.target_arn not in
                    targets.get(candidate.runbook_id.value, [])):
                return "SECOPS 후보의 조치·대상이 입력 메뉴 밖입니다"
            if candidate.runbook_id is RunbookId.RUNBOOK_NACL_ADD_DENY:
                threats = [item.content.event for item in graph_input.evidences
                           if item.evidence_type is EvidenceType.THREAT
                           and item.evidence_id in candidate.evidence_ids]
                try:
                    source = ipaddress.ip_address(threats[0].payload.source_ip)
                    network = ipaddress.ip_network(candidate.parameters.cidr_block)
                    if (len(threats) != 1 or network.num_addresses != 1
                            or network.network_address != source
                            or candidate.parameters.protocol != "tcp"):
                        return "SSH 차단 파라미터가 인용한 위협 근거와 다릅니다"
                except (ValueError, AttributeError, IndexError):
                    return "SSH 차단의 출발지 근거를 확인할 수 없습니다"

    unstorable = _unstorable_field(output)
    if unstorable is not None:
        return f"저장할 수 없는 문자(NUL)가 있습니다: {unstorable}"
    return None


def _verified_output(
    graph_input: AgentGraphInput, output: AgentGraphOutput, incident_id: str
) -> AgentGraphOutput:
    violation = _contract_violation(graph_input, output)
    if violation is None:
        return output
    logger.warning(
        "agent_output_contract_violation",
        extra={"incident_id": incident_id, "violation": violation},
    )
    return AgentGraphOutput(invocation_status=AgentInvocationStatus.FAILED)


# ------------------------------------------------------------------------------
# 스캔
# ------------------------------------------------------------------------------


_TERMINAL_COUNTER = {
    AgentInvocationStatus.SUCCEEDED: "succeeded",
    AgentInvocationStatus.NO_PROPOSAL: "no_proposal",
    AgentInvocationStatus.FAILED: "failed",
}


def _dispatch_one(
    db: Session,
    incident_id: str,
    client: AIModelClient,
    publish: Optional[Publish],
    report: AgentDispatchReport,
) -> None:
    try:
        if not incidents_repo.claim_agent_invocation(
            db, incident_id, started_at=datetime.now(timezone.utc)
        ):
            # 다른 주체가 이미 가져갔다 — 조건부 UPDATE가 걸러 낸 정상 경로다
            db.rollback()
            report.skipped += 1
            return
        # 선점 직후 commit — 여기서 끊지 않으면 모델 호출 시간 내내 행이 잠긴다
        db.commit()
        report.claimed += 1

        try:
            graph_input = build_graph_input(db, incident_id)
        except _GraphInputUnavailable as exc:
            logger.warning(
                "agent_graph_input_unavailable",
                extra={"incident_id": incident_id, "detail": str(exc)},
            )
            db.rollback()
            graph_input = None

        if graph_input is None:
            output = AgentGraphOutput(invocation_status=AgentInvocationStatus.FAILED)
        else:
            # 읽기 트랜잭션을 닫는다 — autobegin으로 다시 열린 것을 여기서 끊어야
            # 그래프 호출이 트랜잭션 밖에서 돈다
            db.rollback()
            output = _verified_output(
                graph_input,
                (run_secops_graph(graph_input, client=client)
                 if isinstance(graph_input, SecOpsGraphInput)
                 else run_finops_graph(graph_input, client=client)),
                incident_id,
            )

        outcome = workflows.record_agent_analysis(db, incident_id, output)
        counter = _TERMINAL_COUNTER[output.invocation_status]
        setattr(report, counter, getattr(report, counter) + 1)
        if publish is not None:
            publish(
                incident_event(
                    WsEventType.INCIDENT_UPDATED,
                    incident_id=incident_id,
                    occurred_at=outcome.occurred_at,
                )
            )
    except Exception:  # noqa: BLE001 — 한 건의 실패가 스캔 전체를 멈추지 않는다
        # 선점 이후에 터졌다면 IN_PROGRESS가 남는다. 여기서 되돌리지 않는 것은, 그래프를
        # 이미 불렀는지 알 수 없어 되돌리면 과금되는 호출이 다음 주기에 한 번 더 나갈 수
        # 있기 때문이다. 상한을 넘긴 뒤 회수가 집어 간다(_reclaim_stale_claims)
        logger.exception("agent_dispatch_failed", extra={"incident_id": incident_id})
        db.rollback()
        report.errored += 1


def dispatch_pending_analysis(
    db: Session, publish: Optional[Publish] = None, *, client: Optional[AIModelClient] = None
) -> AgentDispatchReport:
    """AI 분석 대기 Incident 스캔 1회. **세션 수명은 호출부가 소유한다.**

    목록을 행이 아니라 식별자로만 받아 둔다 — 처리 중에 커밋이 일어나므로 들고 있던
    행 상태는 곧 낡고, 그 값을 믿으면 선점 재확인이 무의미해진다(dispatcher.py와 같다).

    모델 클라이언트는 대상이 있을 때만 만든다. 키 없이도 앱이 뜨는 것이 현 설정 계약이라
    (config.py OPENAI_API_KEY), 대상이 0건인 주기까지 키를 요구하면 키 없는 환경에서
    스캔이 매번 예외로 끝난다.
    """
    report = AgentDispatchReport()
    _reclaim_stale_claims(db, report)

    scanned = incidents_repo.list_pending_agent_analysis(db)
    report.scanned = len(scanned)
    # 지원하는 도메인은 공통 선점·회수 경로를 사용한다.
    dispatchable = [
        incident_id
        for incident_id, category in scanned
        if category in (IncidentCategory.FINOPS, IncidentCategory.SECOPS)
    ]
    report.unsupported = len(scanned) - len(dispatchable)
    if not dispatchable:
        logger.info("agent_dispatch_cycle_done", extra=vars(report))
        return report

    if client is None:
        try:
            client = build_openai_model_client()
        except Exception:  # noqa: BLE001 — 키 누락·설정 오류
            logger.exception("agent_model_client_unavailable")
            report.errored = len(dispatchable)
            return report

    for incident_id in dispatchable:
        _dispatch_one(db, incident_id, client, publish, report)
    logger.info("agent_dispatch_cycle_done", extra=vars(report))
    return report


def run_agent_dispatch_cycle(
    session_factory: sessionmaker[Session], publish: Optional[Publish] = None
) -> AgentDispatchReport:
    """주기 잡의 본체이자 수동 호출 진입점 — 스캔 1회에 세션 1개를 쓰고 닫는다."""
    db = session_factory()
    try:
        return dispatch_pending_analysis(db, publish)
    finally:
        db.close()


def start_agent_dispatcher(publish: Optional[Publish] = None) -> Optional[AsyncIOScheduler]:
    """main의 lifespan에서 기동한다 — 스캔 잡 1개를 등록·기동해 반환한다.

    잡을 겹쳐 돌리지 않는다(max_instances=1). 스캔이 둘이면 같은 Incident를 두 주체가
    선점하고, 그것이 이 모듈이 스캔을 독점하는 이유다(파일 헤더).

    스위치는 실행 디스패치와 DISPATCH_ENABLED를 공유한다 — 테스트가 앱을 띄울 때마다
    스캔이 돌지 않게 하는 목적이 같고, 이쪽은 거기에 더해 스캔 1회가 과금되는 모델
    호출을 낸다(PR #236 리뷰).
    """
    settings = get_settings()
    if not settings.DISPATCH_ENABLED:
        logger.info("agent dispatcher disabled: DISPATCH_ENABLED=false")
        return None
    interval = settings.AGENT_DISPATCH_INTERVAL_SECONDS
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: run_agent_dispatch_cycle(get_session_factory(), publish),
        trigger=IntervalTrigger(seconds=interval),
        id=JOB_ID,
        name="AI 분석 대기 Incident 스캔·회수",
        max_instances=1,
        coalesce=True,  # 밀린 실행은 1회로 합친다
        replace_existing=True,
    )
    scheduler.start()
    logger.info("agent dispatcher started: job=%s interval=%ss", JOB_ID, interval)
    return scheduler
