"""플랫폼 계정 등록 링크(1회용) 운영 CLI (#492) — 발급 · 목록 · 회수.

    cd apps/core-api
    uv run python -m auth.bootstrap_enrollment issue --base-url https://<웹 origin> --output <새 파일>          # 미리보기(변경 없음)
    uv run python -m auth.bootstrap_enrollment issue --base-url https://<웹 origin> --output <새 파일> --yes    # 발급
    uv run python -m auth.bootstrap_enrollment issue ... --count 4 --valid-hours 72                           # 개수 · 유효 시간(기본값)
    uv run python -m auth.bootstrap_enrollment list                                                           # 메타데이터만
    uv run python -m auth.bootstrap_enrollment revoke --enrollment-id <id>                                    # 회수 미리보기
    uv run python -m auth.bootstrap_enrollment revoke --enrollment-id <id> --yes

- 링크 1개 = PLATFORM ADMIN 계정 1개. 받는 사람이 링크 화면에서 고른 이메일로 Cognito 초대(임시 비밀번호 메일)가 나가고,
  첫 로그인은 기존 /login 단계다. API 의 AUTH_PLATFORM_ENROLLMENT_ENABLED 가 꺼져 있으면 링크는 받아들여지지 않는다.
- 발급은 이 환경의 DB(DATABASE_URL)와 출력 파일만 쓴다 — Cognito 를 부르지 않고 그 설정도 필요 없다.
  DB 에는 토큰의 SHA-256 만 남는다.
- 링크 원문은 --output 의 새 파일에만 쓴다(표준 출력 · 로그에 싣지 않는다). 파일은 POSIX 에서 O_CREAT|O_EXCL|O_NOFOLLOW,
  0600 으로 새로 만들고 소유자 전용 일반 파일인지 확인한다. 이미 있는 경로(심볼릭 링크 포함)는 덮어쓰지 않는다.
  Windows 등 POSIX 가 아닌 환경은 0600 이 접근 제어(ACL)를 보장하지 않으므로 발급을 거절한다 — 배포 API 컨테이너
  (Linux) 같은 POSIX 환경에서 실행한다. 권한을 지키지 않는 파일 시스템(예: 일부 마운트)도 거절한다.
- 순서: 출력 파일 쓰기 · fsync → DB 기록 commit. 파일 단계가 실패하면 DB 를 쓰지 않는다. DB 기록이 commit 전에 실패하면
  그 링크는 쓸 수 없으므로 파일을 지운다. commit 결과가 불명이면 파일을 남기고 실패로 끝낸다 — list 로 ID 를 확인한다.
- --base-url 은 https origin 이다(경로 · 쿼리 · 조각 · 자격증명 없음). http 는 loopback(localhost · 127.0.0.0/8 · ::1)
  개발 주소만 받는다. 링크는 <origin>/team/enroll#token=<토큰> 이다(조각은 서버 요청 · Referer 에 실리지 않는다).
- list 는 토큰 · 해시를 출력하지 않는다. RESERVED 는 Cognito 사용자 생성이 시작됐을 수 있고 앱 계정은 없는 결과 불명
  상태다 — Cognito 에서 그 이메일의 사용자를 확인해 정리한 뒤 revoke · 재발급한다(자동 복구 없음).
- revoke 는 ISSUED · RESERVED 링크만 회수한다. 앱 계정 · 세션과 Cognito 사용자는 바꾸지 않는다.
- 종료 코드: 0 성공 · 1 실패 · 2 거절.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import stat
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

# python -m 로 직접 실행될 때 main.py 와 같은 방식으로 import 경로를 잡는다
_CORE_API = Path(__file__).resolve().parents[1]
for _p in (str(_CORE_API), str(_CORE_API.parents[1] / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db import models
from db.repositories import enrollment as enrollment_repo
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth.bootstrap_admin import EXIT_FAILED, EXIT_OK, EXIT_REFUSED
from auth.sessions import hash_token

ENROLL_PATH = "/team/enroll"
DEFAULT_COUNT = 4
MAX_COUNT = 20
DEFAULT_VALID_HOURS = 72
MAX_VALID_HOURS = 720
_TOKEN_BYTES = 32
_S = models.PlatformEnrollmentStatus
_RESERVED_NOTE = (
    "RESERVED: Cognito 사용자 생성이 시작됐을 수 있고 앱 계정은 없다(결과 불명, 자동 복구 없음). "
    "Cognito 에서 그 email 의 사용자를 확인해 정리한 뒤 revoke · 재발급한다."
)


class OutputError(Exception):
    """출력 파일을 운영자 전용 새 파일로 만들 수 없음. 메시지는 사유뿐이다(경로 · 비밀값 없음)."""


def open_private_output(path: Path) -> int:
    """운영자 전용 새 파일을 만들어 쓰기 fd 를 돌려준다(POSIX 전용). 실패는 OutputError."""
    if os.name != "posix":
        raise OutputError(
            "이 실행 환경은 POSIX 가 아니라 출력 파일을 소유자 전용으로 보장할 수 없다(0600 은 Windows ACL 이 아니다) — "
            "배포 API 컨테이너(Linux) 등 POSIX 환경에서 실행한다"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise OutputError("이미 있는 경로다(심볼릭 링크 포함) — 덮어쓰지 않는다. 새 파일 경로를 준다") from None
    except OSError as exc:
        raise OutputError(
            f"파일을 만들지 못했다({type(exc).__name__}) — 디렉터리 존재 · 쓰기 권한 · 경로를 확인한다"
        ) from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(fd)
        _remove(path)
        raise OutputError(
            "만든 파일이 소유자 전용(0600) 일반 파일이 아니다 — 권한을 지키지 않는 파일 시스템이다. 다른 위치를 준다"
        )
    return fd


def _remove(path: Path) -> bool:
    try:
        os.remove(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def link_origin(raw: str) -> str:
    """--base-url → 링크의 origin(scheme://host[:port]). 안전하지 않으면 ValueError(사유)."""
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in raw):
        raise ValueError("공백 · 제어 문자 · 역슬래시를 넣지 않는다")
    if "?" in raw or "#" in raw:
        raise ValueError("쿼리 · 조각(#)을 넣지 않는다")
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http"):
        raise ValueError("https:// origin 이어야 한다")
    if "@" in parts.netloc:
        raise ValueError("자격증명(user@)을 넣지 않는다")
    if parts.path not in ("", "/"):
        raise ValueError("경로 없는 origin 이어야 한다(링크 경로는 CLI 가 붙인다)")
    if not parts.hostname:
        raise ValueError("호스트가 없다")
    try:
        parts.port
    except ValueError:
        raise ValueError("포트가 올바르지 않다") from None
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ValueError("http 는 loopback(localhost · 127.0.0.0/8 · ::1) 개발 주소만 받는다 — 그 밖은 https")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m auth.bootstrap_enrollment",
        description="플랫폼 계정 등록 링크(1회용, PLATFORM ADMIN)를 발급 · 조회 · 회수한다.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="링크를 발급해 새 운영자 전용 파일에 쓴다(기본은 미리보기).")
    issue.add_argument("--base-url", required=True, help="웹 화면 origin(https, 개발은 http://localhost:PORT).")
    issue.add_argument("--output", required=True, help="링크를 쓸 새 파일 경로(이미 있으면 거절).")
    issue.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"발급 개수 1–{MAX_COUNT}.")
    issue.add_argument(
        "--valid-hours", type=int, default=DEFAULT_VALID_HOURS, help=f"유효 시간 1–{MAX_VALID_HOURS}."
    )
    issue.add_argument("--yes", action="store_true", help="실제로 발급한다(기본은 미리보기).")
    sub.add_parser("list", help="발급 기록(토큰 · 해시 없음).")
    revoke = sub.add_parser("revoke", help="ISSUED · RESERVED 링크 회수(계정 · 세션 무관).")
    revoke.add_argument("--enrollment-id", required=True)
    revoke.add_argument("--yes", action="store_true", help="실제로 회수한다(기본은 미리보기).")
    return parser


