"""위협 정형화 단위 테스트 — DB·LocalStack 불필요. (Issue #268)

정형화가 프로덕션에 없던 동안 이 변환은 테스트 헬퍼 2벌로만 존재했고, 골든 SecOps
정답 12건은 프로덕션이 공유하지 않는 변환을 검증했다. 이 파일은 그 변환 자체를 본다.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schemas.events import (  # noqa: E402
    OpenIpThreatPayload,
    SshBruteForceThreatPayload,
    ThreatEventType,
)
from security.threat_normalizer import normalize_mock_input  # noqa: E402

GOLDEN_INPUT = REPO_ROOT / "datasets" / "golden" / "secops" / "input"

OPEN_IP_RAW = {
    "event_id": "evt-open-ip-x",
    "event_type": "OPEN_IP",
    "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-0abc",
    "occurred_at": "2026-08-31T00:00:00Z",
    "protocol": "tcp",
    "from_port": 22,
    "to_port": 22,
    "source_cidr": "0.0.0.0/0",
}

SSH_RAW = {
    "event_id": "evt-ssh-x",
    "event_type": "SSH_BRUTE_FORCE",
    "target_arn": "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc",
    "source_ip": "203.0.113.9",
    "occurred_at": "2026-08-31T00:00:00Z",
    "failed_attempt_count": 30,
    "window_seconds": 60,
}


# --- 유형별 정형화 ---


def test_open_ip_maps_to_its_payload():
    event = normalize_mock_input(OPEN_IP_RAW)
    assert event.event_type is ThreatEventType.OPEN_IP
    assert isinstance(event.payload, OpenIpThreatPayload)
    assert event.payload.source_cidr == "0.0.0.0/0"
    assert event.source_event_id == "evt-open-ip-x"   # 입력 event_id 보존
    assert event.target_arn == OPEN_IP_RAW["target_arn"]


def test_ssh_maps_to_its_payload():
    event = normalize_mock_input(SSH_RAW)
    assert event.event_type is ThreatEventType.SSH_BRUTE_FORCE
    assert isinstance(event.payload, SshBruteForceThreatPayload)
    assert event.payload.failed_attempt_count == 30


# --- 서버가 정하는 값 셋 ---


def test_threat_event_id_is_a_uuid():
    """threat_events.threat_event_id 는 PG uuid 다. 다른 형식은 적재에서 캐스트 오류가
    된다 — 종전 테스트 헬퍼의 `te-{event_id}` 는 저장할 수 없는 값이었다."""
    event = normalize_mock_input(SSH_RAW)
    uuid.UUID(event.threat_event_id)  # 형식이 아니면 ValueError


def test_threat_event_id_is_fresh_each_call():
    a = normalize_mock_input(SSH_RAW)
    b = normalize_mock_input(SSH_RAW)
    assert a.threat_event_id != b.threat_event_id


def test_collected_at_defaults_to_now_not_occurred_at():
    """수집 시각과 발생 시각은 다른 축이다. 늦게 도착한 위협에서 갈린다."""
    before = datetime.now(timezone.utc)
    event = normalize_mock_input(SSH_RAW)
    assert event.occurred_at == datetime(2026, 8, 31, tzinfo=timezone.utc)
    assert event.collected_at >= before
    assert event.collected_at != event.occurred_at


def test_collected_at_is_injectable():
    fixed = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    assert normalize_mock_input(SSH_RAW, collected_at=fixed).collected_at == fixed


# --- 중복 억제 키 ---


def test_same_observation_yields_same_key_though_event_id_differs():
    """같은 관측이 두 번 배달되면 event_id 가 달라도 같은 키다 — 생산자가 id 를
    안정적으로 재발급한다는 전제 없이 재배달을 접는다."""
    first = normalize_mock_input({**SSH_RAW, "event_id": "evt-1"})
    second = normalize_mock_input({**SSH_RAW, "event_id": "evt-2"})
    assert first.deduplication_key == second.deduplication_key


def test_escalated_reobservation_is_a_new_key():
    """같은 대상·같은 공격 IP 라도 강도와 시각이 다르면 다른 관측이다.

    골든이 이 쌍을 실제로 갖는다 — S3(1회) 20분 뒤 S6(60회)는 LOW → MEDIUM 으로
    위험도가 오른다. 자원 정체만으로 키를 만들면 이 재관측이 중복으로 사라진다.
    """
    weak = normalize_mock_input(
        {**SSH_RAW, "failed_attempt_count": 1, "window_seconds": 1,
         "occurred_at": "2026-08-31T06:40:00Z"}
    )
    strong = normalize_mock_input(
        {**SSH_RAW, "failed_attempt_count": 60, "window_seconds": 600,
         "occurred_at": "2026-08-31T07:00:00Z"}
    )
    assert weak.deduplication_key != strong.deduplication_key


def test_same_instant_in_another_offset_is_the_same_key():
    """+09:00 표기와 Z 표기는 같은 순간이다. 표기 차이로 재배달이 갈리면 안 된다."""
    utc = normalize_mock_input({**SSH_RAW, "occurred_at": "2026-08-31T00:00:00Z"})
    kst = normalize_mock_input({**SSH_RAW, "occurred_at": "2026-08-31T09:00:00+09:00"})
    assert utc.deduplication_key == kst.deduplication_key


@pytest.mark.parametrize("field, value", [
    ("target_arn", "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-other"),
    ("source_ip", "198.51.100.7"),
    ("failed_attempt_count", 31),
    ("window_seconds", 61),
    ("occurred_at", "2026-08-31T00:00:01Z"),
])
def test_ssh_key_changes_with_identity(field, value):
    base = normalize_mock_input(SSH_RAW)
    other = normalize_mock_input({**SSH_RAW, field: value})
    assert base.deduplication_key != other.deduplication_key


@pytest.mark.parametrize("field, value", [
    ("target_arn", "arn:aws:ec2:ap-northeast-2:123456789012:security-group/sg-other"),
    ("protocol", "udp"),
    ("from_port", 3389),
    ("to_port", 3389),
    ("source_cidr", "10.0.0.0/8"),
    ("occurred_at", "2026-08-31T00:00:01Z"),
])
def test_open_ip_key_changes_with_identity(field, value):
    base = normalize_mock_input(OPEN_IP_RAW)
    other = normalize_mock_input({**OPEN_IP_RAW, field: value})
    assert base.deduplication_key != other.deduplication_key


def test_open_ip_port_none_is_distinct_from_zero():
    """전 포트(None)와 0번 포트를 같은 키로 접으면 서로 다른 위협이 하나로 사라진다."""
    any_port = normalize_mock_input({**OPEN_IP_RAW, "from_port": None, "to_port": None})
    zero_port = normalize_mock_input({**OPEN_IP_RAW, "from_port": 0, "to_port": 0})
    assert any_port.deduplication_key != zero_port.deduplication_key


def test_key_fits_the_column():
    """threat_events.deduplication_key 는 String(512)·UNIQUE 다. 잘리면 서로 다른
    위협이 같은 키가 되어 진짜 위협이 조용히 사라진다."""
    long_arn = "arn:aws:ec2:ap-northeast-2:123456789012:instance/" + "i" * 400
    event = normalize_mock_input({**SSH_RAW, "target_arn": long_arn})
    assert len(event.deduplication_key) <= 512


def test_key_is_prefixed_by_event_type():
    assert normalize_mock_input(SSH_RAW).deduplication_key.startswith("SSH_BRUTE_FORCE|")
    assert normalize_mock_input(OPEN_IP_RAW).deduplication_key.startswith("OPEN_IP|")


# --- 입력 계약 ---


def test_schema_key_is_stripped():
    """골든 JSON 은 편집기용 $schema 를 갖는다. 입력 모델은 extra=forbid 다."""
    event = normalize_mock_input({"$schema": "../../schema/x.json", **SSH_RAW})
    assert event.source_event_id == "evt-ssh-x"


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        normalize_mock_input({**SSH_RAW, "severity": "HIGH"})


def test_missing_required_field_is_rejected():
    raw = {k: v for k, v in SSH_RAW.items() if k != "source_ip"}
    with pytest.raises(ValidationError):
        normalize_mock_input(raw)


# --- 골든 전량 ---


@pytest.mark.parametrize("path", sorted(GOLDEN_INPUT.glob("*.json")), ids=lambda p: p.name)
def test_every_golden_input_normalizes(path: Path):
    """골든 SecOps 입력 12건이 모두 정형화를 통과한다."""
    with path.open(encoding="utf-8") as fp:
        event = normalize_mock_input(json.load(fp))
    uuid.UUID(event.threat_event_id)
    assert event.deduplication_key
    assert event.source_event_id


def test_golden_inputs_have_distinct_keys():
    """골든 12건은 서로 다른 관측이다 — 겹치면 판단 밴드가 하나 사라진다.

    S2/S7 과 S3/S6 은 대상·공격 IP 가 같고 강도와 시각만 다른 쌍이다. 자원 정체만으로
    키를 만들면 이 네 건이 두 건으로 접힌다(#268 구현 중 실제로 그렇게 접혔다).
    """
    keys = set()
    for path in sorted(GOLDEN_INPUT.glob("*.json")):
        with path.open(encoding="utf-8") as fp:
            keys.add(normalize_mock_input(json.load(fp)).deduplication_key)
    assert len(keys) == len(list(GOLDEN_INPUT.glob("*.json")))
