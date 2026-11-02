"""TOTP 분실 복구 운영 CLI 어댑터 (#515) — 앱 세션이 없는 운영자의 비상 경로.

    cd apps/core-api
    # 미리보기(기본) — 읽기 전용. 대상 판정 + 작업 상태만 보여 준다. Cognito 클라이언트를 만들지 않는다.
    uv run python -m auth.recover_totp --user-id <UUID> [--operation-id <UUID>] [--no-admin-available]
    # 실행 — 새 작업은 새 operation-id 로, 이전 요청의 조회 · 재생은 같은 operation-id 로
    uv run python -m auth.recover_totp --user-id <UUID> --operation-id <UUID> \\
        --identity-verified true --yes [--no-admin-available]

인증과 권한:
  - 이 CLI 는 앱 엔드포인트 · 앱 역할이 아니다. 운영 환경에서 CLI 를 실행하고 DB 에 쓰는 권한으로 접근을 통제한다.
    초기화 실행에는 Cognito 자격 증명(AWS IAM)도 필요하다. 행위자 앱 계정이 없어 작업의 actor_user_id 는 NULL 이다.
  - 초기화 대상: ACTIVE PLATFORM(모든 platform_role) 은 허용. ACTIVE TENANT ADMIN 은 --no-admin-available 이 있을 때만
    허용한다 — 이 플래그는 "사용 가능한 관리자 · 중앙 관리자 경로가 없다"는 운영자의 진술이지 기술적 증명이
    아니다(DB 의 ACTIVE 수는 로그인 가능성을 보장하지 않는다). 그 밖(UNASSIGNED · 비활성 · 비ADMIN 테넌트
    사용자)은 거절한다. 계정 종류 · 역할 · 상태 · 비밀번호를 바꾸지 않는다.
  - 이 CLI 는 계정을 만들거나 지정하지 않는다(그것은 auth.bootstrap_admin).

실행 요건: --user-id(UUID) · --operation-id(UUID) · --identity-verified true · --yes. 하나라도 없으면 아무것도 하지 않는다.
  - operation-id 는 자동 생성하지 않고 자동 재시도도 없다. 같은 operation-id 는 이전 요청을 조회 · 재생할 뿐
    제공자를 다시 부르지 않는다. 다른 operation-id 는 명시적인 새 초기화이며 그 사이 새로 등록한 TOTP 를 지울 수 있다.

정리 모드(풀리지 않은 제공자 변경 울타리 · 초기화가 아니다):
    uv run python -m auth.recover_totp --user-id <UUID> --reconcile-mutation <MUTATION_UUID>  # 미리보기
    uv run python -m auth.recover_totp --user-id <UUID> --reconcile-mutation <MUTATION_UUID> \\
        --identity-verified true --external-state-verified true --yes
  - 정리 대상: ACTIVE PLATFORM · ACTIVE TENANT 의 모든 역할. --no-admin-available 은 필요 없다.
    비활성 · UNASSIGNED · 앱 계정 없는 신원은 거절한다. 초기화 대상 정책을 넓히는 것이 아니다.
  - --operation-id 와 함께 쓸 수 없다. Cognito 클라이언트를 만들지 않는다. 울타리 표식만 해소하며 TOTP 초기화는
    완료되지 않는다(작업 상태 · 사용자 · 세션 불변). 이후 새 초기화는 외부 검토 뒤 새 operation-id 로 따로 요청한다.
  - 외부 확인 = 원래 제공자 호출이 끝났거나 더는 적용될 수 없음을 앱 밖에서 확인했다는 운영자 진술이다. 시간 · DB 건수 ·
    MFA 메타데이터는 증명이 아니며, DB 권한 운영자의 진술일 뿐 앱이 AWS 를 검증했다는 증거가 아니다.
  - PROVIDER_PENDING · PROVIDER_UNKNOWN · BUSY 는 새 operation-id 로 풀리지 않는다.

종료 코드: 0 = 읽기 전용 미리보기 또는 COMPLETED 기록 / 2 = 호출 · 정책 거절(아무것도 바꾸지 않음) /
1 = 제공자 · 시스템 실패 또는 미완료 결과(차단은 남을 수 있다). 제공자 원문 · 자격 증명 · 이메일 · SQL 은 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

_CORE_API = Path(__file__).resolve().parents[1]
for _p in (str(_CORE_API), str(_CORE_API.parents[1] / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import get_auth_settings
from db import models
from db.models import TotpRecoveryStatus as Status
from db.repositories import auth as auth_repo
from schemas.api.auth import AccountType, UserRole
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from auth import totp_recovery
from auth.cognito import CognitoAdapter
from auth.totp_recovery import RecoveryRejection, TotpRecoveryRejected

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

_REJECTION_TEXT = {
    RecoveryRejection.IDENTITY_NOT_CONFIRMED: "본인 확인 진술이 없다.",
    RecoveryRejection.TARGET_NOT_FOUND: "그 user_id 의 계정이 없다.",
    RecoveryRejection.TARGET_INELIGIBLE: "복구 대상이 아니다(UNASSIGNED 또는 비활성).",
    RecoveryRejection.SELF_TARGET: "행위자 자신은 대상이 될 수 없다.",
    RecoveryRejection.OPERATION_CONFLICT: "그 operation-id 는 다른 대상의 작업이다.",
    RecoveryRejection.PROVIDER_IDENTITY_MISMATCH: "대상의 issuer 가 설정된 Cognito 풀과 다르다.",
    RecoveryRejection.EXTERNAL_STATE_NOT_CONFIRMED: "외부 상태 확인 진술이 없다.",
    RecoveryRejection.MUTATION_NOT_FOUND: (
        "그 mutation-id 는 이 대상의 풀리지 않은 울타리가 아니다(없거나 이미 풀렸다)."
    ),
}

_BUSY_TEXT = (
    "실패(시스템): 같은 신원에 풀리지 않은 제공자 변경 울타리가 있다(진행 중이거나 결과가 기록되지 않았다). "
    "변경 없음. 새 operation-id 로 해소되지 않고 시간이 지나도 풀리지 않는다 — 미리보기로 울타리를 확인하고, "
    "외부에서 원래 제공자 호출이 끝났거나 더는 적용될 수 없음을 확인한 뒤 --reconcile-mutation 으로 정리한다."
)

_ATTESTATION_NOTE = (
    "외부 확인이란 원래 제공자 호출이 끝났거나 더는 적용될 수 없음을 운영자가 앱 밖에서 확인했다는 뜻이다. "
    "시간 경과 · DB 건수 · MFA 메타데이터는 증명이 아니다. 이 진술은 DB 권한을 가진 운영자의 기록일 뿐 "
    "앱이 AWS 상태를 검증했다는 증거가 아니다."
)

_STATUS_TEXT = {
    Status.COMPLETED: "완료: 제공자의 TOTP 삭제가 확인되어 기록됐다. 대상은 새 TOTP 를 등록해야 로그인한다.",
    Status.PROVIDER_NOT_FOUND: (
        "미완료: 제공자에 등록이 없다고 응답했다(이 작업이 삭제했다는 증명이 아니다). "
        "대상은 새 MFA 설정만 할 수 있다 — 복구 완료로 보고하지 않는다."
    ),
    Status.PROVIDER_REJECTED: "실패: 제공자가 삭제를 거절했다. 삭제되지 않았다. 대상의 로그인은 차단된 채다.",
    Status.PROVIDER_UNKNOWN: (
        "실패: 제공자 결과를 알 수 없다(삭제됐을 수 있다). 미해결 — 새 operation-id 로 해소되지 않는다. "
        "대상의 로그인은 차단된 채다."
    ),
    Status.PROVIDER_PENDING: (
        "미완료: 결과가 기록되지 않았다(진행 중 · 중단 · 기록 실패). 미해결 — 새 operation-id 로 해소되지 않는다. "
        "대상의 로그인은 차단된 채다."
    ),
}


class PolicyDenied(Exception):
    """운영자 대상 정책 거절. 메시지는 고정 문구다."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m auth.recover_totp",
        description="TOTP 분실 복구(운영자 비상 경로). 기본은 읽기 전용 미리보기다.",
    )
    parser.add_argument("--user-id", metavar="UUID", help="복구 대상 user_id.")
    parser.add_argument(
        "--operation-id",
        metavar="UUID",
        help="복구 작업 ID. 실행에 필수. 같은 ID = 이전 요청 조회 · 재생, 다른 ID = 명시적 새 초기화.",
    )
    parser.add_argument(
        "--identity-verified",
        nargs="?",
        const="true",
        metavar="true",
        help="운영자가 외부 절차로 본인 확인을 마쳤다는 진술(true). 실행에 필수.",
    )
    parser.add_argument(
        "--no-admin-available",
        action="store_true",
        help=(
            "운영자 진술: 사용 가능한 관리자 · 중앙 관리자 경로가 없다. 기술적 증명이 아니다. "
            "ACTIVE TENANT ADMIN 초기화에만 필요하다. 표식 정리에는 필요 없다."
        ),
    )
    parser.add_argument(
        "--reconcile-mutation",
        metavar="UUID",
        help=(
            "정리 모드: 대상 신원의 풀리지 않은 울타리(mutation-id)를 운영자 진술로 정리한다. "
            "초기화가 아니다 — --operation-id 와 함께 쓸 수 없다."
        ),
    )
    parser.add_argument(
        "--external-state-verified",
        nargs="?",
        const="true",
        metavar="true",
        help=(
            "운영자 진술(true): 원래 제공자 호출이 끝났거나 더는 적용될 수 없음을 앱 밖에서 확인했다. "
            "정리 실행에 필수. 증명이 아니다."
        ),
    )
    parser.add_argument("--yes", action="store_true", help="실제로 실행한다(기본은 미리보기).")
    return parser