def _write(fd: int, text: str) -> None:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _issue(
    args: argparse.Namespace,
    *,
    db: Session,
    now: Callable[[], datetime],
    say: Callable[[str], None],
    open_output: Callable[[Path], int],
) -> int:
    try:
        origin = link_origin(args.base_url)
    except ValueError as exc:
        say(f"거절: --base-url — {exc}")
        return EXIT_REFUSED
    if not 1 <= args.count <= MAX_COUNT:
        say(f"거절: --count 는 1–{MAX_COUNT} 이다.")
        return EXIT_REFUSED
    if not 1 <= args.valid_hours <= MAX_VALID_HOURS:
        say(f"거절: --valid-hours 는 1–{MAX_VALID_HOURS} 이다.")
        return EXIT_REFUSED
    path = Path(args.output)
    shown = os.path.abspath(path)
    if os.path.lexists(path):
        say(f"거절: --output {shown} 이 이미 있다(심볼릭 링크 포함) — 덮어쓰지 않는다. 새 파일 경로를 준다.")
        return EXIT_REFUSED

    stamp = now()
    expires_at = stamp + timedelta(hours=args.valid_hours)
    say(f"대상: 링크 {args.count}개 — 링크마다 PLATFORM ADMIN 계정 1개(1회용), 만료 {expires_at.isoformat()}")
    say(f"링크: {origin}{ENROLL_PATH}#token=<비공개>")
    say(f"출력: {shown} (새 파일 · 운영자 전용, 표준 출력에는 링크를 싣지 않는다)")
    if not args.yes:
        say("미리보기 — 변경 없음. 적용하려면 --yes 를 붙인다.")
        return EXIT_OK

    ids = [str(uuid.uuid4()) for _ in range(args.count)]
    tokens = [secrets.token_urlsafe(_TOKEN_BYTES) for _ in ids]
    try:
        fd = open_output(path)
    except OutputError as exc:
        say(f"실패: 출력 파일 — {exc}. 변경 없음(DB 기록 없음).")
        return EXIT_FAILED
    body = "".join(
        [
            "# Vigilantis 플랫폼 계정 등록 링크 — 1회용 · 운영자 전용. 링크마다 PLATFORM ADMIN 계정 1개.\n",
            "# 받는 사람에게 하나씩 따로 전달하고, 전달이 끝나면 이 파일을 지운다.\n",
            "# enrollment_id\texpires_at(UTC)\tlink\n",
            *(
                f"{eid}\t{expires_at.isoformat()}\t{origin}{ENROLL_PATH}#token={tok}\n"
                for eid, tok in zip(ids, tokens)
            ),
        ]
    )
    try:
        _write(fd, body)
    except OSError as exc:
        removed = _remove(path)
        say(
            f"실패: 출력 파일 쓰기 오류 {type(exc).__name__}. DB 기록 없음 — "
            + ("불완전한 파일을 지웠다." if removed else f"불완전한 파일 {shown} 을 직접 지운다(그 링크는 쓸 수 없다).")
        )
        return EXIT_FAILED

    try:
        for eid, tok in zip(ids, tokens):
            enrollment_repo.add(
                db, enrollment_id=eid, token_hash=hash_token(tok), now=stamp, expires_at=expires_at
            )
    except SQLAlchemyError as exc:
        db.rollback()
        removed = _remove(path)
        say(
            f"실패: DB 기록 오류 {type(exc).__name__}. 링크는 기록되지 않아 쓸 수 없다 — "
            + ("출력 파일을 지웠다." if removed else f"출력 파일 {shown} 을 직접 지운다.")
        )
        return EXIT_FAILED
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        say(f"실패: DB commit 오류 {type(exc).__name__} — 기록됐는지 알 수 없다. 출력 파일 {shown} 은 남겼다.")
        say("list 로 아래 ID 를 확인한다 — 있으면 쓸 수 있는 링크다(쓰지 않을 것은 revoke), 없으면 파일을 지우고 다시 발급한다.")
        say("enrollment_id: " + " ".join(ids))
        return EXIT_FAILED

    say(f"완료: 발급 {len(ids)}건, 만료 {expires_at.isoformat()}")
    say("enrollment_id: " + " ".join(ids))
    say(f"링크 파일: {shown} — 운영자 전용. 링크를 하나씩 따로 전달하고 파일을 지운다.")
    return EXIT_OK


