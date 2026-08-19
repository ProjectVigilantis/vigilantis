"""packages/ 디렉터리를 import 경로에 추가한다 (schemas.* 로드용).

uv sync 된 환경에서는 workspace 설치본(schemas)이 이미 잡히지만,
pytest 단독 실행 시에도 동일한 최상위 이름 `schemas` 로 로드되도록
apps/core-api/services/tests 와 같은 방식으로 경로를 삽입한다.
(저장소 루트 삽입 → `packages.schemas.*` 로 로드하면 설치본과 모듈
이름이 갈라져 Pydantic 클래스 identity 가 어긋나므로 금지.)
"""

import sys
from pathlib import Path

PACKAGES_DIR = Path(__file__).resolve().parents[2]
if str(PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGES_DIR))
