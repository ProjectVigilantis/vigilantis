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
# ── 가드 셋이 서로 다른 것을 잠근다 ───────────────────────────────────────
# 1) 바이트 등식 — 모델에서 파생되는 내용이 저장본과 일치하는지 확인한다.
# 2) 머리 메타데이터 — 3키의 존재와 x-source-model·$schema 의 값을 확인한다.
#    `render()` 가 머리 값을 저장본에서 복사하므로 1)만으로는 값을 검증하지 못한다.
#    x-note 는 사람이 쓴 설명을 보존하며 특정 문구로 고정하지 않는다.
# 3) 파일 등록 — 실제 스키마 파일과 TARGETS 를 대조해 검사 대상 누락을 막는다.
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
    SCHEMA_DIR,
    TARGETS,
    render,
)

_IDS = [t.filename for t in TARGETS]


@pytest.mark.parametrize("target", TARGETS, ids=_IDS)
def test_golden_schema_is_a_current_extraction_of_the_contract(target):
    """저장본 == 현행 모델에서 다시 뽑은 것. 어긋나면 재추출 명령을 알려 준다.

    머리 3키가 없으면 여기서 `render` 가 ValueError 로 멈춘다 — 그 메시지는
    없어진 키 이름과 할 일을 둘 다 말해 준다. 머리 값은 저장본에서 복사하므로
    x-source-model·$schema 의 값 검사는 아래 테스트가 맡는다.
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
    """머리 3키의 존재와 원천 모델·스키마 draft 의 값을 확인한다.

    위 테스트는 이 값들을 못 잡는다 — `render()` 가 저장본에서 복사하므로
    무엇으로 바꿔도 재생성 결과가 같아진다(2026-09-11 PM 실측: x-source-model 을
    바꿔도 위 테스트는 PASS 했다). **값을 잠그는 것은 이 테스트 하나뿐이다.**

    x-source-model 은 이 JSON 이 어느 모델의 추출본인지 말하는 유일한 자리다.
    저장본의 $schema 는 Pydantic 생성기가 선언한 dialect 와 대조한다. 값이
    바뀌면 편집기 검증이 다른 규칙으로 돈다. x-note 는 존재만 확인하고 보존한다.
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
        f"$schema draft 가 Pydantic 생성기의 dialect 와 다르다 — {target.rel}\n"
        f"  저장본: {stored['$schema']}\n"
        f"  실제  : {JSON_SCHEMA_DRAFT}"
    )


def test_every_schema_file_is_registered():
    """실제 파일과 TARGETS 가 일치해야 모든 스키마 사본이 검사 대상이 된다."""
    on_disk = {path.name for path in SCHEMA_DIR.glob("*.schema.json") if path.is_file()}
    registered = {target.filename for target in TARGETS}

    assert on_disk == registered, (
        "골든 입력 스키마 파일과 TARGETS 등록이 다르다.\n"
        f"  미등록 파일: {sorted(on_disk - registered)}\n"
        "    scripts/extract_golden_schema.py 의 TARGETS 에 추가할 것.\n"
        f"  등록됐지만 없는 파일: {sorted(registered - on_disk)}\n"
        "    datasets/golden/schema/ 의 파일을 복원하거나 불필요한 TARGETS 항목을 정리할 것."
    )