def _mutation_line(mutation: totp_recovery.ProviderMutation) -> str:
    return (
        f"울타리: mutation_id={mutation.mutation_id} kind={mutation.kind.value} "
        f"started_at={mutation.started_at.isoformat()}"
    )


def _show_mutation(db: Session, user_id: str, say: Callable[[str], None]) -> None:
    mutation = totp_recovery.get_unresolved_provider_mutation(db, user_id)
    say(_mutation_line(mutation) if mutation else "울타리: 풀리지 않은 울타리 없음.")


def _reconcile_preview(
    engine: Engine, *, user_id: str, mutation_id: str, authorize, say: Callable[[str], None]
) -> int:
    with Session(engine) as db:
        target = auth_repo.get_user(db, user_id)
        authorize(db, target)
        if target is None:
            raise TotpRecoveryRejected(RecoveryRejection.TARGET_NOT_FOUND)
        say(_describe(target))
        mutation = totp_recovery.get_unresolved_provider_mutation(db, user_id)
        if mutation is None:
            say("울타리: 풀리지 않은 울타리 없음 — 정리할 것이 없다.")
        else:
            say(_mutation_line(mutation))
        if mutation is None or str(mutation.mutation_id) != mutation_id:
            raise TotpRecoveryRejected(RecoveryRejection.MUTATION_NOT_FOUND)
        db.rollback()
    say(_ATTESTATION_NOTE)
    say(
        "정리는 울타리 표식만 해소한다. TOTP 초기화는 완료되지 않으며 작업 상태 · 사용자 · 세션은 바뀌지 않는다. "
        "새 초기화는 외부 검토 뒤 새 operation-id 로 따로 요청한다."
    )
    say(
        "미리보기 — 변경 없음. 정리하려면 --identity-verified true · --external-state-verified true · --yes 를 붙인다."
    )
    return EXIT_OK


