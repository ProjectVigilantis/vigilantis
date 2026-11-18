# ==============================================================================
# [파일 설명]
# 보호 자산 서버 격리 후보의 조건부 추천 규칙 v1과, 후보가 가리키는 GuardDuty 근거의 응답 투영 (#598).
#
#   - 판정은 후보를 만들 때 한 번, 그 후보의 evidence_ids가 가리키는 THREAT 근거 사본 하나만 본다.
#     현재 ThreatEvent · 인시던트 요약 · 자산 태그를 다시 읽지 않고, 조회(GET)에서 다시 계산하지 않는다.
#   - CONDITIONALLY_RECOMMENDED는 침해 증명이 아니다. 정당한 업무 · 승인된 테스트인지는 승인하는 사람이
#     확인하며 규칙은 정당성을 추정하지 않는다. 보호 판정 · 위험도 · 사전 검사는 후보를 허용할 뿐 필요성이 아니다.
#   - Blocked는 관측 사실로만 남고 판정 조건이 아니다. 없는 칸을 기본값으로 채우지 않는다.
#   - DB · AWS · 모델을 부르지 않는다.
# ==============================================================================

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address

from schemas.api.incidents import GuardDutyRecommendationEvidence
from schemas.events import (
    GuardDutyConnectionDirection,
    GuardDutyFactStatus,
    GuardDutyFindingThreatPayload,
    GuardDutyResourceRole,
)
from schemas.evidence import EvidenceItem, ThreatEvidence
from schemas.recommendations import (
    IsolationRecommendationBasis,
    IsolationRecommendationClassification,
    IsolationRecommendationReason,
)

COVERED_FINDING_TYPE = "UnauthorizedAccess:EC2/SSHBruteForce"
COVERED_RESOURCE_TYPE = "Instance"
NETWORK_CONNECTION = "NETWORK_CONNECTION"

_Reason = IsolationRecommendationReason


def _reason(
    evidence: EvidenceItem,
    *,
    incident_id: str,
    subject_arn: str,
    threat_event_id: str | None,
    target_arn: str,
    evidence_ids: Sequence[str],
) -> IsolationRecommendationReason:
    if (
        list(evidence_ids) != [evidence.evidence_id]
        or evidence.incident_id != incident_id
        or target_arn != subject_arn
    ):
        return _Reason.EVIDENCE_TARGET_MISMATCH
    content = evidence.content
    if not isinstance(content, ThreatEvidence):
        return _Reason.FINDING_TYPE_NOT_COVERED
    event = content.event
    if event.target_arn != target_arn or event.threat_event_id != threat_event_id:
        return _Reason.EVIDENCE_TARGET_MISMATCH
    payload = event.payload
    if (
        not isinstance(payload, GuardDutyFindingThreatPayload)
        or payload.finding_type != COVERED_FINDING_TYPE
        or payload.resource_type != COVERED_RESOURCE_TYPE
    ):
        return _Reason.FINDING_TYPE_NOT_COVERED
    activity = payload.activity
    if activity is None:
        return _Reason.RESOURCE_ROLE_NOT_CAPTURED
    if activity.resource_role.status is not GuardDutyFactStatus.RECORDED:
        return _Reason.RESOURCE_ROLE_UNKNOWN
    if activity.resource_role.value is GuardDutyResourceRole.TARGET:
        return _Reason.RESOURCE_ROLE_TARGET
    direction = activity.connection_direction
    if (
        activity.action_type.status is not GuardDutyFactStatus.RECORDED
        or activity.action_type.value != NETWORK_CONNECTION
        or direction.status is not GuardDutyFactStatus.RECORDED
        or direction.value is GuardDutyConnectionDirection.UNKNOWN
    ):
        return _Reason.DIRECTION_NOT_CONFIRMED
    if direction.value is not GuardDutyConnectionDirection.OUTBOUND:
        return _Reason.DIRECTION_CONFLICT
    remote_ip = activity.remote_ip
    if remote_ip.status is not GuardDutyFactStatus.RECORDED or not isinstance(
        remote_ip.value, (IPv4Address, IPv6Address)
    ):
        return _Reason.TARGET_ADDRESS_NOT_PROVIDED
    return _Reason.GUARDDUTY_SSH_BRUTE_FORCE_ACTOR


def judge_protected_isolation(
    evidence: EvidenceItem,
    *,
    incident_id: str,
    subject_arn: str,
    threat_event_id: str | None,
    target_arn: str,
    evidence_ids: Sequence[str],
    evaluated_at: datetime,
) -> IsolationRecommendationBasis:
    """후보(대상 · evidence_ids)와 그 근거 사본 하나로 판정한다. 조건 전부가 같은 사본에서 나와야 한다.

    양성은 정확한 탐지 유형 · Instance 자원 · 사건과 대상이 맞는 근거 · 기록된 ACTOR · NETWORK_CONNECTION ·
    OUTBOUND · 기록된 원격 주소(IPv4/IPv6)가 모두 성립할 때뿐이다. 나머지는 관제자 판단 필요와 그 첫 사유다.
    """
    reason = _reason(
        evidence, incident_id=incident_id, subject_arn=subject_arn,
        threat_event_id=threat_event_id, target_arn=target_arn, evidence_ids=evidence_ids,
    )
    return IsolationRecommendationBasis(
        classification=(
            IsolationRecommendationClassification.CONDITIONALLY_RECOMMENDED
            if reason is _Reason.GUARDDUTY_SSH_BRUTE_FORCE_ACTOR
            else IsolationRecommendationClassification.OPERATOR_JUDGEMENT_REQUIRED
        ),
        reason_code=reason,
        evaluated_at=evaluated_at,
    )


def guardduty_recommendation_evidence(
    evidence: EvidenceItem,
) -> GuardDutyRecommendationEvidence | None:
    """GuardDuty 탐지를 담은 THREAT 근거면 그 사본의 정해진 칸만 투영한다. 판정을 담지 않는다."""
    content = evidence.content
    if not isinstance(content, ThreatEvidence):
        return None
    payload = content.event.payload
    if not isinstance(payload, GuardDutyFindingThreatPayload):
        return None
    return GuardDutyRecommendationEvidence(
        evidence_id=evidence.evidence_id,
        target_arn=content.event.target_arn,
        finding_id=payload.finding_id,
        finding_type=payload.finding_type,
        resource_type=payload.resource_type,
        first_seen_at=payload.first_seen_at,
        last_seen_at=payload.last_seen_at,
        activity=payload.activity,
    )
