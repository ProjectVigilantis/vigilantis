"""GuardDuty 탐지 정형화(threat_normalizer.normalize_guardduty_finding)와 심각도 판정(risk_evaluator) — #588 C · Q2."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(_ROOT / "apps" / "core-api"), str(_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schemas.api.incidents import ResponseMode, RiskLevel  # noqa: E402
from schemas.events import (  # noqa: E402
    GuardDutyConnectionDirection,
    GuardDutyFactStatus,
    GuardDutyResourceRole,
    RiskReasonCode,
    ThreatEventType,
)
from security.risk_evaluator import evaluate_threat  # noqa: E402
from security.threat_normalizer import normalize_guardduty_finding  # noqa: E402

ACCOUNT = "111122223333"
REGION = "ap-northeast-2"
T0 = datetime(2026, 11, 10, 1, 2, 3, 456000, tzinfo=timezone.utc)


def _stamp(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def raw(fid="f-1", *, severity=5.0, updated=T0, last_seen=T0, count=1, resource=None, **over) -> dict:
    return {
        "Id": fid, "AccountId": ACCOUNT, "Region": REGION, "Severity": severity,
        "Type": "UnauthorizedAccess:EC2/SSHBruteForce",
        "Arn": f"arn:aws:guardduty:{REGION}:{ACCOUNT}:detector/d-1/finding/{fid}",
        "CreatedAt": _stamp(T0), "UpdatedAt": _stamp(updated),
        "Resource": resource or {"ResourceType": "Instance", "InstanceDetails": {"InstanceId": "i-0abc"}},
        "Service": {"Archived": False, "Count": count, "EventFirstSeen": _stamp(T0), "EventLastSeen": _stamp(last_seen)},
        **over,
    }


def _norm(finding: dict):
    return normalize_guardduty_finding(finding, account_id=ACCOUNT, region=REGION)


def test_instance_finding_targets_the_instance_arn():
    event = _norm(raw())
    assert event.event_type is ThreatEventType.GUARDDUTY_FINDING
    assert event.target_arn == f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc"
    assert event.source_event_id == "f-1"
    assert event.occurred_at == T0  # 최초 관측 시각이다
    p = event.payload
    assert (p.finding_id, p.finding_type, p.severity, p.resource_type, p.region, p.finding_region) == (
        "f-1", "UnauthorizedAccess:EC2/SSHBruteForce", 5.0, "Instance", REGION, REGION)
    assert (p.first_seen_at, p.last_seen_at) == (T0, T0)


def test_non_instance_finding_targets_the_finding_itself():
    # IAM 키 · S3 ARN 에는 계정이 없어 소속을 정할 수 없다 — 탐지 ARN 은 계정 · 리전을 담는다
    event = _norm(raw(resource={"ResourceType": "AccessKey", "AccessKeyDetails": {"UserName": "ci"}}))
    assert event.target_arn == f"arn:aws:guardduty:{REGION}:{ACCOUNT}:detector/d-1/finding/f-1"
    assert event.payload.resource_type == "AccessKey"


def test_an_updated_finding_keeps_the_same_deduplication_key():
    # GuardDuty 는 같은 ID 를 갱신해 다시 내보낸다(횟수 · 최근 관측 · 심각도). 갱신마다 새 위협이 되면 안 된다
    first = _norm(raw())
    later = _norm(raw(severity=8.0, updated=T0 + timedelta(hours=6), last_seen=T0 + timedelta(hours=6), count=9))
    assert first.deduplication_key == later.deduplication_key
    assert first.deduplication_key.startswith("GUARDDUTY_FINDING|")
    assert _norm(raw("f-2")).deduplication_key != first.deduplication_key


def test_same_finding_id_in_another_region_or_account_is_another_threat():
    other_region = normalize_guardduty_finding(
        raw(Region="ap-northeast-1"), account_id=ACCOUNT, region="ap-northeast-1")
    other_account = normalize_guardduty_finding(
        raw(AccountId="999988887777", Arn="arn:aws:guardduty:x:999988887777:detector/d/finding/f-1"),
        account_id="999988887777", region=REGION)
    keys = {_norm(raw()).deduplication_key, other_region.deduplication_key, other_account.deduplication_key}
    assert len(keys) == 3


def test_a_global_service_finding_shown_in_another_region_is_kept():
    # 글로벌 서비스(IAM · STS) 이벤트 탐지는 다른 리전에서 탐지해도 us-east-1 로 표시된다(PR 리뷰 1)
    access_key = {"ResourceType": "AccessKey", "AccessKeyDetails": {"UserName": "ci"}}
    event = _norm(raw(Region="us-east-1", resource=access_key))
    assert (event.payload.region, event.payload.finding_region) == (REGION, "us-east-1")
    # 중복 키는 읽은 탐지기의 리전으로 — 같은 탐지를 같은 리전에서 다시 읽으면 같은 키다
    assert event.deduplication_key == _norm(raw(resource=access_key)).deduplication_key
    assert evaluate_threat(event).initial_risk_level is RiskLevel.MEDIUM


def test_an_instance_arn_uses_the_region_the_finding_reports():
    event = _norm(raw(Region="ap-northeast-1"))
    assert event.target_arn == f"arn:aws:ec2:ap-northeast-1:{ACCOUNT}:instance/i-0abc"


def test_a_finding_without_region_falls_back_to_the_read_region():
    finding = raw()
    del finding["Region"]
    assert _norm(finding).payload.finding_region == REGION


@pytest.mark.parametrize("override", [
    {"AccountId": "999988887777"},   # 위임 관리자 계정이 돌려준 멤버 계정 탐지
    {"AccountId": "999988887777", "Region": "us-east-1"},  # 리전을 받아 주어도 계정은 그대로 거른다
    {"Region": 7},
    {"Id": ""},
    {"Severity": None},
    {"Severity": 10.5},
    {"Type": ""},
    {"UpdatedAt": None, "Service": {}, "CreatedAt": None},
])
def test_malformed_or_foreign_findings_are_rejected(override):
    with pytest.raises(ValueError):
        _norm(raw(**override))


@pytest.mark.parametrize(("severity", "level"), [
    (10.0, RiskLevel.HIGH), (9.0, RiskLevel.HIGH), (7.0, RiskLevel.HIGH),
    (6.9, RiskLevel.MEDIUM), (4.0, RiskLevel.MEDIUM),
    (3.9, RiskLevel.LOW), (1.0, RiskLevel.LOW), (0.0, RiskLevel.LOW),
])
def test_severity_bands(severity, level):
    # #588 Q2 — 7.0 이상 HIGH(Critical 도 HIGH 로 접는다) · 4.0–6.9 MEDIUM · 그 아래 LOW
    result = evaluate_threat(_norm(raw(severity=severity)))
    assert result.initial_risk_level is level
    assert result.reason_codes == [RiskReasonCode.RISK_GUARDDUTY_FINDING]


def _service(role="TARGET", action=None, count=1) -> dict:
    service = raw()["Service"]
    if role is not None:
        service["ResourceRole"] = role
    if action is not None:
        service["Action"] = action
    return service


def _network(**over) -> dict:
    block = {
        "ConnectionDirection": "INBOUND", "Blocked": False,
        "RemoteIpDetails": {"IpAddressV4": "198.51.100.7"},
    }
    block.update(over)
    return {"ActionType": "NETWORK_CONNECTION", "NetworkConnectionAction": block}


def _activity(service: dict):
    return _norm(raw(Service=service)).payload.activity


def test_activity_maps_the_source_format_from_the_same_finding():
    activity = _activity(_service("ACTOR", _network(ConnectionDirection="OUTBOUND", Blocked=True)))
    assert (activity.resource_role.status, activity.resource_role.value) == (
        GuardDutyFactStatus.RECORDED, GuardDutyResourceRole.ACTOR)
    assert activity.action_type.value == "NETWORK_CONNECTION"
    assert activity.connection_direction.value is GuardDutyConnectionDirection.OUTBOUND
    assert activity.remote_ip.value == ip_address("198.51.100.7")
    assert activity.blocked.value is True


def test_blocked_false_is_a_recorded_fact_not_an_absent_one():
    activity = _activity(_service(action=_network(Blocked=False)))
    assert activity.blocked.status is GuardDutyFactStatus.RECORDED
    assert activity.blocked.value is False


def test_ipv6_remote_address_is_kept_as_a_validated_address():
    activity = _activity(_service(action=_network(RemoteIpDetails={"IpAddressV6": "2001:DB8::1"})))
    assert activity.remote_ip.value == ip_address("2001:db8::1")


@pytest.mark.parametrize("details", [
    {"IpAddressV4": "2001:db8::2"},     # 각 칸은 자기 주소 체계만 받는다(AWS RemoteIpDetails)
    {"IpAddressV6": "198.51.100.7"},
])
def test_remote_ip_in_the_wrong_family_field_is_invalid_and_the_finding_is_kept(details):
    event = _norm(raw(Service=_service(action=_network(RemoteIpDetails=details))))
    fact = event.payload.activity.remote_ip
    assert (fact.status, fact.value) == (GuardDutyFactStatus.INVALID, None)
    assert event.target_arn == f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc"


def test_omitted_keys_are_absent_but_explicit_nulls_are_invalid():
    omitted = _activity(_service(role=None, action={"NetworkConnectionAction": {"RemoteIpDetails": {}}}))
    for fact in (omitted.resource_role, omitted.action_type, omitted.connection_direction,
                 omitted.blocked, omitted.remote_ip):
        assert fact.status is GuardDutyFactStatus.ABSENT
    nulls = _activity(_service(None, {
        "ActionType": None,
        "NetworkConnectionAction": {"ConnectionDirection": None, "Blocked": None,
                                    "RemoteIpDetails": {"IpAddressV4": None}},
    }) | {"ResourceRole": None})
    for fact in (nulls.resource_role, nulls.action_type, nulls.connection_direction,
                 nulls.blocked, nulls.remote_ip):
        assert (fact.status, fact.value, fact.source_value) == (GuardDutyFactStatus.INVALID, None, None)


@pytest.mark.parametrize(("action", "invalid"), [
    ({"Action": None}, {"action_type", "connection_direction", "remote_ip", "blocked"}),
    ({"Action": {"ActionType": "X", "NetworkConnectionAction": None}},
     {"connection_direction", "remote_ip", "blocked"}),
    ({"Action": {"ActionType": "X", "NetworkConnectionAction": {"RemoteIpDetails": None}}}, {"remote_ip"}),
])
def test_explicit_null_parent_blocks_make_their_facts_invalid(action, invalid):
    service = _service() | action
    activity = _activity(service)
    names = ("action_type", "connection_direction", "remote_ip", "blocked")
    for name in names:
        expected = GuardDutyFactStatus.INVALID if name in invalid else (
            GuardDutyFactStatus.RECORDED if name == "action_type" else GuardDutyFactStatus.ABSENT)
        assert getattr(activity, name).status is expected, name


def test_a_finding_without_any_added_fact_is_inspected_and_all_absent():
    activity = _activity(_service(role=None))
    assert activity is not None
    for fact in (activity.resource_role, activity.action_type, activity.connection_direction,
                 activity.remote_ip, activity.blocked):
        assert (fact.status, fact.value, fact.source_value) == (GuardDutyFactStatus.ABSENT, None, None)


def test_action_without_network_block_leaves_network_facts_absent_and_nothing_defaulted():
    activity = _activity(_service(action={"ActionType": "AWS_API_CALL"}))
    assert activity.action_type.value == "AWS_API_CALL"
    assert activity.connection_direction.status is GuardDutyFactStatus.ABSENT
    assert activity.blocked.status is GuardDutyFactStatus.ABSENT
    assert activity.remote_ip.status is GuardDutyFactStatus.ABSENT


def test_missing_individual_network_fields_stay_absent():
    activity = _activity(_service(action={"ActionType": "NETWORK_CONNECTION", "NetworkConnectionAction": {}}))
    assert activity.connection_direction.status is GuardDutyFactStatus.ABSENT
    assert activity.blocked.status is GuardDutyFactStatus.ABSENT
    assert activity.remote_ip.status is GuardDutyFactStatus.ABSENT


def test_unrecognized_values_are_kept_apart_from_absent_and_invalid():
    activity = _activity(_service("OBSERVER", _network(ConnectionDirection="SIDEWAYS")))
    assert (activity.resource_role.status, activity.resource_role.value, activity.resource_role.source_value) == (
        GuardDutyFactStatus.UNRECOGNIZED, None, "OBSERVER")
    assert activity.connection_direction.status is GuardDutyFactStatus.UNRECOGNIZED
    assert activity.connection_direction.source_value == "SIDEWAYS"


def test_malformed_values_are_invalid_not_absent_and_do_not_reject_the_finding():
    activity = _activity(_service(7, _network(
        ConnectionDirection=["INBOUND"], Blocked="false",
        RemoteIpDetails={"IpAddressV4": "198.51.100.7/24"})))
    for fact in (activity.resource_role, activity.connection_direction, activity.blocked, activity.remote_ip):
        assert (fact.status, fact.value, fact.source_value) == (GuardDutyFactStatus.INVALID, None, None)
    assert activity.action_type.status is GuardDutyFactStatus.RECORDED


@pytest.mark.parametrize("action", ["NETWORK_CONNECTION", ["x"], {"ActionType": ""}, {"ActionType": 5}])
def test_malformed_action_shapes_are_invalid(action):
    activity = _activity(_service(action=action))
    assert activity.action_type.status is GuardDutyFactStatus.INVALID


@pytest.mark.parametrize("block", ["x", {"RemoteIpDetails": "x"}])
def test_malformed_network_blocks_make_network_facts_invalid(block):
    action = {"ActionType": "NETWORK_CONNECTION", "NetworkConnectionAction": block}
    activity = _activity(_service(action=action))
    assert activity.remote_ip.status is GuardDutyFactStatus.INVALID
    if block == "x":
        assert activity.blocked.status is GuardDutyFactStatus.INVALID


@pytest.mark.parametrize("service", [None, [], "", 0, False])
def test_explicit_falsy_malformed_service_makes_every_fact_invalid_and_keeps_the_finding(service):
    # 종전에는 `Service or {}` 로 접혀 모두 ABSENT 로 보였다. 시각은 기존 규칙대로 CreatedAt/UpdatedAt 로 대체된다
    event = _norm(raw(Service=service))
    activity = event.payload.activity
    for fact in (activity.resource_role, activity.action_type, activity.connection_direction,
                 activity.remote_ip, activity.blocked):
        assert (fact.status, fact.value, fact.source_value) == (GuardDutyFactStatus.INVALID, None, None)
    assert (event.payload.first_seen_at, event.payload.last_seen_at) == (T0, T0)


def test_omitted_service_stays_absent():
    finding = raw()
    del finding["Service"]
    activity = _norm(finding).payload.activity
    for fact in (activity.resource_role, activity.action_type, activity.connection_direction,
                 activity.remote_ip, activity.blocked):
        assert fact.status is GuardDutyFactStatus.ABSENT


def test_added_facts_do_not_change_dedup_target_or_baseline_fields():
    plain = _norm(raw())
    rich = _norm(raw(Service=_service("ACTOR", _network())))
    assert plain.deduplication_key == rich.deduplication_key
    assert plain.target_arn == rich.target_arn
    assert plain.payload.model_dump(exclude={"activity"}) == rich.payload.model_dump(exclude={"activity"})
    decided = [evaluate_threat(e).model_dump(exclude={"threat_event_id"}) for e in (plain, rich)]
    assert decided[0] == decided[1]


def test_unrecognized_source_value_is_truncated_to_fit_the_schema():
    activity = _activity(_service("X" * 500))
    assert activity.resource_role.source_value == "X" * 64


def test_high_keeps_the_shared_mode_and_the_gate_stops_auto_isolation():
    # 대응 경로는 위험도에서 파생한다(계약). 자동 격리를 막는 것은 workflows 의 위협 유형 관문이다
    assert evaluate_threat(_norm(raw(severity=8.0))).response_mode is ResponseMode.PRE_MITIGATION_0_5S
