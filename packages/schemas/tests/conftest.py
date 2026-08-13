"""저장소 루트를 import 경로에 추가한다 (packages.schemas.* 네임스페이스 로드용).

pytest는 CWD를 sys.path에 넣지 않으므로 apps/core-api/services/tests와
같은 방식으로 경로를 삽입한다.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
