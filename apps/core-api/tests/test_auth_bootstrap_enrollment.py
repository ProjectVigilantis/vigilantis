"""플랫폼 계정 등록 링크 CLI (#492) — 실제 PostgreSQL, Cognito 없음.

지키는 것: --base-url 안전 검사, 미리보기 무변경, 새 파일에만 링크(덮어쓰기 · 심볼릭 링크 거절, 표준 출력에 토큰 없음),
DB 에는 해시만, 파일 · DB 실패 처분(파일 실패면 DB 없음 · DB 실패면 파일 삭제 · commit 불명이면 파일 보존 + 실패),
POSIX 의 0600 전용 파일 · 비 POSIX 거절, list 에 토큰 · 해시 없음, revoke 는 ISSUED · RESERVED 만이고 계정 · 세션 무변화,
CLI 가 만든 링크를 API 가 받아 계정 1개를 만든다.
"""

from __future__ import annotations

import io
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from auth import bootstrap_enrollment
from auth.bootstrap_enrollment import OutputError, link_origin, open_private_output
from auth.cognito import CognitoUser
from auth.http import get_clock
from auth.sessions import hash_token
from config import AuthSettings, get_auth_settings
from db import models
from db.repositories import auth as auth_repo
from db.repositories import enrollment as enrollment_repo
from routers.users import get_cognito_adapter_factory
from schemas.api.auth import AccountType, UserRole
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

BASE = "https://app.example.com"
T0 = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
S = models.PlatformEnrollmentStatus
# FE 가 링크 조각에서 받는 토큰 모양
FE_TOKEN = re.compile(r"[A-Za-z0-9._~-]{16,512}")
POSIX = os.name == "posix"