def _uuid(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return str(uuid.UUID(raw.strip()))
    except ValueError:
        return None


def _authorize(no_admin_available: bool, *, reconcile: bool = False) -> totp_recovery.Authorize:
    """운영자 정책. 대상 없음은 core 가 거절한다. 앱 행위자가 없으므로 None 을 돌려준다."""

    def authorize(db: Session, target: models.AppUser | None) -> str | None:
        if target is None:
            return None
        if (
            target.status is not models.AppUserStatus.ACTIVE
            or target.account_type not in (AccountType.PLATFORM, AccountType.TENANT)
        ):
            raise PolicyDenied("복구 대상이 아니다(UNASSIGNED 또는 비활성).")
        if target.account_type is AccountType.TENANT and not reconcile:
            if target.role is not UserRole.ADMIN:
                raise PolicyDenied(
                    "일반 테넌트 사용자는 운영자 CLI 대상이 아니다 — 앱의 관리자 경로로 복구한다."
                )
            if not no_admin_available:
                raise PolicyDenied(
                    "테넌트 ADMIN 은 사용 가능한 관리자 · 중앙 관리자 경로가 없다는 운영자 진술"
                    "(--no-admin-available)이 있을 때만 복구한다."
                )
        return None

    return authorize


def _describe(target: models.AppUser) -> str:
    platform_role = target.platform_role.value if target.platform_role else "-"
    return (
        f"대상: user_id={target.user_id} account_type={target.account_type.value} "
        f"role={target.role.value} platform_role={platform_role} status={target.status.value}"
    )


def _operation_line(op: totp_recovery.RecoveryOperation) -> str:
    return (
        f"작업: operation_id={op.operation_id} status={op.status.value} "
        f"revoked_sessions={op.revoked_session_count}"
    )


def _preview(
    engine: Engine, *, user_id: str, operation_id: str | None, authorize, say: Callable[[str], None]
) -> int:
    """읽기 전용 — 대상 정책을 먼저 통과해야 작업 상태를 보여 준다(조회 헬퍼는 권한 판정이 없다)."""
    with Session(engine) as db:
        target = auth_repo.get_user(db, user_id)
        authorize(db, target)
        if target is None:
            raise TotpRecoveryRejected(RecoveryRejection.TARGET_NOT_FOUND)
        say(_describe(target))
        if operation_id is not None:
            op = totp_recovery.get_recovery_operation(db, uuid.UUID(operation_id))
            if op is None:
                say("작업: 그 operation-id 의 이전 요청 없음 — 실행하면 새 초기화다.")
            elif op.target_user_id != target.user_id:
                raise TotpRecoveryRejected(RecoveryRejection.OPERATION_CONFLICT)
            else:
                say(_operation_line(op))
        else:
            ops = totp_recovery.list_recovery_operations(db, user_id)
            say(f"이전 작업: {len(ops)}건" + (f", 마지막 status={ops[-1].status.value}" if ops else ""))
        _show_mutation(db, user_id, say)
        db.rollback()
    say(
        "같은 operation-id 는 이전 요청 조회 · 재생이다. 다른 operation-id 는 명시적인 새 초기화이며 그 사이 "
        "새로 등록한 TOTP 를 지울 수 있다 — 단 미해결(PROVIDER_PENDING · PROVIDER_UNKNOWN) 작업을 해소하지 않는다."
    )
    say(
        "미리보기 — 변경 없음. 실행하려면 --operation-id · --identity-verified true · --yes 를 붙인다."
    )
    return EXIT_OK


def run(
    argv: list[str],
    *,
    engine: Engine,
    get_adapter: Callable[[], CognitoAdapter],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    out: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)

    def say(message: str) -> None:
        print(message, file=sys.stdout if out is None else out)

    user_id = _uuid(args.user_id)
    if user_id is None:
        say("거절(호출): --user-id 는 UUID 여야 한다.")
        return EXIT_REFUSED
    operation_id = _uuid(args.operation_id)
    if args.operation_id is not None and operation_id is None:
        say("거절(호출): --operation-id 는 UUID 여야 한다.")
        return EXIT_REFUSED
    if args.identity_verified is not None and args.identity_verified.lower() != "true":
        say("거절(호출): --identity-verified 는 true 만 받는다.")
        return EXIT_REFUSED
    verified = args.identity_verified is not None
    reconcile_id = _uuid(args.reconcile_mutation)
    if args.reconcile_mutation is not None and reconcile_id is None:
        say("거절(호출): --reconcile-mutation 은 UUID 여야 한다.")
        return EXIT_REFUSED
    if args.external_state_verified is not None and args.external_state_verified.lower() != "true":
        say("거절(호출): --external-state-verified 는 true 만 받는다.")
        return EXIT_REFUSED
    attested = args.external_state_verified is not None
    if reconcile_id is not None:
        if args.operation_id is not None:
            say("거절(호출): 정리 모드(--reconcile-mutation)는 --operation-id 와 함께 쓸 수 없다.")
            return EXIT_REFUSED
        if args.yes and not verified:
            say("거절(호출): 정리 실행에는 --identity-verified true 가 필요하다.")
            return EXIT_REFUSED
        if args.yes and not attested:
            say("거절(호출): 정리 실행에는 --external-state-verified true 가 필요하다.")
            return EXIT_REFUSED
    elif attested:
        say("거절(호출): --external-state-verified 는 --reconcile-mutation 과만 쓴다.")
        return EXIT_REFUSED

    if args.yes and reconcile_id is None:
        if operation_id is None:
            say("거절(호출): 실행에는 --operation-id 가 필요하다(자동 생성하지 않는다).")
            return EXIT_REFUSED
        if not verified:
            say("거절(호출): 실행에는 --identity-verified true 가 필요하다.")
            return EXIT_REFUSED

    authorize = _authorize(args.no_admin_available, reconcile=reconcile_id is not None)
    try:
        if reconcile_id is not None:
            if not args.yes:
                return _reconcile_preview(
                    engine, user_id=user_id, mutation_id=reconcile_id, authorize=authorize, say=say
                )
            say(f"요청: 정리 user_id={user_id} mutation_id={reconcile_id}")
            done = totp_recovery.reconcile_provider_mutation(
                engine,
                target_user_id=user_id,
                mutation_id=uuid.UUID(reconcile_id),
                external_state_verified=attested,
                authorize=authorize,
                clock=now,
            )
            say(_mutation_line(done) + f" resolution={done.resolution.value}")
            say(
                "정리됨: 울타리 표식만 해소했다(운영자 진술). TOTP 초기화는 완료되지 않았다 — 작업 상태 · 사용자 · "
                "세션은 바뀌지 않았고 Cognito 를 부르지 않았다. 새 초기화는 외부 검토 뒤 새 operation-id 로 따로 요청한다."
            )
            say(_ATTESTATION_NOTE)
            return EXIT_OK
        if not args.yes:
            return _preview(
                engine, user_id=user_id, operation_id=operation_id, authorize=authorize, say=say
            )
        assert operation_id is not None
        say(f"요청: user_id={user_id} operation_id={operation_id}")
        result = totp_recovery.reset_totp(
            engine,
            operation_id=uuid.UUID(operation_id),
            target_user_id=user_id,
            identity_verified=verified,
            authorize=authorize,
            get_adapter=get_adapter,
            clock=now,
        )
    except PolicyDenied as exc:
        say(f"거절(정책): {exc} 변경 없음.")
        return EXIT_REFUSED
    except TotpRecoveryRejected as exc:
        if exc.reason is RecoveryRejection.BUSY:
            say(_BUSY_TEXT)
            return EXIT_FAILED
        say(f"거절(정책): {_REJECTION_TEXT[exc.reason]} 변경 없음.")
        return EXIT_REFUSED
    except SQLAlchemyError as exc:
        if reconcile_id is not None:
            guide = "같은 --user-id · --reconcile-mutation 으로 미리보기해 울타리 상태를 확인한다"
        else:
            guide = "같은 operation-id 로 미리보기해 확인한다"
        say(f"실패(시스템): DB 오류 {type(exc).__name__}. 결과가 기록되었는지 알 수 없다 — {guide}.")
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - 원문은 싣지 않는다(자격 증명 · 제공자 메시지)
        say(f"실패(시스템): 제공자 클라이언트 또는 내부 오류 {type(exc).__name__}. 변경 없음.")
        return EXIT_FAILED

    if result.replayed:
        say("재생: 이전 요청의 결과다 — 이번 호출은 제공자를 부르지 않았다.")
    say(_operation_line(result.operation))
    say(_STATUS_TEXT[result.operation.status])
    return EXIT_OK if result.completed else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = _parser().parse_args(argv)  # 사용법 오류는 argparse 가 직접 종료한다
    try:
        from db.session import get_engine

        from auth.provider import build_recovery_adapter

        settings = get_auth_settings()
        missing = settings.missing_cognito_keys() if args.yes and args.reconcile_mutation is None else []
        engine = get_engine()
    except Exception:  # noqa: BLE001 - 검증 입력 · URL · 자격 증명이 실리므로 원문 · 예외 이름 모두 싣지 않는다
        print("실패(시스템): 설정 또는 DB 엔진을 초기화하지 못했다. 환경 설정을 확인한다. 변경 없음.", file=sys.stderr)
        return EXIT_FAILED
    if missing:
        print(f"실패(시스템): Cognito 설정 누락 키: {', '.join(missing)}", file=sys.stderr)
        return EXIT_FAILED
    return run(argv, engine=engine, get_adapter=lambda: build_recovery_adapter(settings))


if __name__ == "__main__":
    raise SystemExit(main())
