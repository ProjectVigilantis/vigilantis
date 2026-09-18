"""Offline corpus semantics and current schema/risk boundary; no app startup."""

from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import secops_log_corpus as corpus


@pytest.fixture
def scope():
    return {"purpose": "threat_boundary", "coverage": {"state": "complete"},
            "selection": {"host": "mock-ec2-C01", "source_ip": "203.0.113.10",
                          "time_field": "source_time", "start": "2026-01-01T00:00:00Z",
                          "end": "2026-01-01T00:00:10Z"},
            "event_id": "test-event", "target_arn": "test-target"}


def record(record_id="a", at="2026-01-01T00:00:01Z", message=None, **changes):
    row = {"record_id": record_id, "source_time": at,
           "received_at": "2026-01-01T00:00:20Z", "host": "mock-ec2-C01",
           "process_id": 10001,
           "message": message or "Failed password for invalid user mock-user from 203.0.113.10 port 41000 ssh2"}
    row.update(changes)
    return row


def test_secops_log_corpus_all_nine_cases_cross_current_contract():
    rows = corpus.check(corpus.CORPUS)
    assert [r["case_id"] for r in rows] == list(corpus.CASE_IDS)
    assert sum(r["record_count"] for r in rows) == 2201
    assert sum(r["failed"] for r in rows) == 1311
    assert sum(r["accepted"] for r in rows) == 2
    assert sum(r["ingress_contract_checked"] for r in rows) == 7


def test_secops_log_corpus_rebuild_is_byte_identical_and_preserves_oracle(tmp_path):
    oracle = (corpus.CORPUS / "expectations.json").read_bytes()
    assert corpus.build(corpus.CORPUS, tmp_path) == 34
    assert len(corpus.check(tmp_path)) == 9
    assert (corpus.CORPUS / "expectations.json").read_bytes() == oracle


def test_secops_log_corpus_sparse_failures_use_short_separate_connections():
    rows = corpus.read_records(corpus.CORPUS / "cases/C06/logs.jsonl")
    groups = {}
    for row in rows:
        groups.setdefault(row["process_id"], []).append(row)
    assert len(groups) == 5
    for records in groups.values():
        times = [corpus.timestamp(r["source_time"]) for r in records]
        assert (max(times) - min(times)).total_seconds() < 1
        assert sum(r["message"].startswith("Failed password ") for r in records) == 1


def test_secops_log_corpus_source_clock_half_open_window_and_target_filter(scope):
    rows = [record("start", "2026-01-01T09:00:00+09:00"),
            record("last", "2026-01-01T00:00:09.999999Z"),
            record("end", "2026-01-01T00:00:10Z"),
            record("before", "2025-12-31T23:59:59.999999Z"),
            record("host", host="mock-ec2-C02"),
            record("ip", message="Failed password for mock-user from 198.51.100.77 port 41000 ssh2")]
    got = corpus.aggregate(rows, scope)
    assert got["failed"] == 2  # Receipt times are deliberately outside the window.
    assert got["excluded"] == {"window": 2, "host": 1, "source_ip": 1}


def test_secops_log_corpus_delivery_identity_does_not_collapse_repeated_text(scope):
    first = record()
    got = corpus.aggregate([first, dict(first), record("b")], scope)
    assert got["failed"] == 2
    assert got["duplicate_deliveries"] == 1
    with pytest.raises(ValueError, match="conflicting duplicate"):
        corpus.aggregate([first, record(message="Accepted password for mock-user from 203.0.113.10 port 41000 ssh2")], scope)


def test_secops_log_corpus_auxiliary_partial_and_unsupported_methods_are_not_failures(scope):
    messages = [
        "Failed password for invalid user mock-user from 203.0.113.10 port 41000 ssh2",
        "Invalid user mock-user from 203.0.113.10 port 41000",
        "pam_unix(sshd:auth): authentication failure; rhost=203.0.113.10",
        "Connection closed by invalid user mock-user 203.0.113.10 port 41000 [preauth]",
        "Accepted publickey for mock-user from 203.0.113.10 port 41000 ssh2: ED25519 KEY_FINGERPRINT_REDACTED",
        "Partial password for mock-user from 203.0.113.10 port 41000 ssh2",
        "Postponed publickey for mock-user from 203.0.113.10 port 41000 ssh2",
        "Failed none for mock-user from 203.0.113.10 port 41000 ssh2",
        "Failed publickey for mock-user from 203.0.113.10 port 41000 ssh2",
    ]
    got = corpus.aggregate([record(str(i), message=m) for i, m in enumerate(messages)], scope)
    assert (got["failed"], got["accepted"], got["auxiliary"], got["unsupported"]) == (1, 1, 3, 4)
    with pytest.raises(ValueError, match="unsupported records"):
        corpus.observation(scope, got)


