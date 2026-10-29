"""플랫폼 계정 수명주기 운영 CLI (#492) — 플랫폼 계정은 이 CLI 로 직접 만들고 지정해서만 생긴다.

    cd apps/core-api
    # 플랫폼 계정 만들기(첫 계정이든 추가 계정이든 권한 동일). --platform 필수.
    uv run python -m auth.bootstrap_admin --platform --email ops@example.com            # 미리보기(변경 없음)
    uv run python -m auth.bootstrap_admin --platform --email ops@example.com --yes      # Cognito 사용자 생성 + 등록
    uv run python -m auth.bootstrap_admin --platform --existing-cognito-user --email ... --yes   # 기존 Cognito 사용자 연결
    # 이미 있는 UNASSIGNED 계정을 user_id 로 플랫폼 계정으로 지정(Cognito 호출 없음)
    uv run python -m auth.bootstrap_admin --designate-unassigned-user-id <user_id>       # 미리보기
    uv run python -m auth.bootstrap_admin --designate-unassigned-user-id <user_id> --yes

- app_users 가 비어 있지 않아도 플랫폼 계정을 추가할 수 있다. --platform 이 없으면 거절한다
  (ADMIN 이나 tenant_id NULL 로 플랫폼을 추론하지 않는다). 플랫폼 역할 등급 · 옵션은 없다.
- 지정은 tenant_id NULL 이고 ACTIVE 인 UNASSIGNED 만 받는다. TENANT · 비활성 계정은 거절하며 복구하지 않는다.
  지정하면 role=ADMIN · account_type=PLATFORM 으로 바꾸고 기존 세션을 ADMIN_REVOKED 로 폐기한다(새 로그인 필요).
- 기존 신원((issuer, sub))이 이미 app_users 에 있으면 만들기는 거절한다 — 고객사 소속을 옮기지 않는다.
- 삭제 · 비활성화 · 강등 경로는 없다.
- 기존 Cognito 사용자 연결은 운영자가 --existing-cognito-user 로 명시할 때만 한다(자동 연결 없음).
- 임시 비밀번호 · 비밀값 · 토큰은 출력하지 않는다. user_id 와 Cognito 사용자 상태만 출력한다.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

# python -m 로 직접 실행될 때 main.py 와 같은 방식으로 import 경로를 잡는다
_CORE_API = Path(__file__).resolve().parents[1]
for _p in (str(_CORE_API), str(_CORE_API.parents[1] / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from botocore.exceptions import BotoCoreError, ClientError
from config import AuthSettings, get_auth_settings
from db import models
from db.repositories import auth as auth_repo
from schemas.api.auth import AccountType, UserRole
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth.provider import build_cognito_client

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m auth.bootstrap_admin",
        description="플랫폼 계정을 만들거나, 기존 UNASSIGNED 계정을 플랫폼 계정으로 지정한다.",
    )
    parser.add_argument("--platform", action="store_true", help="플랫폼 계정 생성을 명시한다(생성에 필수).")
    parser.add_argument("--email")
    parser.add_argument(
        "--designate-unassigned-user-id",
        metavar="USER_ID",
        help="이미 있는 UNASSIGNED 계정(user_id)을 플랫폼 계정으로 지정한다. Cognito 호출 없음.",
    )
    parser.add_argument(
        "--existing-cognito-user",
        action="store_true",
        help="Cognito 사용자를 새로 만들지 않고 이미 있는 사용자(AdminGetUser)를 연결한다.",
    )
    parser.add_argument("--yes", action="store_true", help="실제로 적용한다(기본은 미리보기).")
    return parser


def _issuer(settings: AuthSettings) -> str:
    return (
        f"https://cognito-idp.{settings.COGNITO_REGION}.amazonaws.com/"
        f"{settings.COGNITO_USER_POOL_ID}"
    )


def _sub(user_attributes: list[dict[str, str]]) -> str | None:
    return next((a["Value"] for a in user_attributes if a["Name"] == "sub"), None)


def _designate_blocker(user: models.AppUser | None) -> str | None:
    """지정할 수 없는 사유. 지정할 수 있으면 None."""
    if user is None:
        return "그 user_id 의 계정이 없다."
    if user.status is not models.AppUserStatus.ACTIVE:
        return "ACTIVE 가 아니다 — 비활성 계정은 복구하지 않고 거절한다."
    if user.account_type is not AccountType.UNASSIGNED:
        return f"account_type={user.account_type.value} — UNASSIGNED 만 지정할 수 있다."
    if user.tenant_id is not None:
        return "tenant_id 가 있다 — 고객사 소속 계정은 지정할 수 없다."
    return None


def _designate(
    args: argparse.Namespace,
    *,
    db: Session,
    now: Callable[[], datetime],
    say: Callable[[str], None],
) -> int:
    try:
        user_id = str(uuid.UUID(args.designate_unassigned_user_id.strip()))
    except ValueError:
        say("거절: user_id 형식이 올바르지 않다.")
        return EXIT_REFUSED

    blocker = _designate_blocker(auth_repo.get_user(db, user_id))
    db.rollback()
    if blocker:
        say(f"거절: {blocker}")
        return EXIT_REFUSED
    say(f"대상: user_id={user_id} UNASSIGNED → PLATFORM (role=ADMIN, 기존 세션 폐기)")
    if not args.yes:
        say("미리보기 — 변경 없음. 적용하려면 --yes 를 붙인다.")
        return EXIT_OK

    try:
        # 잠금 안에서 다시 확인하고 바꾼다 — 그 사이 소속·상태가 바뀐 계정을 지정하지 않는다
        auth_repo.lock_users_table(db)
        row = auth_repo.get_user(db, user_id)
        blocker = _designate_blocker(row)
        if blocker:
            db.rollback()
            say(f"거절: 그 사이 상태가 바뀌었다. {blocker}")
            return EXIT_REFUSED
        stamp = now()
        row.role = UserRole.ADMIN
        row.account_type = AccountType.PLATFORM
        row.updated_at = stamp
        db.flush()
        revoked = auth_repo.revoke_user_sessions(
            db,
            user_id=user_id,
            reason=models.AuthSessionRevokeReason.ADMIN_REVOKED,
            now=stamp,
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        say(f"실패: DB 오류 {type(exc).__name__}. 변경 없음.")
        return EXIT_FAILED
    say(f"완료: user_id={user_id} account_type=PLATFORM revoked_sessions={revoked} (새 로그인 필요)")
    return EXIT_OK


def run(
    argv: list[str],
    *,
    db: Session,
    settings: AuthSettings,
    client: Any,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    out: TextIO = sys.stdout,
) -> int:
    args = _parser().parse_args(argv)

    def say(message: str) -> None:
        print(message, file=out)

    if args.designate_unassigned_user_id is not None:
        if args.email or args.platform or args.existing_cognito_user:
            say("거절: --designate-unassigned-user-id 는 --email · --platform · --existing-cognito-user 와 함께 쓰지 않는다.")
            return EXIT_REFUSED
        return _designate(args, db=db, now=now, say=say)

    if not args.platform:
        say("거절: 플랫폼 계정을 만들려면 --platform 을 명시한다(ADMIN 이라고 플랫폼으로 추론하지 않는다).")
        return EXIT_REFUSED
    email = (args.email or "").strip()
    if not email:
        say("거절: --email 이 필요하다.")
        return EXIT_REFUSED

    mode = "기존 Cognito 사용자 연결" if args.existing_cognito_user else "Cognito 사용자 생성(초대 이메일 발송)"
    say(f"대상: pool={settings.COGNITO_USER_POOL_ID} email={email} account_type=PLATFORM role=ADMIN")
    say(f"방식: {mode}")
    if not args.yes:
        say("미리보기 — 변경 없음. 적용하려면 --yes 를 붙인다.")
        return EXIT_OK

    pool = settings.COGNITO_USER_POOL_ID
    try:
        if args.existing_cognito_user:
            resp = client.admin_get_user(UserPoolId=pool, Username=email)
            attributes, status = resp.get("UserAttributes", []), resp.get("UserStatus")
        else:
            resp = client.admin_create_user(
                UserPoolId=pool,
                Username=email,
                UserAttributes=[{"Name": "email", "Value": email}],
                DesiredDeliveryMediums=["EMAIL"],
            )
            user = resp.get("User", {})
            attributes, status = user.get("Attributes", []), user.get("UserStatus")
    except ClientError as exc:
        # 메시지는 싣지 않는다 — 오류 코드만
        say(f"실패: Cognito 오류 {exc.response.get('Error', {}).get('Code', 'Unknown')}")
        if not args.existing_cognito_user:
            say("이미 Cognito 에 있는 사용자면 --existing-cognito-user 로 연결한다.")
        return EXIT_FAILED
    except BotoCoreError as exc:
        say(f"실패: Cognito 호출 오류 {type(exc).__name__}")
        return EXIT_FAILED

    sub = _sub(attributes)
    if not sub:
        say("실패: Cognito 응답에 sub 가 없다.")
        return EXIT_FAILED

    try:
        # 확인과 삽입을 같은 잠금 안에서 — 동시 실행이 같은 신원((issuer, sub))을 둘 다 만들지 못하게 한다(이메일은 유일하지 않다)
        auth_repo.lock_users_table(db)
        issuer = _issuer(settings)
        if auth_repo.get_user_by_identity(db, issuer=issuer, cognito_sub=sub) is not None:
            db.rollback()
            say("거절: 그 신원((issuer, sub))의 계정이 이미 app_users 에 있다. 기존 계정은 바꾸지 않았다 — UNASSIGNED 면 --designate-unassigned-user-id 로 지정한다.")
            return EXIT_REFUSED
        row = auth_repo.create_user(
            db,
            issuer=issuer,
            cognito_sub=sub,
            email=email,
            role=UserRole.ADMIN,
            account_type=AccountType.PLATFORM,
            tenant_id=None,
            now=now(),
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        say(f"실패: DB 등록 오류 {type(exc).__name__}. Cognito 사용자는 남아 있을 수 있다 — --existing-cognito-user 로 다시 연결한다.")
        return EXIT_FAILED

    say(f"완료: user_id={row.user_id} cognito_status={status}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    from db.session import get_session_factory

    argv = sys.argv[1:] if argv is None else argv
    settings = get_auth_settings()
    # 지정은 Cognito 를 쓰지 않는다 — 자격 증명 없이 실행되고 클라이언트도 만들지 않는다
    designating = _parser().parse_args(argv).designate_unassigned_user_id is not None
    client = None
    if not designating:
        missing = settings.missing_cognito_keys()
        if missing:
            print(f"실패: Cognito 설정 누락 키: {', '.join(missing)}", file=sys.stderr)
            return EXIT_FAILED
        client = build_cognito_client(settings)
    with get_session_factory()() as db:
        return run(argv, db=db, settings=settings, client=client)


if __name__ == "__main__":
    raise SystemExit(main())
