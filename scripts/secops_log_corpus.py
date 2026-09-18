"""SSH corpus build/check and explicit mock inbox preparation. No DB/cloud/model calls.

case-specs.json describes construction; expectations.json is a separately
authored oracle. build never derives or updates that oracle from code results.
This deliberately narrow parser is a fixture tool, not an operational detector.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "datasets" / "secops-log-corpus"
CASE_IDS = tuple(f"C{i:02}" for i in range(1, 8)) + ("N01", "N02")
POLICY = "ssh-observed-password-results-v1"
AUTH = re.compile(
    r"^(Failed|Accepted|Partial|Postponed) (\S+) for (?:invalid user )?"
    r"(?P<user>\S+) from (?P<ip>\S+) port (?P<port>\d+) ssh2(?:[: ].*)?$"
)
AUX = re.compile(
    r"^(?:Invalid user \S+ from \S+ port \d+|"
    r"Connection closed by invalid user \S+ \S+ port \d+ \[preauth\]|"
    r"pam_unix\(sshd:(?:auth|session)\): .+)$"
)
DOC_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def fingerprint(value: object) -> str:
    """Semantic source identity, independent of a checkout's line endings."""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def records_bytes(records: list[dict]) -> bytes:
    return ("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in records)).encode()


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "timestamp must include a timezone")
    return result.astimezone(UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def window(manifest: dict) -> tuple[datetime, datetime]:
    scope = manifest["selection"]
    require(scope["time_field"] == "source_time", "unsupported selection clock")
    start, end = timestamp(scope["start"]), timestamp(scope["end"])
    require(end > start, "selection window must be positive")
    return start, end


def aggregate(records: list[dict], manifest: dict) -> dict:
    """Count observed results; source-record IDs deduplicate deliveries, not text."""
    state = manifest["coverage"]["state"]
    require(state in {"complete", "sampled", "partial", "not_collected", "failed"}, "unknown coverage")
    start, end = window(manifest)
    if state in {"not_collected", "failed"}:
        require(not records, "unavailable source cannot contain records")
        return {"record_count": 0, "failed": None, "accepted": None, "auxiliary": 0,
                "unsupported": 0, "excluded": {}, "duplicate_deliveries": 0}
    counts = Counter(failed=0, accepted=0, auxiliary=0, unsupported=0)
    excluded: Counter = Counter()
    seen: dict[str, dict] = {}
    duplicates = 0
    for row in records:
        record_id = row["record_id"]
        require(isinstance(record_id, str) and bool(record_id), "record_id must be nonempty")
        if record_id in seen:
            require(row == seen[record_id], f"conflicting duplicate record_id: {record_id}")
            duplicates += 1
            continue
        seen[record_id] = row
        at = timestamp(row["source_time"])
        timestamp(row["received_at"])
        if row["host"] != manifest["selection"]["host"]:
            excluded["host"] += 1
            continue
        if not start <= at < end:
            excluded["window"] += 1
            continue
        message = row["message"]
        match = AUTH.fullmatch(message)
        if match:
            ipaddress.ip_address(match["ip"])
            require(0 < int(match["port"]) <= 65535, "invalid source port")
            if match["ip"] != manifest["selection"]["source_ip"]:
                excluded["source_ip"] += 1
                continue
            outcome, method = match.group(1, 2)
            if outcome == "Failed" and method == "password":
                counts["failed"] += 1
            elif outcome == "Accepted" and method in {"password", "publickey"}:
                counts["accepted"] += 1
            else:
                counts["unsupported"] += 1
        elif AUX.fullmatch(message):
            # Auxiliary rows document the selected host/window. They never
            # contribute an auth result, even when containing 'failure'.
            counts["auxiliary"] += 1
        else:
            counts["unsupported"] += 1
    return {"record_count": len(seen), **counts, "excluded": dict(excluded),
            "duplicate_deliveries": duplicates}


def observation(manifest: dict, summary: dict) -> dict | None:
    """Only complete synthetic cases project to the existing ingress contract."""
    if manifest["purpose"] != "threat_boundary":
        return None
    require(manifest["coverage"]["state"] == "complete", "incomplete source cannot project a threat")
    require(summary["unsupported"] == 0, "unsupported records cannot project a threat")
    require(summary["failed"] is not None and summary["failed"] > 0, "no observed password failures")
    start, end = window(manifest)
    seconds = (end - start).total_seconds()
    require(seconds.is_integer(), "ingress window must use whole seconds")
    return {"event_id": manifest["event_id"], "event_type": "SSH_BRUTE_FORCE",
            "target_arn": manifest["target_arn"], "source_ip": manifest["selection"]["source_ip"],
            "occurred_at": stamp(end), "failed_attempt_count": summary["failed"],
            "window_seconds": int(seconds)}


def synthesize(spec: dict, golden: dict, template: str) -> tuple[dict, list[dict]]:
    count, seconds = spec["failed_count"], spec["window_seconds"]
    require(type(count) is int and 1 <= count <= 1000, "fixture count outside 1..1000")
    require(type(seconds) is int and 1 <= seconds <= 3600, "fixture window outside 1..3600")
    end = timestamp(golden["occurred_at"])
    start = end - timedelta(seconds=seconds)
    step_us = seconds * 1_000_000 // count
    require(seconds * 1_000_000 % count == 0, "fixture spacing must be exact microseconds")
    # Sparse observations must not pretend that one unauthenticated connection
    # stays open for tens of minutes. Keep failure spans within 60 seconds.
    group_size = min(3, 60_000_000 // step_us + 1)
    auxiliary_offset = timedelta(microseconds=min(step_us // 4, 1000))
    rows = []

    def add(at: datetime, pid: int, message: str) -> None:
        rows.append({"source_time": stamp(at), "received_at": stamp(at + timedelta(milliseconds=50)),
                     "host": spec["host"], "process_id": pid, "message": message})

    for i in range(count):
        at = start + timedelta(microseconds=(2 * i + 1) * step_us // 2)
        connection = i // group_size
        pid, port = 10001 + connection, 41000 + connection
        ip = golden["source_ip"]
        if i % group_size == 0:
            add(at - auxiliary_offset, pid,
                f"Invalid user mock-user from {ip} port {port}")
        add(at, pid, template.format(user="mock-user", ip=ip, port=port))
        if i % group_size == group_size - 1 or i == count - 1:
            add(at + auxiliary_offset, pid,
                f"Connection closed by invalid user mock-user {ip} port {port} [preauth]")
    rows.sort(key=lambda r: r["source_time"])
    for i, row in enumerate(rows, 1):
        row["record_id"] = f"{spec['case_id']}-{i:06}"
    return {"host": spec["host"], "source_ip": golden["source_ip"], "time_field": "source_time",
            "start": stamp(start), "end": stamp(end), "bounds": "[start,end)"}, rows


def render(source: Path, repo: Path = REPO) -> dict[str, bytes]:
    """Render generated artifacts. The human-authored oracle is read, never inferred."""
    specs = read_json(source / "case-specs.json")
    expectations = read_json(source / "expectations.json")
    registry = read_json(source / "sources" / "registry.json")
    require([s["case_id"] for s in specs] == list(CASE_IDS), "expected the nine declared cases")
    require(set(expectations) == set(CASE_IDS), "oracle must cover all nine cases")
    template = (source / "sources" / "failed-password.txt").read_text(encoding="utf-8").strip()
    outputs: dict[str, bytes] = {}
    for spec in specs:
        cid = spec["case_id"]
        manifest = {"schema_version": 1, "case_id": cid, "selection_policy": POLICY,
                    "registry_fingerprint": fingerprint(registry), "fingerprint_format": "sha256-canonical-json-v1",
                    "ai_evaluation": spec["ai_evaluation"]}
        if cid.startswith("C"):
            name = spec["golden_input"]
            require(re.fullmatch(r"evt_ssh_bruteforce_\d{3}\.json", name) is not None, "invalid Golden file")
            golden = read_json(repo / "datasets" / "golden" / "secops" / "input" / name)
            selection, rows = synthesize(spec, golden, template)
            manifest.update({"data_kind": "synthetic", "origin_provider": "official_example",
                             "source_id": "aws-directory-service-password-example",
                             "source_fingerprint": fingerprint(template), "golden_input": name,
                             "golden_fingerprint": fingerprint(golden), "event_id": golden["event_id"],
                             "target_arn": golden["target_arn"], "asset_mapping": "synthetic_aws_golden",
                             "purpose": "threat_boundary", "selection": selection,
                             "generation": {"max_failures_per_connection": 3, "max_failure_span_seconds": 60},
                             "coverage": {"state": "complete", "scope": "generated_fixture_only"},
                             "transformations": ["message_template_expansion", "synthetic_identity",
                                                 "uniform_source_times", "bounded_connection_groups",
                                                 "constructed_auxiliary_messages", "synthetic_50ms_receipt_offset"]})
        else:
            rows = read_records(source / "sources" / f"{cid}.jsonl")
            manifest.update({"data_kind": "anonymized_observation", "origin_provider": "NCP",
                             "source_id": "ncp-sshd-export-20260917", "source_fingerprint": fingerprint(rows),
                             "target_arn": None, "asset_mapping": None, "purpose": "acceptance_control",
                             "selection": spec["selection"],
                             "coverage": {"state": "sampled", "scope": "selected_process_records_only"},
                             "transformations": ["selected_three_records", "allowlisted_fields",
                                                 "identity_and_port_pseudonyms", "key_fingerprint_redacted"],
                             "source_lines": [row["source_line"] for row in rows]})
        summary = aggregate(rows, manifest)
        event = observation(manifest, summary)
        prefix = f"cases/{cid}/"
        outputs[prefix + "manifest.json"] = json_bytes(manifest)
        outputs[prefix + "logs.jsonl"] = records_bytes(rows)
        outputs[prefix + "expected.json"] = json_bytes(expectations[cid])
        if event is not None:
            outputs[prefix + "observation.json"] = json_bytes(event)
    return outputs


def build(source: Path, output: Path, repo: Path = REPO) -> int:
    outputs = render(source, repo)
    # Only this explicit file set is written; no recursive cleanup or inbox delivery.
    for relative, content in outputs.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return len(outputs)


def check_public_records(records: list[dict]) -> None:
    fields = {"record_id", "source_time", "received_at", "host", "process_id", "message", "source_line"}
    for row in records:
        require(set(row) <= fields and fields - {"source_line"} <= set(row), "unexpected record fields")
        require(type(row["process_id"]) is int and row["process_id"] > 0, "invalid process alias")
        require(re.fullmatch(r"(?:mock-ec2-C\d{2}|ncp-host-001)", row["host"]) is not None, "invalid host alias")
        message = row["message"]
        require(isinstance(message, str) and len(message) <= 512, "invalid message length")
        require("\n" not in message and "\r" not in message and "\x00" not in message, "invalid message characters")
        require("SHA256:" not in message and "PRIVATE KEY" not in message, "unredacted key material")
        for text in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", message):
            ip = ipaddress.ip_address(text)
            require(any(ip in network for network in DOC_NETWORKS), "non-documentation IP")
        match = AUTH.fullmatch(message)
        if match:
            require(re.fullmatch(r"(?:mock-user|user-\d{3})", match["user"]) is not None, "unredacted auth user")


def check_runtime(event: dict, expected: dict, golden: dict) -> None:
    for path in (REPO / "packages", REPO / "apps" / "core-api"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from schemas.events import SshBruteForceThreatInput
    from security.risk_evaluator import evaluate_threat
    from security.threat_normalizer import normalize_mock_input

    parsed = SshBruteForceThreatInput.model_validate(event)
    existing = SshBruteForceThreatInput.model_validate({k: v for k, v in golden.items() if k != "$schema"})
    require(parsed == existing, "observation drifted from existing Golden input")
    verdict = evaluate_threat(normalize_mock_input(event))
    actual = {"initial_risk_level": verdict.initial_risk_level.value,
              "response_mode": verdict.response_mode.value,
              "reason_codes": sorted(code.value for code in verdict.reason_codes)}
    require(actual == expected["initial_risk"], "runtime verdict differs from independently authored expectation")


def check(root: Path, source: Path = CORPUS, repo: Path = REPO) -> list[dict]:
    rendered = render(source, repo)
    present = {p.relative_to(root).as_posix() for p in (root / "cases").rglob("*") if p.is_file()}
    require(present == set(rendered), "case artifact set differs (missing or unexpected files)")
    results = []
    for cid in CASE_IDS:
        folder = root / "cases" / cid
        manifest, expected = read_json(folder / "manifest.json"), read_json(folder / "expected.json")
        require(manifest["selection_policy"] == POLICY, "selection policy drift")
        records = read_records(folder / "logs.jsonl")
        check_public_records(records)
        summary = aggregate(records, manifest)
        for key, value in expected["aggregate"].items():
            require(summary[key] == value, f"{cid}: {key} differs from expectation")
        require(not summary["excluded"] and not summary["duplicate_deliveries"], f"{cid}: unexpected exclusion/duplicate")
        event = observation(manifest, summary)
        if event is not None:
            saved = read_json(folder / "observation.json")
            require(saved == event, f"{cid}: saved observation differs from log aggregate")
            golden = read_json(repo / "datasets" / "golden" / "secops" / "input" / manifest["golden_input"])
            check_runtime(saved, expected, golden)
        else:
            require(expected["initial_risk"] is None, f"{cid}: acceptance control cannot claim a threat verdict")
        results.append({"case_id": cid, **{k: summary[k] for k in ("record_count", "failed", "accepted")},
                        "ingress_contract_checked": event is not None, "status": "PASS"})
    for relative, content in rendered.items():
        require((root / relative).read_bytes() == content, f"non-reproducible artifact: {relative}")
    return results


def prepare_case(case_id: str, inbox: Path, target_arn: str, occurred_at: str | None = None) -> Path:
    """Attach a bounded, deterministic excerpt to the existing file ingress.

    The fingerprint identifies the full unshifted corpus; an explicit time shift
    and target mapping describe replay. Preparation never claims DB delivery.
    """
    require(case_id in CASE_IDS[:7], "only C01..C07 project to threat input")
    check(CORPUS)
    from mock_threat_source import parse_observation, prepare_observation
    from schemas.mock_logs import MockSshLogEvidence

    folder = CORPUS / "cases" / case_id
    manifest = read_json(folder / "manifest.json")
    records = read_records(folder / "logs.jsonl")
    summary = aggregate(records, manifest)
    raw = read_json(folder / "observation.json")
    end = timestamp(raw["occurred_at"])
    shift = (timestamp(occurred_at) - end).total_seconds() if occurred_at else 0.0
    require(float(shift).is_integer(), "replay shift must use whole seconds")
    delta = timedelta(seconds=shift)
    raw.update(target_arn=target_arn, occurred_at=stamp(end + delta))
    excerpts = records if len(records) <= 12 else records[:6] + records[-6:]
    shifted = [{**row, "source_time": stamp(timestamp(row["source_time"]) + delta),
                "received_at": stamp(timestamp(row["received_at"]) + delta)} for row in excerpts]
    start, _ = window(manifest)
    logs = MockSshLogEvidence(
        case_id=case_id, corpus_fingerprint=fingerprint(records),
        source_id=manifest["source_id"], source_fingerprint=manifest["source_fingerprint"],
        selection_policy=manifest["selection_policy"], host=manifest["selection"]["host"],
        source_ip=raw["source_ip"], target_arn=target_arn, time_shift_seconds=int(shift),
        window_start=start + delta, window_end=end + delta,
        record_count=summary["record_count"], failed_attempt_count=summary["failed"],
        accepted_count=summary["accepted"], auxiliary_count=summary["auxiliary"], records=shifted,
    )
    return prepare_observation(inbox, parse_observation(raw), log_evidence=logs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "check", "prepare"))
    parser.add_argument("--output", type=Path, default=CORPUS, help="build/check artifact root")
    parser.add_argument("--case", choices=CASE_IDS[:7])
    parser.add_argument("--inbox", type=Path, help="prepare only: application mock inbox")
    parser.add_argument("--target-arn", help="prepare only: explicit mock target")
    parser.add_argument("--occurred-at", help="prepare only: replay window end (whole-second shift)")
    args = parser.parse_args()
    try:
        if args.command == "build":
            print(json.dumps({"status": "BUILT", "files": build(CORPUS, args.output)}))
        elif args.command == "check":
            print(json.dumps({"status": "PASS", "cases": check(args.output)}, indent=2))
        else:
            if not args.case or args.inbox is None or not args.target_arn:
                parser.error("prepare requires --case, --inbox and --target-arn")
            path = prepare_case(args.case, args.inbox, args.target_arn, args.occurred_at)
            print(json.dumps({"status": "PREPARED", "path": str(path.resolve())}))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"corpus check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