@pytest.mark.parametrize("state", ["not_collected", "failed"])
def test_secops_log_corpus_unavailable_is_not_observed_zero(scope, state):
    scope["coverage"]["state"] = state
    got = corpus.aggregate([], scope)
    assert got["failed"] is None and got["accepted"] is None
    with pytest.raises(ValueError, match="incomplete source"):
        corpus.observation(scope, got)
    with pytest.raises(ValueError, match="cannot contain records"):
        corpus.aggregate([record()], scope)


def test_secops_log_corpus_partial_preserves_observed_count_without_projecting(scope):
    scope["coverage"]["state"] = "partial"
    got = corpus.aggregate([record()], scope)
    assert got["failed"] == 1
    with pytest.raises(ValueError, match="incomplete source"):
        corpus.observation(scope, got)


def test_secops_log_corpus_acceptance_control_does_not_make_invalid_zero_threat(scope):
    scope["purpose"] = "acceptance_control"
    scope["coverage"]["state"] = "sampled"
    got = corpus.aggregate([record(message="Accepted password for mock-user from 203.0.113.10 port 41000 ssh2")], scope)
    assert got["failed"] == 0
    assert corpus.observation(scope, got) is None


@pytest.mark.parametrize("change", [
    {"message": "Accepted password for user-001 from 8.8.8.8 port 41000 ssh2"},
    {"message": "Accepted publickey for user-001 from 198.51.100.10 port 41000 ssh2: ED25519 SHA256:secret"},
    {"message": "Accepted password for realusername from 198.51.100.10 port 41000 ssh2"},
    {"host": "private-original-host"},
    {"_MACHINE_ID": "original-private-metadata"},
])
def test_secops_log_corpus_rejects_unredacted_public_records(change):
    with pytest.raises(ValueError):
        corpus.check_public_records([record(**change)])


@pytest.mark.parametrize("relative", ["cases/C01/observation.json", "cases/C01/expected.json"])
def test_secops_log_corpus_drift_in_projection_or_oracle_fails(tmp_path, relative):
    destination = tmp_path / "corpus"
    shutil.copytree(corpus.CORPUS, destination)
    path = destination / relative
    value = corpus.read_json(path)
    if "observation" in relative:
        value["failed_attempt_count"] += 1
    else:
        value["aggregate"]["failed"] += 1
    path.write_bytes(corpus.json_bytes(value))
    with pytest.raises(ValueError):
        corpus.check(destination)


def test_secops_log_corpus_bad_generation_target_cannot_rewrite_expectations(tmp_path):
    destination = tmp_path / "corpus"
    shutil.copytree(corpus.CORPUS, destination)
    oracle = (destination / "expectations.json").read_bytes()
    specs = corpus.read_json(destination / "case-specs.json")
    specs[0]["failed_count"] = 60
    (destination / "case-specs.json").write_bytes(corpus.json_bytes(specs))
    corpus.build(destination, destination)
    assert (destination / "expectations.json").read_bytes() == oracle
    with pytest.raises(ValueError, match="differs from expectation"):
        corpus.check(destination, source=destination)


def test_secops_log_corpus_control_cannot_accidentally_include_ingress_file(tmp_path):
    corpus.build(corpus.CORPUS, tmp_path)
    (tmp_path / "cases/N01/observation.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact set differs"):
        corpus.check(tmp_path)


def test_secops_log_corpus_rejects_ambiguous_clock(scope):
    bad = copy.deepcopy(scope)
    bad["selection"]["time_field"] = "received_at"
    with pytest.raises(ValueError, match="selection clock"):
        corpus.aggregate([record()], bad)
    with pytest.raises(ValueError, match="timezone"):
        corpus.aggregate([record(at="2026-01-01T00:00:01")], scope)
