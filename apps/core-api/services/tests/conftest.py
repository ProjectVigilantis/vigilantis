# ==============================================================================
# [파일 설명]
# services/tests 공용 픽스처 등록.
#
# PostgreSQL 픽스처(db·pg_engine)는 db/tests/conftest.py 가 원천이고, 여기서
# **디렉터리 단위로 한 번만** 들여온다. 테스트 모듈마다 각자 import 하면 같은 픽스처가
# 모듈 수만큼 따로 등록되고, session 스코프인 pg_engine 이 그만큼 실행돼
# `CREATE DATABASE "vigilantis_test_xxxx" already exists` 로 두 번째부터 전부 ERROR 가
# 난다(TEST_DB_NAME 은 프로세스당 하나다). 파일 하나만 돌리면 재현되지 않아 전체 실행에서
# 처음 드러난다. (Issue #332)
# ==============================================================================

from __future__ import annotations

import sys
from pathlib import Path

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db.tests.conftest import db, pg_engine  # noqa: F401, E402
