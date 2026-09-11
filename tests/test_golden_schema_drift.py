# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 골든 입력 스키마 사본이 packages/schemas 와 갈리지 않는지 지킨다. (Issue #309)
#
# datasets/golden/schema/asset_inventory.schema.json 은 AssetInventory 의 추출본
# 이고 **사람이 읽는 계약 사본**이다 — 골든을 손으로 만들 때 보는 문서다.
# 그런데 재추출을 요구하는 것이 아무것도 없어서 조용히 낡는다: #255 가 더한
# metrics_window_end 가 빠진 채 남아 있었고 #276 작업 중 **우연히** 발견됐다.
#
# ── 왜 바이트 등식인가 ────────────────────────────────────────────────────
# 필드 집합만 세면 description·default·enum 값이 갈려도 통과한다. 이 파일의
# 쓸모가 "사람이 읽고 골든을 만든다" 라서 **설명문이 낡는 것도 드리프트**다.
# scripts/extract_golden_schema.py 가 저장본을 바이트 단위로 재생성하므로
# 등식으로 잠글 수 있고, 깨졌을 때 처방이 **"이 명령을 돌려라"** 하나로 끝난다.
#
# ── 양방향으로 깨진다 ─────────────────────────────────────────────────────
# 모델을 고쳐도(추출 결과가 달라진다) 저장본만 고쳐도(비교 대상이 달라진다)
# 깨진다. 한쪽만 잡는 가드는 반대쪽 드리프트를 통과시킨다.
# ==============================================================================

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "scripts", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from extract_golden_schema import (  # noqa: E402
    HAND_WRITTEN_KEYS,
    REEXTRACT_COMMAND,
    SCHEMA_PATH,
    render,
)


def test_golden_schema_is_a_current_extraction_of_the_contract():
    """저장본 == 현행 모델에서 다시 뽑은 것. 어긋나면 재추출 명령을 알려 준다.

    머리 3키가 없으면 여기서 `render` 가 ValueError 로 멈춘다 — 재추출 명령을
    못 알려 주는 실패다. **원인을 이름으로 말해 주는 것은 아래 테스트**이고,
    그래서 둘을 갈라 뒀다.
    """
    current = SCHEMA_PATH.read_text(encoding="utf-8")
    expected = render(json.loads(current))

    assert current == expected, (
        "골든 입력 스키마 사본이 packages/schemas 와 갈렸다.\n"
        f"  재추출 —  {REEXTRACT_COMMAND}\n"
        "  재추출 결과 diff 를 함께 커밋할 것 — 계약이 바뀌었다는 뜻이다."
    )


def test_hand_written_header_survives_reextraction():
    """모델에서 안 나오는 머리 3키가 저장본에 남아 있는가.

    위 테스트도 함께 깨지지만 **`ValueError` 로 멈출 뿐 무엇을 해야 하는지는
    말하지 않는다**(실측 확인 — `x-note` 를 지우고 돌려 봤다). 이 테스트가
    없어진 키를 **이름으로** 짚는다. x-source-model 은 어느 모델의 추출본인지
    말하는 유일한 자리라 값까지 본다.
    """
    stored = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert [k for k in HAND_WRITTEN_KEYS if k not in stored] == []
    assert stored["x-source-model"] == "packages/schemas/assets.py :: AssetInventory"
