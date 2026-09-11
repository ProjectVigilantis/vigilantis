"""위협 관측 → 정형화·초기 판정 → Incident 저장·생성 이벤트 (Issue #322).

입력 공급 방식은 mock_threat_source가 소유한다. 이 Workflow는 검증된 관측을 받고
기존 정형화·Risk Evaluator·Intake를 호출한다. AI 분석·격리·AWS 실행은 후속 소비자 몫이다.
"""

from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy.orm import Session

from incident_intake import IntakeOutcome, create_incident_from_intake
from realtime import incident_event
from schemas.api.ws import WsEvent, WsEventType
from schemas.events import MockThreatEventInput, ThreatEventType
from schemas.intake import SecOpsIncidentIntake
from security.risk_evaluator import evaluate_threat
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
) -> IntakeOutcome:
    """독립 세션에서 관측 1건 처리. 신규 저장 성공에만 생성 이벤트를 발행한다.

    세션 수명은 호출부가 소유하고 Intake가 commit한다. 저장 예외는 rollback 후 전파한다.
    발행 실패는 별도 로그로 남기며 이미 commit된 저장 결과를 실패로 바꾸지 않는다.
    """
    try:
        event = normalize_threat_event(observation)
        intake = SecOpsIncidentIntake(
            title=_THREAT_TITLES[event.event_type],
            threat_event=event,
            initial_risk=evaluate_threat(event),
        )
    except ValueError as exc:
        raise ThreatInputRejected("위협 입력·초기 판정 계약 거부") from exc

    try:
        outcome = create_incident_from_intake(db, intake)
    except Exception:
        db.rollback()
        raise

    logger.info("threat_incident_received", extra={
        "incident_id": outcome.incident_id,
        "incident_created": outcome.created,
        "event_type": event.event_type.value,
    })
    if outcome.created and publish is not None:
        try:
            publish(incident_event(
                WsEventType.INCIDENT_CREATED,
                incident_id=outcome.incident_id,
                occurred_at=outcome.occurred_at,
            ))
        except Exception:  # noqa: BLE001 — 저장 성공 뒤의 알림 실패는 재접수 사유가 아니다
            logger.exception("threat_incident_publish_failed", extra={
                "incident_id": outcome.incident_id,
            })
    return outcome
