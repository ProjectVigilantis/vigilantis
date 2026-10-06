# scripts/qa_scan_compare.py 검증 — 스모크 수집·판정 대조가 코드로 지키는 약속.
#
#   ① `error_summary` 에서 **라벨별 사유 코드**를 잃지 않는다. 그 코드가 LocalStack 의
#      라이선스 실패(`InternalFailure`)와 실 AWS 의 권한 누락(`AccessDenied`)을 가르는
#      유일한 값이다(ADR-0009 §6-1).
#   ② 읽을 수 없는 `error_summary` 를 **빈 값으로 삼키지 않는다.** 삼키면 실패가 없었던
#      것처럼 기록되고, 그 기록은 이월 4행의 처분 근거로 쓰인다.
#   ③ §2-2 표가 **자산 7유형을 모두** 들고 있다. 수집기가 못 본 유형은
#      `asset_inventory_counts` 에 행을 남기지 않으므로(models.AssetInventoryCount),
#      관측 결과에서 유형을 끌어오면 그 유형이 표에서 사라진다 — "0건" 과 "모름" 이
#      같은 칸이 되는 순간 대조가 무의미해진다.
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다. DB·AWS를 부르지 않는다.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "apps" / "core-api", REPO_ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from schemas.api.assets import AssetType  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


qa = _load("qa_scan_compare")


# ① 라벨별 사유 코드를 잃지 않는다
def test_failures_keeps_label_and_reason_code():
    failures, how = qa._failures(
        '{"alb_target_groups":"InternalFailure","auto_scaling_groups":"AccessDenied"}'
    )
    assert failures == {
        "alb_target_groups": "InternalFailure",
        "auto_scaling_groups": "AccessDenied",
    }
    assert how == "JSON"


def test_failures_empty_summary_is_not_a_failure():
    assert qa._failures(None) == ({}, "빈 값")
    assert qa._failures("") == ({}, "빈 값")


# ② 읽을 수 없는 값을 빈 값으로 삼키지 않는다 — 원문을 들고 돌아온다
@pytest.mark.parametrize(
    "raw",
    [
        "AccessDenied: autoscaling",          # JSON 이 아닌 과거/수기 값
        '"alb_target_groups"',                # JSON 이지만 dict 가 아니다
        '{"_truncated":"5"}',                 # 상한 초과로 항목이 버려진 표식
    ],
)
def test_failures_unreadable_summary_reports_raw_text(raw):
    failures, how = qa._failures(raw)
    assert failures == {}
    assert how != "빈 값"
    # 사람이 원문을 보고 판단할 수 있어야 한다 — 설명에 원문이 남는다.
    assert raw in how or "상한 초과" in how


# ③ §2-2 표가 7유형을 모두 들고 있다 — 유형이 늘면 이 테스트가 먼저 깨진다
def test_inventory_table_covers_every_asset_type():
    assert set(qa._ALL_TYPES) == set(AssetType)
    assert len(qa._ALL_TYPES) == 7
