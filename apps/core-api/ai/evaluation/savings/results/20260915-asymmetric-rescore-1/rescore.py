"""사용자가 승인한 비대칭 절감액 기준으로 저장된 결과만 다시 채점한다. 모델 호출은 없다."""

import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROUND = Path(__file__).resolve().parent
BASE = ROUND.parent.parent
ROOT = ROUND.parents[6]
sys.path[:0] = [str(ROOT / path) for path in ("apps/core-api", "packages")]

from ai.evaluation.savings.isolation import score_price_only_report
from ai.evaluation.savings.scoring import score_report


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def relative(path, directory):
    return Path(os.path.relpath(path, directory)).as_posix()


def main():
    before = json.loads((ROUND / "before.json").read_text("utf-8"))
    historical = json.loads((BASE / "spec-v1.json").read_text("utf-8"))
    current = json.loads((BASE / "spec.json").read_text("utf-8"))
    assert sha(BASE / "spec-v1.json") == before["spec_v1_sha256"]
    assert current["fixed_set"] == historical["fixed_set"]
    assert current["price_reference"] == historical["price_reference"]
    for name, expected in before["historical_artifact_sha256"].items():
        assert sha(BASE / name) == expected, name
    reports = []
    for name in sorted(before["historical_artifact_sha256"]):
        if not name.endswith("scored.json"):
            continue
        path = BASE / name
        previous = json.loads(path.read_text("utf-8"))
        source = path.parent / previous.get("source_file", path.name.replace("scored.json", "raw.json"))
        raw = json.loads(source.read_text("utf-8"))
        scorer = (score_price_only_report if previous.get("execution_mode") == "price_only_diagnostic"
                  else score_report)
        reproduced = scorer(raw, historical)
        assert all(previous[key] == value for key, value in reproduced.items()), name
        assert previous["source_sha256"] == sha(source)
        assert previous["spec_sha256"] == before["spec_v1_sha256"]
        updated = scorer(raw, current)
        output = ROUND / "scored" / path.parent.name / path.name
        updated.update(
            source_file=relative(source, output.parent), source_sha256=sha(source),
            spec_file=relative(BASE / "spec.json", output.parent), spec_sha256=sha(BASE / "spec.json"),
            previous_scored_file=relative(path, output.parent), previous_scored_sha256=sha(path),
            rescoring_only=True, new_model_calls=0,
        )
        if "condition" in previous:
            updated["condition"] = previous["condition"]
        write_json(output, updated)
        rates_outside = sum(result.get("rate_accuracy", {}).get("within_symmetric_tolerance") is False
                            for result in updated["results"])
        reports.append({
            "source": name, "rescored_file": relative(output, ROUND), "source_sha256": sha(source),
            "runs": updated["runs"], "previous_counts": previous["status_counts"],
            "new_counts": updated["status_counts"], "rate_diagnostic_outside_tolerance": rates_outside,
            "savings_direction_counts": dict(Counter(r["savings_direction"] for r in updated["results"]
                                                       if "savings_direction" in r)),
            "probe_complete": updated.get("probe_complete"), "probe_passed": updated.get("probe_passed"),
            "service_baseline_passed": updated.get("passed", False),
        })
    summary = {
        "reason": "User kept overestimated savings tolerance at 10% and relaxed underestimated savings to 20%; rate accuracy is recorded separately.",
        "spec_version": current["version"], "spec_sha256": sha(BASE / "spec.json"),
        "previous_spec_version": historical["version"], "previous_spec_sha256": before["spec_v1_sha256"],
        "criteria": current["criteria"], "legacy_reports_reproduced": len(reports),
        "historical_files_unchanged": len(before["historical_artifact_sha256"]),
        "new_model_calls": 0, "model_outputs_changed": False, "reports": reports,
    }
    write_json(ROUND / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
