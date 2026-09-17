"""Repository entry point; implementation lives in ai/evaluation."""

# 저장소 경로를 등록한 뒤 실제 CLI를 가져온다.
# ruff: noqa: E402

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "apps/core-api", ROOT / "packages"):
    sys.path.insert(0, str(path))

from ai.evaluation.summary.judge_cli import main

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
