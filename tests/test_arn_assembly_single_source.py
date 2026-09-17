# ==============================================================================
# [파일 설명]  담당: 김승철 (Data & Rule Engine)
# ARN 조립의 단일 원천 가드. (Issue #342)
#
# 자산을 잇는 키는 전부 ARN 문자열이고 가드레일 ③ 이 그것을 완전일치로 대조한다. 그
# 문자열을 만드는 자리가 여럿이면 한 곳만 옛 방식으로 되돌아가도 조용히 어긋난다 —
# 수집기가 만든 자산 ARN 과 AI 평가·관계가 만든 target_arn 이 한 글자라도 다르면 조치
# 대상이 ARN_TARGET_NOT_MANAGED 로 거절되거나 AI 선택지에서 빠진다.
#
# 그래서 프로덕션 코드(apps·packages, tests·scripts 제외)에서 f-string 으로 arn:aws: 를
# 조립하는 자리는 schemas.arns.build_arn 하나뿐이어야 한다. 셋 중 하나를 옛 f-string 으로
# 되돌리면 이 테스트가 먼저 깨진다(#342 완료 기준). 정규식 리터럴(r"arn:aws:...")과
# docstring 의 예시 문자열은 조립이 아니라 잡지 않는다.
# ==============================================================================

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_ARN_FILE = ROOT / "packages" / "schemas" / "arns.py"

# f"arn:aws: 또는 f'arn:aws: — f-string 조립만 잡는다. r"arn:aws:(정규식)·평문 docstring 은
# 조립이 아니라 제외된다.
_ASSEMBLY = re.compile(r"""f["']arn:aws:""")


def _production_py_files():
    for base in ("apps", "packages"):
        for path in (ROOT / base).rglob("*.py"):
            parts = set(path.parts)
            if "tests" in parts or "scripts" in parts or path.name.startswith("test_"):
                continue
            yield path


def test_build_arn_source_exists():
    """단일 원천 파일이 실제로 조립을 담고 있어야 한다 — 가드가 헛돌지 않게."""
    assert _ASSEMBLY.search(BUILD_ARN_FILE.read_text(encoding="utf-8")), (
        f"{BUILD_ARN_FILE.relative_to(ROOT)} 에 build_arn 의 조립이 없다 — 원천이 옮겨졌나"
    )


def test_arn_assembly_has_single_source():
    """build_arn(schemas/arns.py) 밖에서 f-string 으로 ARN 을 조립하면 실패한다."""
    offenders = []
    for path in _production_py_files():
        if path == BUILD_ARN_FILE:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ASSEMBLY.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "ARN 을 f-string 으로 직접 조립한 자리 — schemas.arns.build_arn 을 쓸 것:\n"
        + "\n".join(offenders)
    )
