"""커밋 메시지·PR 본문의 AI 서명을 검사한다 — CLAUDE.md §커밋 메시지 규칙(AI 서명 금지).

    python scripts/check_ai_signature.py --commits <base>..<head>
    python scripts/check_ai_signature.py --text-env PR_BODY

찾으면 해당 줄을 출력하고 exit 1. 서명 형태의 줄만 잡는다 — 규칙을 설명하려고
`Co-Authored-By`를 인용한 문장까지 막으면 이 규칙 자체를 문서로 남길 수 없다.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

AI_NAMES = r"(Claude|Anthropic|Codex|Copilot|ChatGPT|OpenAI|Gemini|Cursor)"

PATTERNS = [
    # 트레일러 — 사람 공동 작성자(`Co-Authored-By: 박지현 <...>`)는 통과시킨다
    re.compile(rf"^\s*Co-Authored-By:.*\b{AI_NAMES}\b", re.I),
    re.compile(r"^\s*Claude-Session:", re.I),
    # 세션·작업 링크 — 줄 어디에 있어도 서명이다
    re.compile(r"https?://claude\.ai/code/session_\w+", re.I),
    re.compile(r"https?://chatgpt\.com/codex/tasks/\w+", re.I),
    # 생성 문구 — 이모지 접두(`🤖 Generated with [Claude Code](...)`)까지
    re.compile(rf"^\W*Generated (with|by) \[?{AI_NAMES}", re.I),
]


def find_signatures(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if any(p.search(ln) for p in PATTERNS)]


def _is_commit(rev: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            capture_output=True,
        ).returncode
        == 0
    )


def commit_messages(rev_range: str) -> list[tuple[str, str]]:
    base, sep, head = rev_range.partition("..")
    if sep and not _is_commit(base):
        # 첫 푸시(before=000…)나 강제 푸시로 이전 머리가 사라졌으면 새 머리 커밋만 본다
        args = ["-1", head or "HEAD"]
    else:
        args = [rev_range]
    out = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x1e", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    records = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if rec:
            sha, _, body = rec.partition("\x00")
            records.append((sha, body))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--commits", metavar="BASE..HEAD", help="검사할 커밋 범위")
    target.add_argument("--text-env", metavar="VAR", help="본문이 담긴 환경변수 이름")
    args = parser.parse_args(argv)

    if args.commits:
        hits = [
            f"{sha[:7]}: {line}"
            for sha, body in commit_messages(args.commits)
            for line in find_signatures(body)
        ]
    else:
        hits = [f"{args.text_env}: {line}" for line in find_signatures(os.environ.get(args.text_env, ""))]

    if hits:
        print("AI 서명 발견 — CLAUDE.md §커밋 메시지 규칙(AI 서명 금지). 아래 줄을 지우고 다시 올린다:")
        for hit in hits:
            print(f"  {hit}")
        return 1
    print("AI 서명 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
