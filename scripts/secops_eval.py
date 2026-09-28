"""저장소 루트에서 실행하는 SecOps 오프라인 평가 진입점."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "apps/core-api", ROOT / "packages"):
    sys.path.insert(0, str(path))

from ai.evaluation.secops.cli import main

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
