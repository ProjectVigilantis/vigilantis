"""저장소 루트의 평가 명령 진입점. 구현은 ai/evaluation/summary에 둔다."""

# 저장소 경로를 등록한 뒤 실제 CLI와 과거 실험용 이름을 가져온다.
# ruff: noqa: E402

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "apps/core-api", ROOT / "packages"):
    sys.path.insert(0, str(path))

from ai.evaluation.summary.cli import (
    _fixed_set,  # noqa: F401
    _label,  # noqa: F401
    _UsageRecordingClient,  # noqa: F401
    load_cases,  # noqa: F401
    main,
)

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
