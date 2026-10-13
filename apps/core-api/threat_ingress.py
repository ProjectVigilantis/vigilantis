"""위협 관측 → 정형화·초기 판정 → Incident 저장·생성 이벤트 (Issue #322).

입력 공급 방식은 mock_threat_source가 소유한다. 이 Workflow는 검증된 관측을 받고
기존 정형화·Risk Evaluator·Intake를 호출하고 High 선차단을 SOAR에 넘긴다.
AI 분석은 독립 소비자가 처리하며 AWS 실행은 dispatcher 경계를 유지한다.
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.orm import Session

from incident_intake import IntakeOutcome, create_incident_from_intake
from isolation_timing import begin_trace, current_trace
from realtime import incident_event
from schemas.api.ws import WsEvent, WsEventType
from schemas.events import MockThreatEventInput, ThreatEventType
from schemas.intake import SecOpsIncidentIntake
from schemas.mock_logs import MockSshLogEvidence
from schemas.runbooks import TriggerSource
from secops_context import SecOpsContextLimitExceeded
from security.risk_evaluator import evaluate_threat
from security.soar import trigger_isolation
from security.threat_normalizer import normalize_threat_event

logger = logging.getLogger("vigilantis.threat_ingress")

_THREAT_TITLES = {
    ThreatEventType.OPEN_IP: "보안 그룹 인그레스 전체 개방",
    ThreatEventType.SSH_BRUTE_FORCE: "SSH 브루트포스 시도",
}


class ThreatInputRejected(ValueError):
    """관측·판정 계약 거부. 저장소 장애와 구분해 입력 공급자에게 반환한다."""


def receive_threat(
    db: Session,
    observation: MockThreatEventInput,
    publish: Callable[[WsEvent], None] | None = None,
    *, log_evidence: MockSshLogEvidence | None = None,
) -> IntakeOutcome:
    # 기존 trace는 시뮬레이션의 명시적 환경 표기만 공급한다. 시작 경계는 이 함수 진입이다.
    if current_trace() is not None:
        return _receive_threat(db, observation, publish, log_evidence=log_evidence)
    with begin_trace():
        return _receive_threat(db, observation, publish, log_evidence=log_evidence)


def _receive_threat(
    db: Session,
    observation: MockThreatEventInput,
    publish: Callable[[WsEvent], None] | None = None,
    *, log_evidence: MockSshLogEvidence | None = None,
) -> IntakeOutcome:
    """독립 세션에서 관측 1건 처리. 신규 저장 성공에만 생성 이벤트를 발행한다.

    세션 수명은 호출부가 소유하고 Intake가 commit한다. 저장 예외는 rollback 후 전파한다.
    발행 실패는 별도 로그로 남기며 이미 commit된 저장 결과를 실패로 바꾸지 않는다.
    """
    try:
        if log_evidence is not None and not log_evidence.matches_observation(observation):
            raise ValueError("log evidence differs from mock observation")
        event = normalize_threat_event(observation)
        intake = SecOpsIncidentIntake(
            title=_THREAT_TITLES[event.event_type],
            threat_event=event,
            initial_risk=evaluate_threat(event),
            log_evidence=log_evidence,
        )
        current_trace().mark("risk_evaluated")
    except ValueError as exc:
        raise ThreatInputRejected("위협 입력·초기 판정 계약 거부") from exc

    try:
        outcome = create_incident_from_intake(db, intake)
    except SecOpsContextLimitExceeded as exc:
        db.rollback()
        raise ThreatInputRejected("MVP 근거 관계 수 상한 초과") from exc
    except Exception:
        db.rollback()
        raise

    logger.info("threat_incident_received", extra={
        "incident_id": outcome.incident_id,
        "incident_created": outcome.created,
        "event_type": event.event_type.value,
    })
    current_trace().bind(incident_id=outcome.incident_id)
    current_trace().mark("incident_committed")
    if outcome.created and publish is not None:
        try:
            publish(incident_event(
                WsEventType.INCIDENT_CREATED,
                incident_id=outcome.incident_id,
                occurred_at=outcome.occurred_at,
                category=outcome.category,
            ))
        except Exception:  # noqa: BLE001 — 저장 성공 뒤의 알림 실패는 재접수 사유가 아니다
            logger.exception("threat_incident_publish_failed", extra={
                "incident_id": outcome.incident_id,
            })
    if intake.initial_risk.response_mode.value == TriggerSource.PRE_MITIGATION_0_5S.value:
        try:
            trigger_isolation(
                db, outcome.incident_id,
                trigger_source=TriggerSource.PRE_MITIGATION_0_5S, publish=publish,
            )
        except Exception:  # 접수 commit 이후의 실행 실패는 입력 거부가 아니다
            db.rollback()
            current_trace().finish("failed")
            logger.exception("automatic_isolation_trigger_failed", extra={
                "incident_id": outcome.incident_id,
            })
    return outcome
