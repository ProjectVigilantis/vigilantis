"""플랫폼 계정 수명주기 운영 CLI (#492) — 플랫폼 계정은 이 CLI 로 직접 만들고 지정해서만 생긴다.

    cd apps/core-api
    # 직원 계정 만들기 — --platform 필수, 등급은 담당 고객사 배정이 없는 TENANT_MANAGER. 기본은 미리보기다.
    uv run python -m auth.bootstrap_admin --platform --email ops@example.com            # 미리보기(변경 없음)
    uv run python -m auth.bootstrap_admin --platform --email ops@example.com --yes      # Cognito 사용자 생성 + 등록
    uv run python -m auth.bootstrap_admin --platform --existing-cognito-user --email ... --yes   # 기존 Cognito 사용자 연결
    # 첫 중앙 관리자를 만들면서 지정 — 활성 중앙 관리자가 없을 때만
    uv run python -m auth.bootstrap_admin --platform --central-admin --email ... --yes
    # 이미 있는 UNASSIGNED 계정을 user_id 로 직원(배정 없는 TENANT_MANAGER)으로 지정(Cognito 호출 없음)
    uv run python -m auth.bootstrap_admin --designate-unassigned-user-id <user_id>       # 미리보기
    uv run python -m auth.bootstrap_admin --designate-unassigned-user-id <user_id> --yes
    # 이미 있는 PLATFORM 계정(TENANT_MANAGER)을 첫 중앙 관리자로 지정 — 활성 중앙 관리자가 없을 때만
    uv run python -m auth.bootstrap_admin --bootstrap-central-admin-user-id <user_id> --email ops@example.com --yes

- 플랫폼 관리 권한의 원천은 platform_role 하나다. PLATFORM 의 role=ADMIN 은 호환 값이고, ADMIN 이나
  tenant_id NULL 로 플랫폼 계정 · 관리 권한을 추론하지 않는다. --platform 이 없으면 만들기를 거절한다.
- 새 직원은 담당 고객사 배정이 없는 TENANT_MANAGER 다(고객사 범위 없음). 이 CLI 가 주는 높은 등급은
  CENTRAL_ADMIN 하나이며, ACTIVE 중앙 관리자가 0명일 때만(첫 지정 · 전원 비활성 뒤 복구) 준다 — 사전 확인하고
  잠금 안에서 다시 센다. 이미 있으면 거절한다. 마이그레이션은 기존 PLATFORM 을 CENTRAL_ADMIN 으로 옮기므로
  그런 DB 에서는 필요 없다. 추가 중앙 관리자 지정 · 강등과 담당 고객사 배정은 중앙 관리자가 직원 관리
  API(/api/v1/staff)에서 한다. 이 CLI 는 담당 고객사를 배정하지 않는다.
- 지정(UNASSIGNED)은 tenant_id NULL 이고 ACTIVE 인 UNASSIGNED 만 받는다. TENANT · 비활성 계정은 거절하며
  복구하지 않는다. 지정하면 account_type=PLATFORM · platform_role=TENANT_MANAGER(role=ADMIN 호환 값,
  배정 없음)로 바꾸고 기존 세션을 ADMIN_REVOKED 로 폐기한다(새 로그인 필요).
- 중앙 관리자 지정(기존 계정)은 ACTIVE PLATFORM 계정만 받는다 — TENANT · UNASSIGNED 는 거절한다(계정 종류 ·
  소속을 바꾸지 않는다). 그 계정의 issuer 가 설정된 Cognito 풀이고, Cognito AdminGetUser(--email)의 sub 가
  계정의 cognito_sub 와 같을 때만 바꾼다. 기존 담당 고객사 배정은 같은 트랜잭션에서 비운다 — 뒤에 담당자로
  내려도 예전 배정이 되살아나지 않는다. 세션은 그대로 둔다 — 세션 판정이 매 요청 현재 등급 · 배정을 읽는다.
- 기존 신원((issuer, sub))이 이미 app_users 에 있으면 만들기는 거절한다 — 고객사 소속을 옮기지 않는다.
- 삭제 · 비활성화 · 강등 경로는 없다. 이 CLI 의 변경은 사용자 관리 이력(user_admin_events)에 남지 않는다
  (행위자 앱 계정이 없다).
- 기존 Cognito 사용자 연결은 운영자가 --existing-cognito-user 로 명시할 때만 한다(자동 연결 없음).
- 임시 비밀번호 · 비밀값 · 토큰은 출력하지 않는다. user_id · 등급 · Cognito 사용자 상태만 출력한다.
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
from schemas.api.auth import AccountType, PlatformRole, UserRole
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth.provider import build_cognito_client

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m auth.bootstrap_admin",
        description=(
            "플랫폼 계정(배정 없는 TENANT_MANAGER)을 만들거나, 기존 UNASSIGNED 계정을 그 등급으로 지정하거나, "
            "첫 중앙 관리자를 지정한다."
        ),
    )
    parser.add_argument("--platform", action="store_true", help="플랫폼 계정 생성을 명시한다(생성에 필수).")
    parser.add_argument(
        "--central-admin",
        action="store_true",
        help="만드는 계정을 중앙 관리자(CENTRAL_ADMIN)로 지정한다. ACTIVE 중앙 관리자가 없을 때만.",
    )
    parser.add_argument("--email")
    parser.add_argument(
        "--designate-unassigned-user-id",
        metavar="USER_ID",
        help=(
            "이미 있는 UNASSIGNED 계정(user_id)을 플랫폼 TENANT_MANAGER(배정 없음)로 지정한다. "
            "Cognito 호출 없음."
        ),
    )
    parser.add_argument(
        "--bootstrap-central-admin-user-id",
        metavar="USER_ID",
        help=(
            "이미 있는 PLATFORM 계정(user_id)을 중앙 관리자로 지정한다. ACTIVE 중앙 관리자가 없을 때만, "
            "--email 의 Cognito sub 가 그 계정의 신원과 같을 때만."
        ),
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


def _user_id(raw: str) -> str | None:
    """정규 UUID 문자열. 형식이 틀리면 None."""
    try:
        return str(uuid.UUID(raw.strip()))
    except ValueError:
        return None


def _cognito(call: Callable[[], dict], say: Callable[[str], None]) -> dict | None:
    """Cognito 호출 1회(트랜잭션 밖). 실패면 오류 코드 · 예외 이름만 출력하고 None — 메시지는 싣지 않는다."""
    try:
        return call()
    except ClientError as exc:
        say(f"실패: Cognito 오류 {exc.response.get('Error', {}).get('Code', 'Unknown')}")
    except BotoCoreError as exc:
        say(f"실패: Cognito 호출 오류 {type(exc).__name__}")
    return None


def _central_admin_blocker(db: Session) -> str | None:
    """CLI 의 중앙 관리자 지정은 ACTIVE 중앙 관리자가 0명일 때만 — 그 밖의 추가 · 변경은 직원 관리 API 몫이다."""
    if auth_repo.count_active_central_admins(db) > 0:
        return (
            "ACTIVE 중앙 관리자가 이미 있다 — 추가 중앙 관리자는 중앙 관리자가 "
            "직원 관리(/api/v1/staff)에서 지정한다."
        )
    return None


def _bootstrap_blocker(
    db: Session, user: models.AppUser | None, *, issuer: str, sub: str | None = None
) -> str | None:
    """기존 계정을 중앙 관리자로 지정할 수 없는 사유. sub 는 Cognito 에서 확인한 값(사전 확인에서는 None)."""
    if user is None:
        return "그 user_id 의 계정이 없다."
    if user.status is not models.AppUserStatus.ACTIVE:
        return "ACTIVE 가 아니다 — 비활성 계정은 복구하지 않고 거절한다."
    if user.account_type is not AccountType.PLATFORM:
        return (
            f"account_type={user.account_type.value} — PLATFORM 계정만 지정한다"
            "(계정 종류 · 소속을 바꾸지 않는다)."
        )
    if user.platform_role is PlatformRole.CENTRAL_ADMIN:
        return "이미 CENTRAL_ADMIN 이다."
    if user.issuer != issuer:
        return "계정의 issuer 가 설정된 Cognito 풀과 다르다."
    if sub is not None and user.cognito_sub != sub:
        return "Cognito 에서 확인한 그 이메일의 sub 가 계정의 신원과 다르다."
    return _central_admin_blocker(db)


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
    user_id = _user_id(args.designate_unassigned_user_id)
    if user_id is None:
        say("거절: user_id 형식이 올바르지 않다.")
        return EXIT_REFUSED

    blocker = _designate_blocker(auth_repo.get_user(db, user_id))
    db.rollback()
    if blocker:
        say(f"거절: {blocker}")
        return EXIT_REFUSED
    say(
        f"대상: user_id={user_id} UNASSIGNED → PLATFORM platform_role=TENANT_MANAGER "
        "(담당 고객사 없음, 기존 세션 폐기)"
    )
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
        row.role = UserRole.ADMIN  # PLATFORM 호환 값 — 권한 근거가 아니다
        row.account_type = AccountType.PLATFORM
        row.platform_role = PlatformRole.TENANT_MANAGER
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
    say(
        f"완료: user_id={user_id} account_type=PLATFORM platform_role=TENANT_MANAGER "
        f"revoked_sessions={revoked} (담당 고객사 없음, 새 로그인 필요)"
    )
    return EXIT_OK


def _bootstrap_central_admin(
    args: argparse.Namespace,
    *,
    db: Session,
    settings: AuthSettings,
    client: Any,
    now: Callable[[], datetime],
    say: Callable[[str], None],
) -> int:
    """기존 PLATFORM 계정 → CENTRAL_ADMIN. DB 사전 확인 → (트랜잭션 밖) Cognito 신원 확인 → 잠금 안 재확인 · 변경."""
    user_id = _user_id(args.bootstrap_central_admin_user_id)
    if user_id is None:
        say("거절: user_id 형식이 올바르지 않다.")
        return EXIT_REFUSED
    email = (args.email or "").strip()
    if not email:
        say("거절: --email 이 필요하다(Cognito 에서 그 계정의 신원을 확인한다).")
        return EXIT_REFUSED

    issuer = _issuer(settings)
    blocker = _bootstrap_blocker(db, auth_repo.get_user(db, user_id), issuer=issuer)
    db.rollback()
    if blocker:
        say(f"거절: {blocker}")
        return EXIT_REFUSED
    say(
        f"대상: user_id={user_id} PLATFORM platform_role → CENTRAL_ADMIN "
        f"(pool={settings.COGNITO_USER_POOL_ID} email={email} 의 sub 로 신원 확인)"
    )
    if not args.yes:
        say("미리보기 — 변경 없음. 적용하려면 --yes 를 붙인다.")
        return EXIT_OK

    resp = _cognito(
        lambda: client.admin_get_user(UserPoolId=settings.COGNITO_USER_POOL_ID, Username=email),
        say,
    )
    if resp is None:
        return EXIT_FAILED
    sub = _sub(resp.get("UserAttributes", []))
    if not sub:
        say("실패: Cognito 응답에 sub 가 없다.")
        return EXIT_FAILED

    try:
        # 잠금 안에서 신원 · 상태 · 중앙 관리자 수를 다시 확인하고 바꾼다
        auth_repo.lock_users_table(db)
        blocker = _bootstrap_blocker(db, auth_repo.get_user(db, user_id), issuer=issuer, sub=sub)
        if blocker:
            db.rollback()
            say(f"거절: {blocker} 변경 없음.")
            return EXIT_REFUSED
        stamp = now()
        auth_repo.set_platform_role(
            db, user_id=user_id, platform_role=PlatformRole.CENTRAL_ADMIN, now=stamp
        )
        # 중앙 관리자는 전체 범위라 배정이 필요 없다 — 남기면 뒤에 담당자로 내릴 때 예전 범위가 되살아난다
        cleared = len(auth_repo.list_assigned_tenant_ids(db, user_id=user_id))
        auth_repo.replace_assigned_tenants(db, user_id=user_id, tenant_ids=(), now=stamp)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        say(f"실패: DB 오류 {type(exc).__name__}. 변경 없음.")
        return EXIT_FAILED
    say(f"완료: user_id={user_id} platform_role=CENTRAL_ADMIN cleared_assignments={cleared}")
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
        if (
            args.email
            or args.platform
            or args.existing_cognito_user
            or args.central_admin
            or args.bootstrap_central_admin_user_id is not None
        ):
            say(
                "거절: --designate-unassigned-user-id 는 --email · --platform · --existing-cognito-user · "
                "--central-admin · --bootstrap-central-admin-user-id 와 함께 쓰지 않는다"
                "(지정 등급은 배정 없는 TENANT_MANAGER)."
            )
            return EXIT_REFUSED
        return _designate(args, db=db, now=now, say=say)

    if args.bootstrap_central_admin_user_id is not None:
        if args.platform or args.existing_cognito_user or args.central_admin:
            say(
                "거절: --bootstrap-central-admin-user-id 는 --email · --yes 만 함께 쓴다"
                "(계정을 만들지 않는다)."
            )
            return EXIT_REFUSED
        return _bootstrap_central_admin(
            args, db=db, settings=settings, client=client, now=now, say=say
        )

    if not args.platform:
        say("거절: 플랫폼 계정을 만들려면 --platform 을 명시한다(ADMIN 이라고 플랫폼으로 추론하지 않는다).")
        return EXIT_REFUSED
    email = (args.email or "").strip()
    if not email:
        say("거절: --email 이 필요하다.")
        return EXIT_REFUSED

    platform_role = PlatformRole.CENTRAL_ADMIN if args.central_admin else PlatformRole.TENANT_MANAGER
    if args.central_admin:
        # Cognito 사용자를 만들기 전에 거절한다(사전 확인 — 잠금 안에서 다시 센다)
        blocker = _central_admin_blocker(db)
        db.rollback()
        if blocker:
            say(f"거절: {blocker}")
            return EXIT_REFUSED

    mode = "기존 Cognito 사용자 연결" if args.existing_cognito_user else "Cognito 사용자 생성(초대 이메일 발송)"
    say(
        f"대상: pool={settings.COGNITO_USER_POOL_ID} email={email} account_type=PLATFORM "
        f"platform_role={platform_role.value}"
    )
    say(f"방식: {mode}")
    if not args.yes:
        say("미리보기 — 변경 없음. 적용하려면 --yes 를 붙인다.")
        return EXIT_OK

    pool = settings.COGNITO_USER_POOL_ID
    if args.existing_cognito_user:
        resp = _cognito(lambda: client.admin_get_user(UserPoolId=pool, Username=email), say)
        if resp is None:
            return EXIT_FAILED
        attributes, status = resp.get("UserAttributes", []), resp.get("UserStatus")
    else:
        resp = _cognito(
            lambda: client.admin_create_user(
                UserPoolId=pool,
                Username=email,
                UserAttributes=[{"Name": "email", "Value": email}],
                DesiredDeliveryMediums=["EMAIL"],
            ),
            say,
        )
        if resp is None:
            say("이미 Cognito 에 있는 사용자면 --existing-cognito-user 로 연결한다.")
            return EXIT_FAILED
        user = resp.get("User", {})
        attributes, status = user.get("Attributes", []), user.get("UserStatus")

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
        blocker = _central_admin_blocker(db) if args.central_admin else None
        if blocker:
            db.rollback()
            say(
                f"거절: 그 사이 바뀌었다. {blocker} 계정은 만들지 않았다 — Cognito 사용자는 남아 있을 수 있다"
                "(TENANT_MANAGER 로 연결하려면 --existing-cognito-user 로 다시 실행한다)."
            )
            return EXIT_REFUSED
        row = auth_repo.create_user(
            db,
            issuer=issuer,
            cognito_sub=sub,
            email=email,
            role=UserRole.ADMIN,  # PLATFORM 호환 값 — 권한 근거가 아니다
            account_type=AccountType.PLATFORM,
            platform_role=platform_role,
            tenant_id=None,
            now=now(),
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        say(f"실패: DB 등록 오류 {type(exc).__name__}. Cognito 사용자는 남아 있을 수 있다 — --existing-cognito-user 로 다시 연결한다.")
        return EXIT_FAILED

    say(f"완료: user_id={row.user_id} platform_role={platform_role.value} cognito_status={status}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    from db.session import get_session_factory

    argv = sys.argv[1:] if argv is None else argv
    settings = get_auth_settings()
    # UNASSIGNED 지정만 Cognito 를 쓰지 않는다 — 자격 증명 없이 실행되고 클라이언트도 만들지 않는다.
    # 중앙 관리자 지정(기존 계정)은 Cognito 로 신원을 확인하므로 생성과 같이 설정이 필요하다
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
