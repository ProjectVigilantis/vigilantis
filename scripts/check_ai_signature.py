"""커밋 메시지·신원·PR 본문의 AI 서명을 검사한다 — CLAUDE.md §커밋 메시지 규칙(AI 서명 금지).

    python scripts/check_ai_signature.py --commits <base>..<head>
    python scripts/check_ai_signature.py --text-env PR_BODY

찾으면 해당 줄을 출력하고 exit 1. 서명 형태의 줄만 잡는다 — 규칙을 설명하려고
`Co-Authored-By`를 인용한 문장까지 막으면 이 규칙 자체를 문서로 남길 수 없다.

`--commits`는 메시지 본문과 **커밋 신원**(author·committer의 이름·메일)을 함께 본다.
신원은 메시지와 별개 경로다 — Claude Code를 클라우드 세션에서 돌리면 git identity가
`Claude <noreply@anthropic.com>`으로 기본 설정돼, 메시지가 깨끗해도 GitHub Contributors
목록에 AI 계정이 올라온다(2026-09-21 `9149b00`·`9e42db6`, PR #392·#391).
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

# 신원(author·committer)용 — 메시지와 달리 "인용" 여지가 없으므로 이름·메일을 직접 본다.
# 이름은 단어 경계로 끊는다: 사람 이름에 AI 이름이 통째로 들어갈 일은 없고, 부분 일치로
# 넓히면 엉뚱한 이름(예: `Geminiani`)까지 막힌다.
IDENTITY_NAME = re.compile(rf"(^|[\W_]){AI_NAMES}([\W_]|$)", re.I)
IDENTITY_EMAIL = re.compile(r"@(anthropic|openai)\.com$", re.I)


def find_signatures(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if any(p.search(ln) for p in PATTERNS)]


def find_identity(name: str, email: str) -> str | None:
    """AI 신원이면 사유를, 아니면 None을 돌려준다."""
    if IDENTITY_EMAIL.search(email.strip()):
        return "AI 제공사 메일 도메인"
    if IDENTITY_NAME.search(name.strip()):
        return "AI 이름"
    return None


def _is_commit(rev: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            capture_output=True,
        ).returncode
        == 0
    )


FORMAT = "%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e"


def commit_records(rev_range: str) -> list[tuple[str, str, str, str, str, str]]:
    """(sha, author 이름, author 메일, committer 이름, committer 메일, 메시지 본문)"""
    base, sep, head = rev_range.partition("..")
    if sep and not _is_commit(base):
        # 첫 푸시(before=000…)나 강제 푸시로 이전 머리가 사라졌으면 새 머리 커밋만 본다
        args = ["-1", head or "HEAD"]
    else:
        args = [rev_range]
    out = subprocess.run(
        ["git", "log", f"--format={FORMAT}", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    records = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if rec:
            sha, an, ae, cn, ce, body = rec.split("\x00", 5)
            records.append((sha, an, ae, cn, ce, body))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--commits", metavar="BASE..HEAD", help="검사할 커밋 범위")
    target.add_argument("--text-env", metavar="VAR", help="본문이 담긴 환경변수 이름")
    args = parser.parse_args(argv)

    identity_hit = False
    if args.commits:
        hits = []
        for sha, an, ae, cn, ce, body in commit_records(args.commits):
            hits += [f"{sha[:7]}: {line}" for line in find_signatures(body)]
            for role, name, email in (("author", an, ae), ("committer", cn, ce)):
                reason = find_identity(name, email)
                if reason:
                    identity_hit = True
                    hits.append(f"{sha[:7]}: {role} {name} <{email}> — {reason}")
    else:
        hits = [f"{args.text_env}: {line}" for line in find_signatures(os.environ.get(args.text_env, ""))]

    if hits:
        print("AI 서명 발견 — CLAUDE.md §커밋 메시지 규칙(AI 서명 금지). 아래를 고치고 다시 올린다:")
        for hit in hits:
            print(f"  {hit}")
        if identity_hit:
            print(
                "\n신원(author·committer)은 메시지를 지워도 남는다 — GitHub Contributors 목록에 올라간다.\n"
                "  1) git config user.name '<본인 이름>' && git config user.email '<본인 메일>'\n"
                "  2) 이미 만든 커밋: git rebase <base> --exec 'git commit --amend --no-edit --reset-author'"
            )
        return 1
    print("AI 서명 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