def exclusive_opener(path: Path) -> int:
    """플랫폼과 무관한 대역(새 파일만 · 덮어쓰기 없음) — 권한 보장은 open_private_output 테스트(POSIX)가 본다."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)


def run(db, *argv, opener=exclusive_opener, now=T0):
    out = io.StringIO()
    code = bootstrap_enrollment.run(list(argv), db=db, now=lambda: now, out=out, open_output=opener)
    return code, out.getvalue()


def issue_args(path, *extra):
    return ["issue", "--base-url", BASE, "--output", str(path), *extra]


def rows(db):
    return db.execute(
        text("SELECT enrollment_id::text AS id, token_hash, status::text AS status, created_at, expires_at"
             " FROM platform_enrollments ORDER BY enrollment_id")
    ).all()


def links(path: Path) -> list[tuple[str, str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [tuple(line.split("\t")) for line in lines if not line.startswith("#")]


def token_of(url: str) -> str:
    prefix = f"{BASE}/team/enroll#token="
    assert url.startswith(prefix)
    return url[len(prefix):]


def no_token_in(out: str) -> bool:
    return "#token=" not in out.replace("#token=<비공개>", "")


# ----- --base-url -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, origin",
    [
        ("https://app.example.com", "https://app.example.com"),
        ("https://App.Example.com:8443/", "https://app.example.com:8443"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://127.0.0.1:3000", "http://127.0.0.1:3000"),
        ("http://[::1]:3000", "http://[::1]:3000"),
    ],
)
def test_link_origin_accepts_https_and_loopback_http(raw, origin):
    assert link_origin(raw) == origin


@pytest.mark.parametrize(
    "raw",
    [
        "http://app.example.com",
        "http://10.0.0.5:3000",
        "https://user:pw@app.example.com",
        "https://user@app.example.com",
        "https://app.example.com/team",
        "https://app.example.com/?x=1",
        "https://app.example.com?",
        "https://app.example.com/#token=x",
        "https://app.example.com:99999",
        "https://",
        "ftp://app.example.com",
        "javascript:alert(1)",
        "//app.example.com",
        "app.example.com",
        " https://app.example.com",
        "https://app.example.com\\@evil.example.com",
        "https://app.exa mple.com",
    ],
)
def test_link_origin_rejects_unsafe_bases(raw):
    with pytest.raises(ValueError):
        link_origin(raw)


# ----- 발급 ---------------------------------------------------------------------


def test_preview_writes_nothing(db, tmp_path):
    path = tmp_path / "links.txt"
    code, out = run(db, *issue_args(path))
    assert code == 0
    assert "미리보기" in out
    assert not path.exists()
    assert rows(db) == []


@pytest.mark.parametrize(
    "extra",
    [["--count", "0"], ["--count", "21"], ["--valid-hours", "0"], ["--valid-hours", "721"]],
)
def test_out_of_range_inputs_are_refused_before_any_write(db, tmp_path, extra):
    path = tmp_path / "links.txt"
    code, _ = run(db, *issue_args(path, *extra, "--yes"))
    assert code == 2
    assert not path.exists()
    assert rows(db) == []


def test_unsafe_base_url_is_refused_before_any_write(db, tmp_path):
    path = tmp_path / "links.txt"
    code, out = run(db, "issue", "--base-url", "http://app.example.com", "--output", str(path), "--yes")
    assert code == 2
    assert "--base-url" in out
    assert not path.exists()
    assert rows(db) == []


@pytest.mark.parametrize("yes", [[], ["--yes"]])
def test_existing_output_is_never_overwritten(db, tmp_path, yes):
    path = tmp_path / "links.txt"
    path.write_text("keep", encoding="utf-8")
    code, _ = run(db, *issue_args(path, *yes))
    assert code == 2
    assert path.read_text(encoding="utf-8") == "keep"
    assert rows(db) == []


def test_issue_writes_links_only_to_the_file_and_only_hashes_to_db(db, tmp_path):
    path = tmp_path / "links.txt"
    code, out = run(db, *issue_args(path, "--count", "3", "--valid-hours", "48", "--yes"))
    assert code == 0
    issued = links(path)
    assert len(issued) == 3
    tokens = [token_of(url) for _, _, url in issued]
    assert all(FE_TOKEN.fullmatch(t) for t in tokens)
    assert len(set(tokens)) == 3
    stored = {r.id: r for r in rows(db)}
    assert set(stored) == {eid for eid, _, _ in issued}
    for eid, expires, url in issued:
        row = stored[eid]
        assert row.token_hash == hash_token(token_of(url))
        assert (row.status, row.created_at, row.expires_at) == ("ISSUED", T0, T0 + timedelta(hours=48))
        assert expires == row.expires_at.isoformat()
    dumped = " ".join(str(v) for r in rows(db) for v in r)
    assert not any(t in dumped for t in tokens)
    assert not any(t in out for t in tokens) and no_token_in(out)
    assert all(eid in out for eid in stored)
    assert "완료" in out


def test_default_count_is_four(db, tmp_path):
    path = tmp_path / "links.txt"
    assert run(db, *issue_args(path, "--yes"))[0] == 0
    assert len(links(path)) == 4
    assert len(rows(db)) == 4


def test_output_creation_failure_writes_no_db_rows(db, tmp_path):
    def refuse(_path):
        raise OutputError("거절된 위치")

    path = tmp_path / "links.txt"
    code, out = run(db, *issue_args(path, "--yes"), opener=refuse)
    assert code == 1
    assert "DB 기록 없음" in out
    assert rows(db) == [] and not path.exists()


def test_output_write_failure_removes_partial_file_and_writes_no_db_rows(db, tmp_path):
    def read_only(path):
        return os.open(path, os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o600)

    path = tmp_path / "links.txt"
    code, out = run(db, *issue_args(path, "--yes"), opener=read_only)
    assert code == 1
    assert rows(db) == [] and not path.exists()
    assert no_token_in(out)


def test_db_failure_before_commit_removes_file_and_reports_failure(db, tmp_path, monkeypatch):
    def failing_add(*args, **kwargs):
        raise OperationalError("INSERT", {}, Exception("db down"))

    monkeypatch.setattr(bootstrap_enrollment.enrollment_repo, "add", failing_add)
    path = tmp_path / "links.txt"
    code, out = run(db, *issue_args(path, "--yes"))
    monkeypatch.undo()
    assert code == 1
    assert "쓸 수 없다" in out and "완료" not in out
    assert not path.exists()
    assert rows(db) == []
    assert no_token_in(out)


def test_ambiguous_commit_keeps_file_and_does_not_claim_success(db, tmp_path, monkeypatch):
    def failing_commit():
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(db, "commit", failing_commit)
    path = tmp_path / "links.txt"
    code, out = run(db, *issue_args(path, "--count", "2", "--yes"))
    monkeypatch.undo()
    assert code == 1
    assert "완료" not in out
    issued = links(path)  # 쓸 수 있을지도 모르는 링크를 조용히 버리지 않는다
    assert len(issued) == 2
    assert all(eid in out for eid, _, _ in issued)
    assert no_token_in(out) and not any(token_of(url) in out for _, _, url in issued)


# ----- 운영자 전용 파일 -----------------------------------------------------------


@pytest.mark.skipif(not POSIX, reason="POSIX 권한 · O_NOFOLLOW 검사")
def test_private_output_is_a_new_owner_only_file(tmp_path):
    path = tmp_path / "links.txt"
    fd = open_private_output(path)
    os.close(fd)
    info = os.stat(path)
    assert info.st_mode & 0o777 == 0o600
    assert info.st_uid == os.geteuid()
    with pytest.raises(OutputError):
        open_private_output(path)


@pytest.mark.skipif(not POSIX, reason="POSIX 심볼릭 링크 검사")
@pytest.mark.parametrize("dangling", [False, True])
def test_private_output_refuses_symlinks(tmp_path, dangling):
    target = tmp_path / "target.txt"
    if not dangling:
        target.write_text("keep", encoding="utf-8")
    link = tmp_path / "links.txt"
    link.symlink_to(target)
    with pytest.raises(OutputError):
        open_private_output(link)
    assert (target.read_text(encoding="utf-8") == "keep") if not dangling else not target.exists()


@pytest.mark.skipif(not POSIX, reason="POSIX 실제 출력 경로")
def test_issue_with_real_private_output(db, tmp_path):
    path = tmp_path / "links.txt"
    code, out = run(db, *issue_args(path, "--count", "1", "--yes"), opener=open_private_output)
    assert code == 0
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert len(links(path)) == 1 and len(rows(db)) == 1
    assert no_token_in(out)


@pytest.mark.skipif(POSIX, reason="비 POSIX 거절 경로")
def test_non_posix_output_fails_closed_without_db_rows(db, tmp_path):
    path = tmp_path / "links.txt"
    with pytest.raises(OutputError, match="POSIX"):
        open_private_output(path)
    code, out = run(db, *issue_args(path, "--yes"), opener=open_private_output)
    assert code == 1
    assert "POSIX" in out and "DB 기록 없음" in out
    assert not path.exists() and rows(db) == []


# ----- 목록 · 회수 -----------------------------------------------------------------


def _seed(db, status=S.ISSUED, *, expires_at=T0 + timedelta(hours=1)):
    token = uuid.uuid4().hex
    row = enrollment_repo.add(
        db, enrollment_id=str(uuid.uuid4()), token_hash=hash_token(token),
        now=T0 - timedelta(hours=2), expires_at=expires_at,
    )
    if status is not S.ISSUED:
        enrollment_repo.reserve(db, token_hash=row.token_hash, email="r@example.com", now=T0 - timedelta(hours=1))
    if status is S.REDEEMED:
        user = auth_repo.create_user(
            db, issuer="https://issuer.example", cognito_sub=f"sub-{token}", email="r@example.com",
            role=UserRole.ADMIN, account_type=AccountType.PLATFORM, tenant_id=None, now=T0,
        )
        db.add(models.AuthSession(
            token_hash=hash_token("session-" + token), user_id=user.user_id,
            mfa_step=models.AuthSessionMfaStep.SOFTWARE_TOKEN_MFA,
            created_at=T0, last_activity_at=T0, absolute_expires_at=T0 + timedelta(hours=8),
        ))
        locked = enrollment_repo.lock(db, row.enrollment_id)
        locked.status, locked.redeemed_user_id, locked.redeemed_at = S.REDEEMED, user.user_id, T0
    if status is S.REVOKED:
        enrollment_repo.revoke(db, enrollment_id=row.enrollment_id, now=T0)
    db.commit()
    return row.enrollment_id, row.token_hash


def accounts(db):
    return db.execute(
        text("SELECT u.user_id::text, u.status::text, u.role::text, s.revoked_at"
             " FROM app_users u LEFT JOIN auth_sessions s ON s.user_id = u.user_id ORDER BY 1")
    ).all()


def test_list_shows_metadata_only_with_derived_expiry(db):
    seeded = {
        "ISSUED": _seed(db),
        "EXPIRED": _seed(db, expires_at=T0),
        "RESERVED": _seed(db, S.RESERVED),
        "REDEEMED": _seed(db, S.REDEEMED),
        "REVOKED": _seed(db, S.REVOKED),
    }
    code, out = run(db, "list")
    assert code == 0
    for shown, (eid, token_hash) in seeded.items():
        line = next(line for line in out.splitlines() if eid in line)
        assert f"status={shown}" in line
        assert token_hash not in out
    assert "email=r@example.com" in next(line for line in out.splitlines() if seeded["RESERVED"][0] in line)
    assert "합계:" in out and "RESERVED=1" in out
    assert "Cognito" in out  # 예약 행의 정리 안내


def test_list_without_links(db):
    assert run(db, "list") == (0, "등록 링크 없음\n")


@pytest.mark.parametrize("status", [S.ISSUED, S.RESERVED])
def test_revoke_preview_then_apply_touches_only_the_link(db, status):
    eid, _ = _seed(db, status)
    other, _ = _seed(db, S.REDEEMED)
    before = accounts(db)

    code, out = run(db, "revoke", "--enrollment-id", eid)
    assert code == 0 and "미리보기" in out
    assert enrollment_repo.get(db, eid).status is status
    if status is S.RESERVED:
        assert "r@example.com" in out and "Cognito" in out

    code, out = run(db, "revoke", "--enrollment-id", eid.upper(), "--yes")
    assert code == 0 and "REVOKED" in out
    row = enrollment_repo.get(db, eid)
    assert (row.status, row.revoked_at) == (S.REVOKED, T0)
    assert accounts(db) == before
    assert enrollment_repo.get(db, other).status is S.REDEEMED


@pytest.mark.parametrize("status", [S.REDEEMED, S.REVOKED])
def test_revoke_refuses_used_or_revoked_links_and_never_touches_accounts(db, status):
    eid, _ = _seed(db, status)
    before = accounts(db)
    code, _ = run(db, "revoke", "--enrollment-id", eid, "--yes")
    assert code == 2
    assert enrollment_repo.get(db, eid).status is status
    assert accounts(db) == before


@pytest.mark.parametrize("value", ["not-a-uuid", str(uuid.uuid4())])
def test_revoke_refuses_unknown_or_malformed_ids(db, value):
    assert run(db, "revoke", "--enrollment-id", value, "--yes")[0] == 2


# ----- CLI 링크 → API ---------------------------------------------------------------


class _Adapter:
    issuer = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_cli"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def admin_create_user(self, email, *, resend=False):
        self.calls.append(email)
        return CognitoUser(email, "sub-from-cli-link", email, "FORCE_CHANGE_PASSWORD")


def test_cli_link_is_accepted_once_by_the_api(db, tmp_path, client_pg_anon):
    path = tmp_path / "links.txt"
    assert run(db, *issue_args(path, "--count", "1", "--yes"))[0] == 0
    (_, _, url), = links(path)
    adapter = _Adapter()
    app = client_pg_anon.app
    app.dependency_overrides[get_auth_settings] = lambda: AuthSettings(
        _env_file=None, AUTH_ALLOWED_ORIGINS=BASE, AUTH_PLATFORM_ENROLLMENT_ENABLED=True
    )
    app.dependency_overrides[get_clock] = lambda: (lambda: T0)
    app.dependency_overrides[get_cognito_adapter_factory] = lambda: (lambda: adapter)

    body = {"token": token_of(url), "email": "first@example.com"}
    first = client_pg_anon.post("/api/v1/auth/platform-enrollment", json=body, headers={"Origin": BASE})
    second = client_pg_anon.post("/api/v1/auth/platform-enrollment", json=body, headers={"Origin": BASE})
    assert first.status_code == 204
    assert (second.status_code, second.json()["error"]["code"]) == (404, "ENROLLMENT_UNAVAILABLE")
    assert adapter.calls == ["first@example.com"]
    assert [r.status for r in rows(db)] == ["REDEEMED"]
