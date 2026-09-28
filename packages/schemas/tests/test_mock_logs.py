"""모의 로그와 관측·저장 이벤트의 일치 계약을 검증한다."""

import pytest
from pydantic import ValidationError
from schemas.events import OpenIpThreatInput, SshBruteForceThreatInput
from schemas.evidence import ThreatEvidence
from schemas.mock_logs import MockSshLogEvidence


@pytest.fixture
def observation():
    return SshBruteForceThreatInput(
        event_id="mock-ssh", target_arn="mock-target", source_ip="203.0.113.10",
        occurred_at="2026-09-17T00:01:00Z", failed_attempt_count=1, window_seconds=60,
    )


@pytest.fixture
def logs(observation):
    return MockSshLogEvidence(
        case_id="C01", corpus_fingerprint="a" * 64, source_fingerprint="b" * 64,
        source_id="aws-directory-service-password-example",
        selection_policy="ssh-observed-password-results-v1", host="mock-ec2-C01",
        target_arn=observation.target_arn, source_ip=observation.source_ip,
        window_start="2026-09-17T00:00:00Z", window_end=observation.occurred_at,
        record_count=1, failed_attempt_count=1, accepted_count=0, auxiliary_count=0,
        records=[{
            "record_id": "C01-000001", "host": "mock-ec2-C01", "process_id": 10001,
            "source_time": "2026-09-17T00:00:30Z", "received_at": "2026-09-17T00:00:31Z",
            "message": "Failed password for mock-user from 203.0.113.10 port 41000 ssh2",
        }],
    )


def stored_evidence(observation, logs):
    # 저장 형식은 관측의 공통 필드와 유형별 payload를 분리한다.
    return {
        "event": {
            "threat_event_id": "stored-ssh", "source_event_id": observation.event_id,
            "event_type": observation.event_type, "target_arn": observation.target_arn,
            "occurred_at": observation.occurred_at, "collected_at": observation.occurred_at,
            "deduplication_key": "mock-dedup",
            "payload": observation.model_dump(exclude={"event_id", "event_type", "target_arn", "occurred_at"}),
        },
        "context": {
            "captured_at": observation.occurred_at, "target_status": "not_collected",
            "log_evidence": logs.model_dump(),
        },
    }


def test_log_matches_flat_observation_and_nested_stored_event(observation, logs):
    assert logs.matches_observation(observation)
    evidence = ThreatEvidence.model_validate(stored_evidence(observation, logs))
    assert evidence.context.log_evidence == logs
    assert ThreatEvidence.model_validate_json(evidence.model_dump_json()) == evidence


@pytest.mark.parametrize("field, value", [
    ("target_arn", "another-target"),
    ("source_ip", "198.51.100.1"),
    ("occurred_at", "2026-09-17T00:02:00Z"),
    ("failed_attempt_count", 2),
    ("window_seconds", 61),
])
def test_log_mismatch_is_rejected_in_both_event_layouts(observation, logs, field, value):
    changed = SshBruteForceThreatInput.model_validate({**observation.model_dump(), field: value})
    assert not logs.matches_observation(changed)
    with pytest.raises(ValidationError, match="log evidence differs from threat event"):
        ThreatEvidence.model_validate(stored_evidence(changed, logs))


def test_log_comparison_keeps_same_instant_with_different_timezone(observation, logs):
    same = SshBruteForceThreatInput.model_validate({
        **observation.model_dump(), "occurred_at": "2026-09-17T09:01:00+09:00",
    })
    assert logs.matches_observation(same)
    assert ThreatEvidence.model_validate(stored_evidence(same, logs)).context.log_evidence == logs


def test_ssh_log_cannot_be_attached_to_open_ip_event(observation, logs):
    other = OpenIpThreatInput(
        event_id="mock-open", target_arn=observation.target_arn, occurred_at=observation.occurred_at,
        protocol="tcp", from_port=22, to_port=22, source_cidr="0.0.0.0/0",
    )
    assert not logs.matches_observation(other)
    with pytest.raises(ValidationError, match="log evidence differs from threat event"):
        ThreatEvidence.model_validate(stored_evidence(other, logs))