def _list(*, db: Session, now: Callable[[], datetime], say: Callable[[str], None]) -> int:
    rows = enrollment_repo.list_all(db)
    db.rollback()
    if not rows:
        say("등록 링크 없음")
        return EXIT_OK
    stamp = now()
    counts: Counter[str] = Counter()
    for row in rows:
        shown = "EXPIRED" if row.status is _S.ISSUED and row.expires_at <= stamp else row.status.value
        counts[shown] += 1
        fields = [
            f"enrollment_id={row.enrollment_id}",
            f"status={shown}",
            f"created_at={row.created_at.isoformat()}",
            f"expires_at={row.expires_at.isoformat()}",
        ]
        if row.reserved_at is not None:
            fields += [f"reserved_at={row.reserved_at.isoformat()}", f"email={row.email}"]
        if row.redeemed_user_id is not None:
            fields += [f"user_id={row.redeemed_user_id}", f"redeemed_at={row.redeemed_at.isoformat()}"]
        if row.revoked_at is not None:
            fields.append(f"revoked_at={row.revoked_at.isoformat()}")
        say(" ".join(fields))
    say("합계: " + " ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    if counts[_S.RESERVED.value]:
        say(_RESERVED_NOTE)
    return EXIT_OK


def _revoke_blocker(row: models.PlatformEnrollment | None) -> str | None:
    if row is None:
        return "그 enrollment_id 의 링크가 없다."
    if row.status is _S.REDEEMED:
        return f"이미 계정이 만들어졌다(user_id={row.redeemed_user_id}). 링크 회수로 계정 · 세션을 바꾸지 않는다."
    if row.status is _S.REVOKED:
        return "이미 회수됐다."
    return None


def _revoke(
    args: argparse.Namespace,
    *,
    db: Session,
    now: Callable[[], datetime],
    say: Callable[[str], None],
) -> int:
    try:
        enrollment_id = str(uuid.UUID(args.enrollment_id.strip()))
    except ValueError:
        say("거절: enrollment_id 형식이 올바르지 않다.")
        return EXIT_REFUSED
    row = enrollment_repo.get(db, enrollment_id)
    db.rollback()
    blocker = _revoke_blocker(row)
    if blocker:
        say(f"거절: {blocker}")
        return EXIT_REFUSED
    say(f"대상: enrollment_id={enrollment_id} status={row.status.value} → REVOKED (계정 · 세션 · Cognito 는 바꾸지 않는다)")
    if row.status is _S.RESERVED:
        say(f"주의: 예약된 링크다(email={row.email}). Cognito 사용자가 생겼을 수 있다 — 회수해도 지우지 않는다.")
    if not args.yes:
        say("미리보기 — 변경 없음. 적용하려면 --yes 를 붙인다.")
        return EXIT_OK
    try:
        if not enrollment_repo.revoke(db, enrollment_id=enrollment_id, now=now()):
            db.rollback()
            say("거절: 그 사이 상태가 바뀌었다(사용 · 회수됨). list 로 확인한다.")
            return EXIT_REFUSED
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        say(f"실패: DB 오류 {type(exc).__name__}. list 로 상태를 확인한다.")
        return EXIT_FAILED
    say(f"완료: enrollment_id={enrollment_id} status=REVOKED")
    return EXIT_OK


def run(
    argv: list[str],
    *,
    db: Session,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    out: TextIO = sys.stdout,
    open_output: Callable[[Path], int] = open_private_output,
) -> int:
    args = _parser().parse_args(argv)

    def say(message: str) -> None:
        print(message, file=out)

    if args.command == "issue":
        return _issue(args, db=db, now=now, say=say, open_output=open_output)
    if args.command == "list":
        return _list(db=db, now=now, say=say)
    return _revoke(args, db=db, now=now, say=say)


def main(argv: list[str] | None = None) -> int:
    from db.session import get_session_factory

    argv = sys.argv[1:] if argv is None else argv
    with get_session_factory()() as db:
        return run(argv, db=db)


if __name__ == "__main__":
    raise SystemExit(main())
