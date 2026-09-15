# ==============================================================================
# [파일 설명]  담당: 박지현 (QA & Scenario)
# 골든 입력 스키마 사본 2개를 packages/schemas 에서 다시 뽑는다. (Issue #309)
#
# 실행 (repo 루트):
#   재추출: uv run python scripts/extract_golden_schema.py
#   대조만: uv run python scripts/extract_golden_schema.py --check
#
# ── 왜 필요한가 ───────────────────────────────────────────────────────────
# datasets/golden/schema/ 의 JSON 둘은 Pydantic 모델의 추출본이고 **사람이 읽는
# 계약 사본**이다 — 골든을 손으로 만들 때 보는 문서다(datasets/golden/README.md
# §양식). packages/schemas 가 바뀌어도 이 JSON 은 아무 신호를 내지 않아서 조용히
# 낡는다. 실제로 #255 가 더한 metrics_window_end 가 빠진 채 남아 있었고, #276
# 작업 중 **우연히** 발견돼 PR #300 에서 고쳐졌다. 우연이 없었으면 그대로 남았다.
#
# **두 파일을 함께 지킨다.** 한쪽만 지키면 이름이 약속하는 것과 실제가 갈리고,
# 다음 사람은 둘 다 지켜진다고 믿는다 — #309 가 막으려던 상황이 그대로 남는다.
#
# ── 손수 쓴 머리 3키는 보존한다 ───────────────────────────────────────────
# $schema · x-source-model · x-note 는 모델에서 나오지 않는 사람의 글이다.
# 지우고 새로 쓰면 그 글이 사라지므로 **저장본에서 읽어 그대로 얹는다.**
# 하나라도 없으면 조용히 넘기지 않고 멈춘다 — 없어진 것을 모른 채 재추출하면
# 다음 재추출부터는 그게 정상으로 굳는다.
#
# ⚠️ 다만 **머리 키의 「값」은 이 파일이 지키지 못한다** — 저장본에서 그대로
# 복사하므로 값이 무엇이든 재생성 결과가 같아진다. 그 값을 잠그는 것은
# tests/test_golden_schema_drift.py 의 두 번째 가드다.
#
# 이 스크립트가 저장본을 **바이트 단위로** 재생성한다. 그래서 가드가 등식으로
# 잠글 수 있고, 깨졌을 때 처방이 "고쳐라"가 아니라 "이 명령을 돌려라"가 된다.
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "packages",):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pydantic import TypeAdapter  # noqa: E402

from schemas.assets import AssetInventory  # noqa: E402
from schemas.events import MockThreatEventInput  # noqa: E402

SCHEMA_DIR = ROOT / "datasets" / "golden" / "schema"

# 모델에서 파생되지 않는 키 — 순서도 저장본 그대로 유지한다(diff 를 순수 추가로 둔다)
HAND_WRITTEN_KEYS = ("$schema", "x-source-model", "x-note")

# pydantic v2 가 뽑는 스키마의 draft. 사람의 의견이 아니라 추출기가 정하는 사실이다
JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"

REEXTRACT_COMMAND = "uv run python scripts/extract_golden_schema.py"


@dataclass(frozen=True)
class Target:
    """추출본 한 개 — 파일, 뽑는 법, 그 파일이 주장하는 원천 모델."""

    filename: str
    produce: Callable[[], dict]
    source_model: str

    @property
    def path(self) -> Path:
        return SCHEMA_DIR / self.filename

    @property
    def rel(self) -> str:
        return self.path.relative_to(ROOT).as_posix()


TARGETS: tuple[Target, ...] = (
    Target(
        "asset_inventory.schema.json",
        lambda: AssetInventory.model_json_schema(),
        "packages/schemas/assets.py :: AssetInventory",
    ),
    # 판별 유니온이라 클래스가 아니다 — TypeAdapter 로 뽑는다
    Target(
        "mock_threat_event_input.schema.json",
        lambda: TypeAdapter(MockThreatEventInput).json_schema(),
        "packages/schemas/events.py :: MockThreatEventInput "
        "(OpenIpThreatInput | SshBruteForceThreatInput)",
    ),
)


def render(stored: dict, target: Target) -> str:
    """저장본의 머리 3키 + 현행 모델의 스키마 → 파일 문자열."""
    missing = [k for k in HAND_WRITTEN_KEYS if k not in stored]
    if missing:
        # SystemExit 이 아니라 ValueError 다 — 이 함수는 가드 테스트도 부른다.
        # 테스트에서 SystemExit 이 나면 pytest 가 원인을 못 말해 준다.
        raise ValueError(
            f"손수 쓴 머리 키가 없다({', '.join(missing)}) — {target.rel}\n"
            "  이 키들은 모델에서 파생되지 않는다. 되살린 뒤 다시 돌릴 것."
        )
    head = {k: stored[k] for k in HAND_WRITTEN_KEYS}
    return json.dumps({**head, **target.produce()}, ensure_ascii=False, indent=2) + "\n"


def _use_utf8_output() -> None:
    """Windows 기본 콘솔 코드페이지(cp949)는 이 파일의 한국어·em dash 를 못 낸다.
    팀 개발 환경이 Windows 라 출력 스트림을 UTF-8 로 고정한다.

    모듈 최상위가 아니라 여기서 부르는 이유는 **가드 테스트가 이 모듈을
    import 하기 때문**이다 — 최상위에 두면 pytest 프로세스 전체의 출력 스트림이
    수집 시점에 바뀐다. (scripts/inject_mock_threat.py 와 같은 관례)
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):  # 파이프·리다이렉트 등 재설정 불가
            pass


def main() -> int:
    _use_utf8_output()
    parser = argparse.ArgumentParser(description="골든 입력 스키마 사본을 다시 뽑는다")
    parser.add_argument(
        "--check", action="store_true", help="쓰지 않고 대조만 한다(어긋나면 rc=1)"
    )
    args = parser.parse_args()

    drifted = 0
    for target in TARGETS:
        current = target.path.read_text(encoding="utf-8")
        try:
            expected = render(json.loads(current), target)
        except ValueError as exc:
            raise SystemExit(f"중단: {exc}") from exc

        if current == expected:
            print(f"일치: {target.rel}")
            continue

        drifted += 1
        if args.check:
            print(f"어긋남: {target.rel}")
            continue
        target.path.write_text(expected, encoding="utf-8", newline="\n")
        print(f"재추출: {target.rel}")

    if drifted and args.check:
        print(f"  재추출 —  {REEXTRACT_COMMAND}")
        return 1
    if drifted:
        print("  diff 를 확인하고 함께 커밋할 것 — 계약이 바뀌었다는 뜻이다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
