# ==============================================================================
# [파일 설명]
# scripts/inject_mock_threat.py 의 --json 출력 계약 검증. (#268 / #269 리뷰: 안성일)
#
#   scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다 — 실제 CLI 를 subprocess 로
#   불러 안성일이 쓴 시나리오(`--json | json.load`)를 그대로 재현한다.
# ==============================================================================

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "inject_mock_threat.py"

_SUMMARY_MARK = "정답 대조 통과"


def _run_json() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_json_mode_stdout_is_a_single_json_document():
    """--json 의 stdout 전체가 하나의 JSON 배열로 파싱된다 — 요약 텍스트가 섞이면
    기계가 못 읽는다(안성일 #269: `--json | json.load` 가 요약 줄에 Extra data 로 깨졌다)."""
    proc = _run_json()
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)  # 추가 텍스트가 있으면 여기서 JSONDecodeError
    assert isinstance(parsed, list) and parsed, "결과가 비지 않은 JSON 배열이어야 한다"


def test_json_mode_summary_goes_to_stderr_not_stdout():
    """성공 요약은 stderr 로 분리돼 stdout(기계용 JSON)을 오염시키지 않는다."""
    proc = _run_json()
    assert proc.returncode == 0, proc.stderr
    assert _SUMMARY_MARK not in proc.stdout  # stdout 은 순수 JSON
    assert _SUMMARY_MARK in proc.stderr       # 사람이 보는 요약은 stderr
