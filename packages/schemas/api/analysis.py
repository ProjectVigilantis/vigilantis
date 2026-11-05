"""저장된 분석 결과의 조회 계약. 사용자 종료 판단·현재 제안 목록과 독립이다.

분석 실패 사유(AnalysisFailureReason)의 원천도 여기다. 그래프 출력 계약(schemas/agents.py)이
같은 어휘를 쓰지만 agents.py → api/incidents.py → 이 모듈 순으로 import하므로, 여기서
agents.py를 import하면 순환이 된다. 그래서 공개 응답 쪽에 두고 agents.py가 가져간다.
"""

from enum import Enum, StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..guardrails import GuardrailReasonCode, GuardrailStep
from ..runbooks import RunbookId


class AnalysisFailureReason(StrEnum):
    """분석이 FAILED로 끝난 사유. 그래프(ai/agent.py)와 Workflow 검증(agent_dispatcher)이 함께 쓴다.

    주입 시도로 거절된 제안(메뉴 밖 Runbook·대상 밖 ARN)과 형식 실패·모델 거절을 나중에
    가를 수 있게 하는 것이 목적이다(#424). 저장 칸(incidents.agent_failure_reason)은 DB에서
    자유 문자열이라 값을 늘려도 마이그레이션이 필요 없다. 기존 값은 바꾸지 않는다.
    """

    # 모델 호출 — ai/model_client.py 경계 예외 3갈래. 왕복의 어느 자리인지는 phase가 가른다
    MODEL_REJECTED = "MODEL_REJECTED"                    # 인증·요청 거절, 모델 refusal
    MODEL_CONTRACT_VIOLATION = "MODEL_CONTRACT_VIOLATION"  # 요청 직렬화·응답 파싱 실패
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"              # 제한시간·일시 오류(재시도 소진)
    # 후보 — 모델이 지어낼 수 없는 값을 지어낸 경우
    RUNBOOK_NOT_OFFERED = "RUNBOOK_NOT_OFFERED"          # 입력 capabilities 밖 Runbook
    TARGET_NOT_ALLOWED = "TARGET_NOT_ALLOWED"            # 허용 대상 밖 target_arn
    SERVER_PARAMETER_UNAVAILABLE = "SERVER_PARAMETER_UNAVAILABLE"  # 서버 몫 값을 계산 못 함
    CANDIDATE_CONTRACT_VIOLATION = "CANDIDATE_CONTRACT_VIOLATION"  # RunbookCandidateDraft 위반
    OUTPUT_CONTRACT_VIOLATION = "OUTPUT_CONTRACT_VIOLATION"        # AgentGraphOutput 위반
    # Workflow 검증 — 출력 모델 단독으로 볼 수 없는 것(agent_dispatcher._contract_violation)
    EVIDENCE_NOT_IN_INPUT = "EVIDENCE_NOT_IN_INPUT"
    REVIEWED_RISK_MISMATCH = "REVIEWED_RISK_MISMATCH"
    SERVER_VALUE_MISMATCH = "SERVER_VALUE_MISMATCH"
    SSH_DENY_PARAMETER_MISMATCH = "SSH_DENY_PARAMETER_MISMATCH"
    UNSTORABLE_VALUE = "UNSTORABLE_VALUE"
    # 입력 — 모델을 부르기 전에 닫는다(agent_dispatcher.GraphInputUnavailable)
    GRAPH_INPUT_UNAVAILABLE = "GRAPH_INPUT_UNAVAILABLE"
    NO_ACTION_AVAILABLE = "NO_ACTION_AVAILABLE"                # 이 위협·자산에 제공할 조치 메뉴가 없다
    SOURCE_ADDRESS_UNSUPPORTED = "SOURCE_ADDRESS_UNSUPPORTED"  # SSH 출발지가 IPv4가 아니다(NACL CidrBlock 불가)


# 분석이 고장 난 것이 아니라 자동 조치가 없어 닫힌 사유 — 공개 상태 NO_ACTION_AVAILABLE로 가른다
NO_ACTION_FAILURE_REASONS: frozenset[AnalysisFailureReason] = frozenset({
    AnalysisFailureReason.NO_ACTION_AVAILABLE,
    AnalysisFailureReason.SOURCE_ADDRESS_UNSUPPORTED,
})


class AnalysisResultStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PROPOSALS_GENERATED = "PROPOSALS_GENERATED"
    NO_PROPOSAL = "NO_PROPOSAL"
    GUARDRAIL_REJECTED = "GUARDRAIL_REJECTED"
    FAILED = "FAILED"
    # 분석 호출은 FAILED로 닫혔지만 원인이 분석 고장이 아니라 제공할 자동 조치가 없는 것.
    # 모델을 부르지 않았고 인시던트는 FAILED(관제자 종료 처리 대상)로 남는다
    NO_ACTION_AVAILABLE = "NO_ACTION_AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"  # 저장된 평가가 없어 성공·전체 거절을 확정할 수 없음


class GuardrailRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runbook_id: RunbookId
    failed_step: GuardrailStep
    reason_code: GuardrailReasonCode | None = None


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AnalysisResultStatus
    # 모델 원문·내부 예외·AWS 응답은 포함하지 않는다.
    guardrail_rejections: list[GuardrailRejection] = Field(default_factory=list)
    # 분석 호출이 FAILED로 닫힌 사유 코드. 사유 기록 이전에 닫힌 건은 FAILED여도 null이다
    failure_reason: AnalysisFailureReason | None = None

    @model_validator(mode="after")
    def _reason_matches_status(self):
        no_action = self.failure_reason in NO_ACTION_FAILURE_REASONS
        if (self.status is AnalysisResultStatus.NO_ACTION_AVAILABLE) != no_action:
            raise ValueError(
                "NO_ACTION_AVAILABLE은 조치 없음 사유(NO_ACTION_AVAILABLE·SOURCE_ADDRESS_UNSUPPORTED)와만 함께 옵니다"
            )
        if self.failure_reason is not None and self.status not in (
            AnalysisResultStatus.FAILED, AnalysisResultStatus.NO_ACTION_AVAILABLE,
        ):
            raise ValueError("failure_reason은 FAILED·NO_ACTION_AVAILABLE에만 옵니다")
        return self
