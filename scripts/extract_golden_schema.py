# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 골든 입력 스키마 사본을 packages/schemas 에서 다시 뽑는다. (Issue #309)
#
# 실행 (repo 루트):
#   재추출: uv run python scripts/extract_golden_schema.py
#   대조만: uv run python scripts/extract_golden_schema.py --check
#
# ── 왜 필요한가 ───────────────────────────────────────────────────────────
# datasets/golden/schema/asset_inventory.schema.json 은 AssetInventory 의 추출본
# 이고 **사람이 읽는 계약 사본**이다 — 골든을 손으로 만들 때 보는 문서다.
# packages/schemas 가 바뀌어도 이 JSON 은 아무 신호를 내지 않아서 조용히 낡는다.
# 실제로 #255 가 더한 metrics_window_end 가 빠진 채 남아 있었고, #276 작업 중
# **우연히** 발견돼 PR #300 에서 고쳐졌다. 우연이 없었으면 그대로 남았다.
#
# ── 손수 쓴 머리 3키는 보존한다 ───────────────────────────────────────────
# $schema · x-source-model · x-note 는 모델에서 나오지 않는 사람의 글이다.
# 지우고 새로 쓰면 그 글이 사라지므로 **저장본에서 읽어 그대로 얹는다.**
# 하나라도 없으면 조용히 넘기지 않고 멈춘다 — 없어진 것을 모른 채 재추출하면
# 다음 재추출부터는 그게 정상으로 굳는다.
#
# 이 스크립트가 저장본을 **바이트 단위로** 재생성한다. 그래서 가드
# (tests/test_golden_schema_drift.py)가 등식으로 잠글 수 있고, 깨졌을 때
# 처방이 "고쳐라"가 아니라 "이 명령을 돌려라"가 된다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 콘솔(cp949)은 em dash 등 출력 시 UnicodeEncodeError로 죽는다 — UTF-8로 강제
# (scripts/seed_localstack.py:41 과 같은 이유). 파일은 이미 쓰인 뒤 print 에서만
# 죽으면 "재추출이 실패했다"로 읽혀 다음 사람이 멈춘다.
# stderr 도 함께 — 중단 메시지(SystemExit)가 그리로 나간다. stdout 만 고치면
# 정상 출력은 읽히는데 **정작 멈춘 이유가 깨져서** 나온다.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "packages",):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from schemas.assets import AssetInventory  # noqa: E402

SCHEMA_PATH = ROOT / "datasets" / "golden" / "schema" / "asset_inventory.schema.json"

# 모델에서 파생되지 않는 키 — 순서도 저장본 그대로 유지한다(diff 를 순수 추가로 둔다)
HAND_WRITTEN_KEYS = ("$schema", "x-source-model", "x-note")

REEXTRACT_COMMAND = "uv run python scripts/extract_golden_schema.py"


def render(stored: dict) -> str:
    """저장본의 머리 3키 + 현행 모델의 스키마 → 파일 문자열."""
    missing = [k for k in HAND_WRITTEN_KEYS if k not in stored]
    if missing:
        # SystemExit 이 아니라 ValueError 다 — 이 함수는 가드 테스트도 부른다.
        # 테스트에서 SystemExit 이 나면 pytest 가 원인을 못 말해 준다.
        raise ValueError(
            f"손수 쓴 머리 키가 없다({', '.join(missing)}) — {SCHEMA_PATH}\n"
            "  이 키들은 모델에서 파생되지 않는다. 되살린 뒤 다시 돌릴 것."
        )
    head = {k: stored[k] for k in HAND_WRITTEN_KEYS}
    return json.dumps(
        {**head, **AssetInventory.model_json_schema()}, ensure_ascii=False, indent=2
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="골든 입력 스키마 사본을 다시 뽑는다")
    parser.add_argument(
        "--check", action="store_true", help="쓰지 않고 대조만 한다(어긋나면 rc=1)"
    )
    args = parser.parse_args()

    current = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        expected = render(json.loads(current))
    except ValueError as exc:
        raise SystemExit(f"중단: {exc}") from exc

    if current == expected:
        print(f"일치: {SCHEMA_PATH.relative_to(ROOT).as_posix()}")
        return 0

    if args.check:
        print(f"어긋남: {SCHEMA_PATH.relative_to(ROOT).as_posix()}")
        print(f"  재추출 —  {REEXTRACT_COMMAND}")
        return 1

    SCHEMA_PATH.write_text(expected, encoding="utf-8", newline="\n")
    print(f"재추출 완료: {SCHEMA_PATH.relative_to(ROOT).as_posix()}")
    print("  diff 를 확인하고 함께 커밋할 것 — 계약이 바뀌었다는 뜻이다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
