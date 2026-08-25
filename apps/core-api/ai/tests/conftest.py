"""apps/core-api·packages를 import 경로에 추가한다 (ai.* · schemas.* 로드용).

없으면 이 디렉터리를 단독 실행할 때 schemas를 찾지 못해 수집이 깨지고, 다른
디렉터리의 conftest가 먼저 로드되는 CI 전체 인자에서만 통과한다.
(저장소 루트 삽입 금지 — `packages.schemas.*`로 로드하면 설치본과 모듈 이름이
갈라져 Pydantic 클래스 identity가 어긋난다.)
"""

import sys
from pathlib import Path

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)
