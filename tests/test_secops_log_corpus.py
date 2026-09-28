"""Offline corpus semantics and current schema/risk boundary; no app startup."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import textwrap
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
    {"message": "pam_unix(sshd:auth): authentication failure; rhost:8.8.8.8"},
    {"message": "Accepted publickey for user-001 from 198.51.100.10 port 41000 ssh2: ED25519 SHA256:secret"},
    {"message": "Accepted password for realusername from 198.51.100.10 port 41000 ssh2"},
    {"message": "pam_unix(sshd:session): session opened for user user-001(uid=REDACTED) by user-001(uid=0)"},
    {"message": "pam_unix(sshd:auth): authentication failure; uid=REDACTED euid=0 rhost=198.51.100.10"},
    {"host": "private-original-host"},
    {"_MACHINE_ID": "original-private-metadata"},
])
def test_secops_log_corpus_rejects_unredacted_public_records(change):
    with pytest.raises(ValueError):
        corpus.check_public_records([record(**change)])


@pytest.mark.parametrize("message", [
    "Invalid user {user} from 203.0.113.10 port 41000",
    "Connection closed by invalid user {user} 203.0.113.10 port 41000 [preauth]",
    "pam_unix(sshd:session): session opened for user {user}(uid=REDACTED) by user-001(uid=REDACTED)",
    "pam_unix(sshd:session): session opened for user user-001(uid=REDACTED) by {user}(uid=REDACTED)",
    "pam_unix(sshd:session): session closed for user {user}",
    "pam_unix(sshd:auth): authentication failure; user={user} rhost=203.0.113.10",
    "pam_unix(sshd:auth): authentication failure; ruser={user} rhost=203.0.113.10",
    "pam_unix(sshd:auth): authentication failure; logname={user} rhost=203.0.113.10",
    "Disconnecting invalid user {user} 198.51.100.10 port 41000: Too many authentication failures [preauth]",
    "input_userauth_request: invalid user {user} [preauth]",
    "error: maximum authentication attempts exceeded for {user} from 198.51.100.10 port 41000 ssh2 [preauth]",
    "Disconnected from authenticating user {user} 198.51.100.10 port 41000 [preauth]",
    "error: PAM: Authentication failure for {user} from 198.51.100.10",
    "subsystem request for sftp by user {user}",
])
def test_secops_log_corpus_auxiliary_user_fields_require_aliases(message):
    # 각 위치에서 별칭은 허용하고, 비식별화되지 않은 사용자명은 거부한다.
    corpus.check_public_records([record(message=message.format(user="user-002"))])
    with pytest.raises(ValueError, match="unredacted.*user"):
        corpus.check_public_records([record(message=message.format(user="example.person"))])


@pytest.mark.parametrize("address", ["203.0.113.10", "2001:db8::1"])
def test_secops_log_corpus_connection_without_user_does_not_treat_ip_as_username(scope, address):
    row = record(message=f"Connection closed by {address} port 41000 [preauth]")
    corpus.check_public_records([row])
    # 익명화 검사 통과와 집계기가 지원하는 메시지 형식은 별개다.
    summary = corpus.aggregate([row], scope)
    assert summary["unsupported"] == 1
    assert summary["failed"] == 0


@pytest.mark.parametrize("candidate", ["2001:db8::1.", "2001:db8:::1", "2001:db8:1:2:3"])
def test_secops_log_corpus_invalid_ip_candidate_has_a_stable_rejection_reason(candidate):
    row = record(message=f"pam_unix(sshd:auth): authentication failure; detail={candidate}")
    with pytest.raises(ValueError, match="^invalid IP candidate$"):
        corpus.check_public_records([row])


@pytest.mark.parametrize("address", ["198.51.100.10", "2001:db8::1"])
@pytest.mark.parametrize("port", [4100, 41001])
def test_secops_log_corpus_disconnect_port_and_reason_are_not_an_ip(scope, address, port):
    row = record(message=f"Received disconnect from {address} port {port}:11: disconnected by user")
    corpus.check_public_records([row])
    # 공개 표본 검사 통과가 집계기의 지원 형식을 늘리지는 않는다.
    summary = corpus.aggregate([row], scope)
    assert summary["unsupported"] == 1
    assert summary["failed"] == 0


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700::1111"])
def test_secops_log_corpus_disconnect_still_checks_the_source_address(address):
    row = record(message=f"Received disconnect from {address} port 41001:11: disconnected by user")
    with pytest.raises(ValueError, match="^non-documentation IP$"):
        corpus.check_public_records([row])


def test_secops_log_corpus_port_like_token_is_only_ignored_in_disconnect_context():
    row = record(message="pam_unix(sshd:auth): authentication failure; detail=41001:11:")
    with pytest.raises(ValueError, match="^invalid IP candidate$"):
        corpus.check_public_records([row])


@pytest.mark.parametrize("time", ["00:00:00", "06:29:15", "23:59:59"])
def test_secops_log_corpus_clock_time_is_not_an_ip(time):
    corpus.check_public_records([record(message=f"pam_unix(sshd:auth): authentication failure; time={time}")])


@pytest.mark.parametrize("address", ["aa:bb:cc:dd:ee:ff", "00:01:02:03:04:05"])
def test_secops_log_corpus_mac_address_has_its_own_rejection_reason(address):
    row = record(message=f"pam_unix(sshd:auth): authentication failure; detail={address}")
    with pytest.raises(ValueError, match="^unredacted MAC address$"):
        corpus.check_public_records([row])


@pytest.mark.parametrize("message", [
    "Accepted password for root from 198.51.100.10 port 41000 ssh2",
    "pam_unix(sshd:auth): authentication failure; user=root rhost=198.51.100.10",
])
def test_secops_log_corpus_system_accounts_also_require_aliases(message):
    with pytest.raises(ValueError, match="unredacted.*user"):
        corpus.check_public_records([record(message=message)])


@pytest.mark.parametrize("message", [
    "Accepted password for user-001 from {ip} port 41000 ssh2",
    "Invalid user mock-user from {ip} port 41000",
    "Connection closed by invalid user mock-user {ip} port 41000 [preauth]",
    "pam_unix(sshd:auth): authentication failure; logname= ruser= rhost={ip} user=user-001",
])
def test_secops_log_corpus_ipv6_is_checked_in_auth_and_auxiliary_fields(message):
    corpus.check_public_records([record(message=message.format(ip="2001:db8::1"))])
    with pytest.raises(ValueError, match="non-documentation IP"):
        corpus.check_public_records([record(message=message.format(ip="2606:4700::1111"))])


@pytest.mark.parametrize("address, allowed", [
    ("2001:0DB8:0000:0000:0000:0000:0000:0001", True),
    ("2001:db8:ffff:ffff:ffff:ffff:ffff:ffff", True),
    ("2001:db8:0:0:0:0:192.0.2.1", True),
    ("2001:db9::1", False),
    ("2001:db9:0:0:0:0:192.0.2.1", False),
    ("1111:2222:3333:4444:5555:6666:7777:8888", False),
    ("00:01:02:03:04:05:06:07", False),
    ("06::29:15", False),
    ("fd00::1", False),
    ("::1", False),
    ("::ffff:198.51.100.10", False),
    ("::ffff:8.8.8.8", False),
    ("0:0:0:0:0:ffff:198.51.100.10", False),
])
def test_secops_log_corpus_ipv6_public_range_and_spelling(address, allowed):
    row = record(message=f"Failed password for mock-user from {address} port 41000 ssh2")
    if allowed:
        corpus.check_public_records([row])
    else:
        with pytest.raises(ValueError, match="non-documentation IP"):
            corpus.check_public_records([row])


@pytest.mark.parametrize("address", ["fe80::1%eth0", "2001:db8::1%private-interface"])
def test_secops_log_corpus_ip_interface_has_its_own_rejection_reason(address):
    row = record(message=f"Failed password for mock-user from {address} port 41000 ssh2")
    with pytest.raises(ValueError, match="^IP interface identifier is not allowed$"):
        corpus.check_public_records([row])


def test_secops_log_corpus_prepare_imports_do_not_depend_on_runtime_validation(tmp_path):
    # 검증기가 앱 모듈을 읽지 않아도 준비 명령이 자신의 의존성을 로드해야 한다.
    code = textwrap.dedent("""
        import sys
        from pathlib import Path

        repo, inbox = map(Path, sys.argv[1:])
        runtime_paths = {repo / "packages", repo / "apps" / "core-api"}
        sys.path = [p for p in sys.path if Path(p).resolve() not in runtime_paths]
        sys.path.insert(0, str(repo / "scripts"))
        import secops_log_corpus as corpus

        corpus.check_runtime = lambda *args: None
        path = corpus.prepare_case(
            "C01", inbox, "arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0a1b2c3d4e5f00001",
            "2026-09-17T00:00:00Z",
        )
        print(path)
    """)
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(corpus.REPO), str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    path = Path(result.stdout.strip())
    assert path.parent == tmp_path
    submission = json.loads(path.read_text(encoding="utf-8"))
    assert submission["observation"]["failed_attempt_count"] == 120
    assert submission["log_evidence"]["case_id"] == "C01"


@pytest.mark.parametrize("extra", ["excluded", "duplicate"])
def test_secops_log_corpus_prepare_rejects_excluded_or_duplicate_records_before_delivery(tmp_path, monkeypatch, extra):
    destination, inbox = tmp_path / "corpus", tmp_path / "inbox"
    corpus.build(corpus.CORPUS, destination)
    folder = destination / "cases/C01"
    rows = corpus.read_records(folder / "logs.jsonl")
    additional = dict(rows[0])
    if extra == "excluded":
        manifest = corpus.read_json(folder / "manifest.json")
        additional.update(record_id="outside-window", source_time=manifest["selection"]["end"])
        # 기대값의 행 수도 맞춰 제외 행에 대한 고정 제한 자체를 확인한다.
        expected = corpus.read_json(folder / "expected.json")
        expected["aggregate"]["record_count"] += 1
        (folder / "expected.json").write_bytes(corpus.json_bytes(expected))
    (folder / "logs.jsonl").write_bytes(corpus.records_bytes([*rows, additional]))
    monkeypatch.setattr(corpus, "CORPUS", destination)

    with pytest.raises(ValueError, match="^C01: unexpected exclusion/duplicate$"):
        corpus.prepare_case("C01", inbox, "test-target")
    assert not inbox.exists()


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
