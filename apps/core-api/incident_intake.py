# ==============================================================================
# [파일 설명]
# Detection 결과를 Incident 1건으로 만드는 업무 흐름 계층입니다. (Issue #254)
# Rule Engine(자산 판정)·Risk Evaluator(위협 판정)와 Incident 저장소 사이에 서며,
# 진입 타입은 packages/schemas/intake.py의 IncidentIntake 하나입니다.
#
# 계층 경계 — workflows.py와 같은 3층 분리를 따릅니다(Router → Workflow → Repository).
#   - commit은 여기서만 합니다. Repository는 commit하지 않습니다.
#   - 판정 자체를 하지 않습니다. 어떤 자산·위협이 Incident가 되는지는 판정 계층이
#     이미 정했고(services/rule_engine.py · security/risk_evaluator.py), 그 결과가
#     계약을 통과하는지는 IncidentIntake가 봅니다. 이 계층은 저장 순서와 중복만 봅니다.
#   - INCIDENT_CREATED 발행은 여기서 하지 않습니다. commit 이후 호출부가 발행합니다
#     (routers/incidents.py가 종료 처리에서 하는 것과 같은 경계 — 이 계층이 앱 상태
#     app.state.realtime을 알면 3층 분리가 깨집니다).
#
# 한 건이 저장되는 순서는 아래 하나입니다. 트랜잭션 1개에 담습니다 — 중간에서 끊기면
# 근거 없는 Incident나 Incident 없는 위협 이벤트가 남습니다.
#   SECOPS: ThreatEvent 저장 → Incident 생성(title·initial_risk_level·response_mode·
#           사유 코드) → THREAT 근거 1건
#   FINOPS: Incident 생성(위험 대응 축 전부 null) → RULE 근거 1건
#
# **RULE 근거의 content로 저장하는 값은 intake.rule_evaluation 객체 그 자체입니다.**
# 그래프 입력 빌더가 최상위 rule_evaluation을 이 근거 행에서 읽어야 두 값이 같은
# 객체에서 나온다는 불변식이 성립합니다(agent_dispatcher.py 헤더 · Issue #243).
#
# **Detection 당시 자산 스냅샷을 보존할 자리가 아직 없습니다.** Intake는 판정과 같은
# 회차의 자산을 함께 받지만(schemas/intake.py DetectionAssetSnapshot), 그 값을 담을
# 곳이 저장소에 없습니다 — 자산 행은 회차마다 덮어써지고(db/repositories/assets.py
# upsert_asset), Evidence 유형 4종(METRIC·RULE·THREAT·EXECUTION, #49 확정)에 자산을
# 실을 칸이 없습니다. 보존 방식을 정하기 전까지 그래프 입력은 이 스냅샷을 얻을 수
# 없으므로, 그 결정이 [남은 작업] 2번입니다.
#
# 중복은 계약이 막지 못해 여기서 막습니다.
#   - FINOPS: 같은 subject_arn의 미종료 Incident가 있으면 만들지 않습니다. 수집
#     주기마다 같은 저활성 자산이 다시 판정되므로, 막지 않으면 한 자산에 카드가
#     주기 수만큼 쌓입니다.
#   - SECOPS: deduplication_key로 막습니다(db/repositories/incidents.py
#     get_threat_event_by_dedup_key · insert_threat_event의 IntegrityError).
#   - 어느 쪽이든 기존 Incident를 그대로 돌려주고 created=False로 알립니다. 중복은
#     오류가 아니라 정상 경로입니다.
#
# [남은 작업]
# 1. 본문 구현 — 위 저장 순서·중복 규칙·트랜잭션 경계.
# 2. Detection 자산 스냅샷의 보존 방식 — 후보는 셋입니다. ⓐ Evidence 유형에 자산을
#    추가 ⓑ 자산 이력을 회차 단위로 남기는 테이블 ⓒ 보존하지 않고 최신 회차로 판정을
#    다시 수행. ⓒ는 Incident 생성 시각의 근거를 버리는 선택이라 감사 기록이 남지
#    않습니다. 이 결정 전까지 그래프 입력이 Detection 시점 자산을 얻을 수 없습니다.
# 3. 판정 계층에서 이 진입점을 부르는 자리 — services/scheduler.py의 수집 파이프라인
#    (담당: 김세혁·김승철)과 위협 주입 경로. 위협 주입 방식은 ADR-0006이 별도 결정
#    대상으로 남겼습니다.
# 4. FINOPS 미종료 판정의 기준 상태 집합 — RESOLVED 외에 무엇을 종료로 볼지는
#    Incident 상태 6종(api/incidents.py)과 함께 확정합니다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from schemas.intake import IncidentIntake

logger = logging.getLogger("vigilantis.incident_intake")


@dataclass
class IntakeOutcome:
    """Intake 1건 처리 결과 — 로그·테스트와 발행 여부를 정하는 호출부가 읽는 값이다."""

    incident_id: str
    created: bool          # False = 중복이라 기존 Incident를 그대로 돌려줬다
    occurred_at: datetime  # 저장된 Incident.updated_at — WS 봉투의 occurred_at


def create_incident_from_intake(db: Session, intake: IncidentIntake) -> IntakeOutcome:
    """Intake 1건 → Incident 1건. **세션 수명은 호출부가 소유한다.**

    created=True인 결과에 대해서만 호출부가 INCIDENT_CREATED를 발행한다.
    """
    raise NotImplementedError("Issue #254 — 저장 순서·중복 규칙은 파일 헤더 참조")
