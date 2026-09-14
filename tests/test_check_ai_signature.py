# scripts/check_ai_signature.py 검증 — CLAUDE.md §커밋 메시지 규칙(AI 서명 금지)의 CI 가드.
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다 — 실제 CLI 를 subprocess 로
# 불러 CI 워크플로(.github/workflows/ai-signature.yml)가 보는 것과 같은 종료 코드를 잰다.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_ai_signature.py"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _run(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=env or ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _check_text(text: str) -> subprocess.CompletedProcess:
    return _run("--text-env", "BODY", env={**ENV, "BODY": text})


@pytest.mark.parametrize(
    "line",
    [
        "Co-Authored-By: Claude <noreply@anthropic.com>",
        "Co-authored-by: Copilot <copilot@github.com>",
        "Claude-Session: https://claude.ai/code/session_01ABCdef",
        "https://claude.ai/code/session_01ABCdef",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
        "https://chatgpt.com/codex/tasks/task_e_abc123",
    ],
)
def test_signature_lines_fail(line):
    result = _check_text(f"## 관련 이슈\n\nRefs #1\n\n{line}\n")
    assert result.returncode == 1, result.stdout
    assert line.strip() in result.stdout


@pytest.mark.parametrize(
    "line",
    [
        # 규칙을 설명하는 문장 — 이것까지 막으면 규칙을 문서로 남길 수 없다
        "- Claude.md: AI 작성 커밋에 Co-Authored-By 트레일러를 붙이지 않도록 규칙 변경",
        "- `Claude-Session:` 트레일러와 세션 링크를 금지한다",
        # 사람 공동 작성자는 서명이 아니다
        "Co-Authored-By: Park Ji Hyeon <jihyeon@example.com>",
    ],
)
def test_rule_mentions_and_human_coauthors_pass(line):
    result = _check_text(f"{line}\n\nRefs #1\n")
    assert result.returncode == 0, result.stdout


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    return tmp_path


def test_commit_range_catches_trailer(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "feat\n\nRefs #1\n\nClaude-Session: https://claude.ai/code/session_01X")
    head = _git(repo, "rev-parse", "HEAD")

    result = _run("--commits", f"{base}..{head}", cwd=repo)

    assert result.returncode == 1, result.stdout
    assert head[:7] in result.stdout


def test_commit_range_outside_is_not_checked(repo):
    # 범위 밖(이미 dev에 있는) 커밋은 이 PR의 책임이 아니다
    _git(repo, "commit", "-q", "--allow-empty", "-m", "old\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "clean\n\nRefs #2")
    head = _git(repo, "rev-parse", "HEAD")

    assert _run("--commits", f"{base}..{head}", cwd=repo).returncode == 0


def test_missing_base_falls_back_to_head_commit(repo):
    # 강제 푸시·첫 푸시로 before가 없는 push 이벤트 — 머리 커밋만 본다
    _git(repo, "commit", "-q", "--allow-empty", "-m", "feat\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    head = _git(repo, "rev-parse", "HEAD")

    result = _run("--commits", f"{'0' * 40}..{head}", cwd=repo)

    assert result.returncode == 1, result.stdout
