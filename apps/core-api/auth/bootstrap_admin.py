"""최초 ADMIN 부트스트랩 운영 CLI (#492 PR 1).

    cd apps/core-api
    uv run python -m auth.bootstrap_admin --email admin@example.com            # 미리보기(변경 없음)
    uv run python -m auth.bootstrap_admin --email admin@example.com --yes      # Cognito 사용자 생성 + ADMIN 등록
    uv run python -m auth.bootstrap_admin --existing-cognito-user --email ... --yes   # 기존 Cognito 사용자 연결

- 첫 관리자 하나만 만든다. app_users 에 행이 하나라도 있으면 거절한다.
- 기존 Cognito 사용자 연결은 운영자가 --existing-cognito-user 로 명시할 때만 한다(자동 연결 없음).
- 임시 비밀번호 · 비밀값 · 토큰은 출력하지 않는다. user_id 와 Cognito 사용자 상태만 출력한다.
"""

from __future__ import annotations

import argparse
import sys
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
from db.repositories import auth as auth_repo
from schemas.api.auth import UserRole
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth.provider import build_cognito_client

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m auth.bootstrap_admin", description="최초 ADMIN 사용자 1명을 만든다."
    )
    parser.add_argument("--email", required=True)
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
    email = args.email.strip()

    def say(message: str) -> None:
        print(message, file=out)

    existing = auth_repo.count_users(db)
    db.rollback()  # 읽기 트랜잭션을 Cognito 호출 전에 끝낸다
    if existing > 0:
        say("거절: app_users 에 이미 사용자가 있다. 이 도구는 최초 관리자만 만든다.")
        return EXIT_REFUSED

    mode = "기존 Cognito 사용자 연결" if args.existing_cognito_user else "Cognito 사용자 생성(초대 이메일 발송)"
    say(f"대상: pool={settings.COGNITO_USER_POOL_ID} email={email} role=ADMIN")
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
        # 확인과 삽입을 같은 잠금 안에서 — 동시 실행이 둘 다 통과하지 못하게 한다
        auth_repo.lock_users_table(db)
        if auth_repo.count_users(db) > 0:
            db.rollback()
            say("거절: 그 사이 app_users 에 사용자가 생겼다. 만든 Cognito 사용자가 있으면 확인한다.")
            return EXIT_REFUSED
        row = auth_repo.create_user(
            db,
            issuer=_issuer(settings),
            cognito_sub=sub,
            email=email,
            role=UserRole.ADMIN,
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

    settings = get_auth_settings()
    missing = settings.missing_cognito_keys()
    if missing:
        print(f"실패: Cognito 설정 누락 키: {', '.join(missing)}", file=sys.stderr)
        return EXIT_FAILED
    with get_session_factory()() as db:
        return run(
            sys.argv[1:] if argv is None else argv,
            db=db,
            settings=settings,
            client=build_cognito_client(settings),
        )


if __name__ == "__main__":
    raise SystemExit(main())
