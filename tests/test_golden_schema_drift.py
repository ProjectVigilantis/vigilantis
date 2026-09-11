# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 골든 입력 스키마 사본 2개가 packages/schemas 와 갈리지 않는지 지킨다. (Issue #309)
#
# datasets/golden/schema/ 의 JSON 둘은 Pydantic 모델의 추출본이고 **사람이 읽는
# 계약 사본**이다 — 골든을 손으로 만들 때 보는 문서다. 그런데 재추출을 요구하는
# 것이 아무것도 없어서 조용히 낡는다: #255 가 더한 metrics_window_end 가 빠진 채
# 남아 있었고 #276 작업 중 **우연히** 발견됐다.
#
# ── 왜 바이트 등식인가 ────────────────────────────────────────────────────
# 필드 집합만 세면 description·default·enum 값이 갈려도 통과한다. 이 파일의
# 쓸모가 "사람이 읽고 골든을 만든다" 라서 **설명문이 낡는 것도 드리프트**다.
# scripts/extract_golden_schema.py 가 저장본을 바이트 단위로 재생성하므로
# 등식으로 잠글 수 있고, 깨졌을 때 처방이 **"이 명령을 돌려라"** 하나로 끝난다.
#
# ── 가드 둘이 서로 다른 것을 잠근다 ───────────────────────────────────────
# 1) 바이트 등식 — 모델을 고쳐도(추출 결과가 달라진다) 저장본만 고쳐도 깨진다.
# 2) 머리 3키의 **값** — `render()` 가 머리 키를 저장본에서 **그대로 복사**하므로
#    1)은 머리 값에 대해 **항상 참**이다. 값을 무엇으로 바꿔도 통과한다.
#    그 값을 잠그는 것은 2) 하나뿐이다. (2026-09-11 PM 리뷰에서 확인된 사실)
# ==============================================================================

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "scripts", ROOT / "packages"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from extract_golden_schema import (  # noqa: E402
    HAND_WRITTEN_KEYS,
    JSON_SCHEMA_DRAFT,
    REEXTRACT_COMMAND,
    TARGETS,
    render,
)

_IDS = [t.filename for t in TARGETS]


@pytest.mark.parametrize("target", TARGETS, ids=_IDS)
def test_golden_schema_is_a_current_extraction_of_the_contract(target):
    """저장본 == 현행 모델에서 다시 뽑은 것. 어긋나면 재추출 명령을 알려 준다.

    머리 3키가 없으면 여기서 `render` 가 ValueError 로 멈춘다 — 그 메시지는
    없어진 키 이름과 할 일을 둘 다 말해 준다. 이 테스트가 **못 잡는 것**은
    머리 키의 *값* 이고, 그것은 아래 테스트가 맡는다.
    """
    current = target.path.read_text(encoding="utf-8")
    expected = render(json.loads(current), target)

    assert current == expected, (
        f"골든 입력 스키마 사본이 packages/schemas 와 갈렸다 — {target.rel}\n"
        f"  재추출 —  {REEXTRACT_COMMAND}\n"
        "  재추출 결과 diff 를 함께 커밋할 것 — 계약이 바뀌었다는 뜻이다."
    )


@pytest.mark.parametrize("target", TARGETS, ids=_IDS)
def test_hand_written_header_keeps_its_values(target):
    """머리 3키가 남아 있고 **값이 그대로인가.**

    위 테스트는 이 값들을 못 잡는다 — `render()` 가 저장본에서 복사하므로
    무엇으로 바꿔도 재생성 결과가 같아진다(2026-09-11 PM 실측: x-source-model 을
    바꿔도 위 테스트는 PASS 했다). **값을 잠그는 것은 이 테스트 하나뿐이다.**

    x-source-model 은 이 JSON 이 어느 모델의 추출본인지 말하는 유일한 자리다.
    $schema 는 pydantic v2 가 정하는 사실이고, 값이 바뀌면 편집기 검증이 다른
    규칙으로 돈다.
    """
    stored = json.loads(target.path.read_text(encoding="utf-8"))

    missing = [k for k in HAND_WRITTEN_KEYS if k not in stored]
    assert missing == [], (
        f"손수 쓴 머리 키가 사라졌다({', '.join(missing)}) — {target.rel}\n"
        "  이 키들은 모델에서 파생되지 않는다. 되살려 넣을 것."
    )
    assert stored["x-source-model"] == target.source_model, (
        f"x-source-model 이 추출 원천과 다르다 — {target.rel}\n"
        f"  저장본: {stored['x-source-model']}\n"
        f"  실제  : {target.source_model}"
    )
    assert stored["$schema"] == JSON_SCHEMA_DRAFT, (
        f"$schema draft 가 pydantic v2 출력과 다르다 — {target.rel}\n"
        f"  저장본: {stored['$schema']}\n"
        f"  실제  : {JSON_SCHEMA_DRAFT}"
    )
