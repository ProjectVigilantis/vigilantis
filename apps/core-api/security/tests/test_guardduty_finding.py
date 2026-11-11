"""GuardDuty 탐지 정형화(threat_normalizer.normalize_guardduty_finding)와 심각도 판정(risk_evaluator) — #588 C · Q2."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(_ROOT / "apps" / "core-api"), str(_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schemas.api.incidents import ResponseMode, RiskLevel  # noqa: E402
from schemas.events import RiskReasonCode, ThreatEventType  # noqa: E402
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
    assert (p.finding_id, p.finding_type, p.severity, p.resource_type, p.region) == (
        "f-1", "UnauthorizedAccess:EC2/SSHBruteForce", 5.0, "Instance", REGION)
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


@pytest.mark.parametrize("override", [
    {"AccountId": "999988887777"},   # 위임 관리자 계정이 돌려준 멤버 계정 탐지
    {"Region": "us-east-1"},
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


def test_high_keeps_the_shared_mode_and_the_gate_stops_auto_isolation():
    # 대응 경로는 위험도에서 파생한다(계약). 자동 격리를 막는 것은 workflows 의 위협 유형 관문이다
    assert evaluate_threat(_norm(raw(severity=8.0))).response_mode is ResponseMode.PRE_MITIGATION_0_5S
