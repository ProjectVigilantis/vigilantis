"""보호 자산 격리 후보의 조건부 추천 규칙 v1과 근거 투영(security.isolation_recommendation) — #598.

탐지 원문은 실제 정규화(normalize_guardduty_finding)를 거친다 — 규칙이 보는 사실이 저장되는 모양과 같다.
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(_ROOT / "apps" / "core-api"), str(_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schemas.evidence import EvidenceItem, EvidenceType, ThreatEvidence  # noqa: E402
from schemas.recommendations import IsolationRecommendationBasis  # noqa: E402
from security.isolation_recommendation import (  # noqa: E402
    guardduty_recommendation_evidence,
    judge_protected_isolation,
)
from security.threat_normalizer import normalize_guardduty_finding, normalize_mock_input  # noqa: E402

ACCOUNT = "111122223333"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc"
TARGET = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
INCIDENT = "incident-1"
EVIDENCE = "evidence-1"
T0 = datetime(2026, 11, 10, 1, 2, 3, tzinfo=timezone.utc)
EVALUATED = datetime(2026, 11, 13, 4, 5, 6, 789012, tzinfo=timezone.utc)

ACTOR_OUTBOUND = {
    "ResourceRole": "ACTOR",
    "Action": {
        "ActionType": "NETWORK_CONNECTION",
        "NetworkConnectionAction": {
            "ConnectionDirection": "OUTBOUND",
            "RemoteIpDetails": {"IpAddressV4": "198.51.100.7"},
            "Blocked": False,
        },
    },
}


def _raw(service: dict | None = None, **over) -> dict:
    stamp = T0.isoformat().replace("+00:00", "Z")
    finding = {
        "Id": "f-1", "AccountId": ACCOUNT, "Region": REGION, "Severity": 5.0,
        "Type": "UnauthorizedAccess:EC2/SSHBruteForce",
        "Arn": f"arn:aws:guardduty:{REGION}:{ACCOUNT}:detector/d-1/finding/f-1",
        "CreatedAt": stamp, "UpdatedAt": stamp,
        "Resource": {"ResourceType": "Instance", "InstanceDetails": {"InstanceId": INSTANCE}},
        "Service": {"Archived": False, "EventFirstSeen": stamp, "EventLastSeen": stamp,
                    **copy.deepcopy(ACTOR_OUTBOUND if service is None else service)},
    }
    finding.update(over)
    return finding


def _evidence(event, *, evidence_id=EVIDENCE, incident_id=INCIDENT) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id, incident_id=incident_id, evidence_type=EvidenceType.THREAT,
        source_type="threat_event", source_id=event.threat_event_id,
        content=ThreatEvidence(event=event), occurred_at=event.occurred_at,
        collected_at=event.collected_at,
    )


def _guardduty(service: dict | None = None, **over):
    return normalize_guardduty_finding(_raw(service, **over), account_id=ACCOUNT, region=REGION)


def _judge(event, *, evidence=None, subject_arn=TARGET, target_arn=TARGET, evidence_ids=(EVIDENCE,),
           threat_event_id=None) -> IsolationRecommendationBasis:
    return judge_protected_isolation(
        evidence or _evidence(event), incident_id=INCIDENT, subject_arn=subject_arn,
        threat_event_id=event.threat_event_id if threat_event_id is None else threat_event_id,
        target_arn=target_arn, evidence_ids=list(evidence_ids), evaluated_at=EVALUATED,
    )


def _service(*, role="ACTOR", action_type="NETWORK_CONNECTION", network=None, drop=()):
    """ACTOR_OUTBOUND에서 한 칸만 바꾼 Service. drop은 지울 경로(점 구분)다."""
    service = copy.deepcopy(ACTOR_OUTBOUND)
    service["ResourceRole"] = role
    service["Action"]["ActionType"] = action_type
    service["Action"]["NetworkConnectionAction"].update(network or {})
    for path in drop:
        *parents, leaf = path.split(".")
        node = service
        for key in parents:
            node = node[key]
        del node[leaf]
    return service


@pytest.mark.parametrize("network", [
    {"Blocked": False},
    {"Blocked": True},
    {"RemoteIpDetails": {"IpAddressV6": "2001:db8::7"}},
], ids=["unblocked", "blocked", "ipv6"])
def test_recorded_actor_outbound_connection_is_only_conditionally_recommended(network):
    basis = _judge(_guardduty(_service(network=network)))
    assert basis.model_dump(mode="json") == {
        "origin": "SERVER_RULE", "rule_id": "PROTECTED_EC2_ISOLATION", "rule_version": 1,
        "classification": "CONDITIONALLY_RECOMMENDED",
        "reason_code": "GUARDDUTY_SSH_BRUTE_FORCE_ACTOR",
        "evaluated_at": "2026-11-13T04:05:06.789012Z",
    }


def test_blocked_is_observed_only_and_not_required():
    # Blocked 칸이 없어도(ABSENT) 판정은 같다 — 기본값 false 를 채우지 않고 조건으로도 쓰지 않는다
    event = _guardduty(_service(drop=("Action.NetworkConnectionAction.Blocked",)))
    assert event.payload.activity.blocked.status.value == "ABSENT"
    assert _judge(event).reason_code.value == "GUARDDUTY_SSH_BRUTE_FORCE_ACTOR"


@pytest.mark.parametrize("service, reason", [
    (_service(role="TARGET"), "RESOURCE_ROLE_TARGET"),
    (_service(drop=("ResourceRole",)), "RESOURCE_ROLE_UNKNOWN"),
    (_service(role="BYSTANDER"), "RESOURCE_ROLE_UNKNOWN"),
    (_service(role=None), "RESOURCE_ROLE_UNKNOWN"),
    (_service(action_type="PORT_PROBE"), "DIRECTION_NOT_CONFIRMED"),
    (_service(drop=("Action.ActionType",)), "DIRECTION_NOT_CONFIRMED"),
    (_service(drop=("Action.NetworkConnectionAction.ConnectionDirection",)), "DIRECTION_NOT_CONFIRMED"),
    (_service(network={"ConnectionDirection": "UNKNOWN"}), "DIRECTION_NOT_CONFIRMED"),
    (_service(network={"ConnectionDirection": "SIDEWAYS"}), "DIRECTION_NOT_CONFIRMED"),
    (_service(network={"ConnectionDirection": 7}), "DIRECTION_NOT_CONFIRMED"),
    (_service(network={"ConnectionDirection": "INBOUND"}), "DIRECTION_CONFLICT"),
    (_service(drop=("Action.NetworkConnectionAction.RemoteIpDetails",)), "TARGET_ADDRESS_NOT_PROVIDED"),
    (_service(network={"RemoteIpDetails": {"IpAddressV4": "not-an-ip"}}), "TARGET_ADDRESS_NOT_PROVIDED"),
    (_service(network={"RemoteIpDetails": {"IpAddressV4": "2001:db8::7"}}), "TARGET_ADDRESS_NOT_PROVIDED"),
    (_service(network={"RemoteIpDetails": None}), "TARGET_ADDRESS_NOT_PROVIDED"),
])
def test_every_unconfirmed_fact_requires_operator_judgement_with_its_reason(service, reason):
    basis = _judge(_guardduty(service))
    assert (basis.classification.value, basis.reason_code.value) == ("OPERATOR_JUDGEMENT_REQUIRED", reason)


def test_activity_not_captured_is_not_read_as_an_absent_role():
    event = _guardduty()
    legacy = event.model_copy(update={"payload": event.payload.model_copy(update={"activity": None})})
    assert _judge(legacy).reason_code.value == "RESOURCE_ROLE_NOT_CAPTURED"
    assert _judge(_guardduty(_service(drop=("ResourceRole",)))).reason_code.value == "RESOURCE_ROLE_UNKNOWN"


@pytest.mark.parametrize("over", [
    {"Type": "UnauthorizedAccess:EC2/RDPBruteForce"},
    {"Type": "unauthorizedaccess:ec2/sshbruteforce"},
    {"Type": "UnauthorizedAccess:EC2/SSHBruteForce!"},
    {"Resource": {"ResourceType": "AccessKey", "AccessKeyDetails": {"UserName": "ci"}}},
])
def test_only_the_exact_instance_finding_is_covered(over):
    event = _guardduty(**over)
    target = event.target_arn
    basis = _judge(event, subject_arn=target, target_arn=target)
    assert basis.reason_code.value == "FINDING_TYPE_NOT_COVERED"


def test_mock_ssh_threat_is_not_a_covered_finding():
    event = normalize_mock_input({
        "event_id": "mock-1", "event_type": "SSH_BRUTE_FORCE", "target_arn": TARGET,
        "source_ip": "203.0.113.10", "occurred_at": "2026-11-10T01:02:03Z",
        "failed_attempt_count": 120, "window_seconds": 300,
    })
    assert _judge(event).reason_code.value == "FINDING_TYPE_NOT_COVERED"
    assert guardduty_recommendation_evidence(_evidence(event)) is None


def test_facts_must_come_from_the_candidates_own_snapshot():
    event = _guardduty()
    other = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0other"
    mismatches = [
        _judge(event, evidence_ids=("evidence-2",)),               # 후보는 다른 사본을 가리킨다
        _judge(event, evidence_ids=(EVIDENCE, "evidence-2")),      # 사실을 여러 사본에서 모을 수 없다
        _judge(event, evidence=_evidence(event, incident_id="incident-2")),
        _judge(event, target_arn=other),                           # 후보 대상이 인시던트 대상과 다르다
        _judge(event, subject_arn=other, target_arn=other),        # 탐지 대상이 후보 대상과 다르다
        _judge(event, threat_event_id="another-threat-event"),     # 인시던트의 위협이 아니다
    ]
    assert {basis.reason_code.value for basis in mismatches} == {"EVIDENCE_TARGET_MISMATCH"}
    assert {basis.classification.value for basis in mismatches} == {"OPERATOR_JUDGEMENT_REQUIRED"}


def test_projection_exposes_only_the_snapshot_facts_without_a_judgement():
    event = _guardduty(_service(role="TARGET"))
    projection = guardduty_recommendation_evidence(_evidence(event))
    stamp = "2026-11-10T01:02:03Z"
    assert projection.model_dump(mode="json") == {
        "evidence_id": EVIDENCE, "target_arn": TARGET, "finding_id": "f-1",
        "finding_type": "UnauthorizedAccess:EC2/SSHBruteForce", "resource_type": "Instance",
        "first_seen_at": stamp, "last_seen_at": stamp,
        "activity": {
            "resource_role": {"status": "RECORDED", "value": "TARGET", "source_value": None},
            "action_type": {"status": "RECORDED", "value": "NETWORK_CONNECTION", "source_value": None},
            "connection_direction": {"status": "RECORDED", "value": "OUTBOUND", "source_value": None},
            "remote_ip": {"status": "RECORDED", "value": "198.51.100.7", "source_value": None},
            "blocked": {"status": "RECORDED", "value": False, "source_value": None},
        },
    }
    legacy = event.model_copy(update={"payload": event.payload.model_copy(update={"activity": None})})
    assert guardduty_recommendation_evidence(_evidence(legacy)).activity is None
