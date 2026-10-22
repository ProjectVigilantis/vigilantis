# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 운영 이미지의 기동 진입점입니다. (ADR-0010)
#
# 하는 일은 둘이다.
#   ① 관제 대상 접근 역할이 주어지면(VIGILANTIS_ACCESS_ROLE_ARN) AWS 설정 파일에 그 역할을
#      AssumeRole하는 프로필을 써 두고 그 프로필을 기본으로 잡는다. 이후 Boto3가 역할 전환과
#      임시 자격증명 갱신을 알아서 한다 — 앱 코드(services/aws/client.py)는 바뀌지 않는다.
#      SaaS(Post-MVP)에서 이 역할이 고객 계정의 역할로 바뀌어도 같은 경로다.
#   ② uvicorn을 **worker 1개 · --reload 없이** 띄운다. 스케줄러·두 디스패처·WebSocket이
#      프로세스 안 메모리에서 돌아 worker를 늘리면 같은 잡이 겹친다(#435 4-2).
#
#   ③ 알림이 켜져 있고 NOTIFICATION_ROUTES_JSON(비밀 아님)이 주어지면 검증한 뒤 홈의
#      .vigilantis/notification-routes.json 파일로 쓰고 NOTIFICATION_ROUTES_FILE을 그 경로로 잡는다.
#      Compose 개발 스택의 명령은 이 파일을 거치지 않는다 — routes 파일을 apps/core-api 바인드 마운트
#      아래에 두고 NOTIFICATION_ROUTES_FILE로 직접 가리킨다.
#
# 셸 스크립트가 아니라 Python인 이유: 작업 트리가 CRLF라 .sh는 컨테이너에서 깨진다.
# compose 개발 스택은 이 파일을 쓰지 않는다(docker-compose.yml이 --reload 명령을 따로 준다).
# ==============================================================================

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PROFILE = "vigilantis-access"


def access_profile_text(role_arn: str, external_id: str, duration: str) -> str:
    lines = [
        f"[profile {_PROFILE}]",
        # ECS 태스크 역할 자격증명으로 역할을 전환한다
        "credential_source = EcsContainer",
        f"role_arn = {role_arn}",
        f"external_id = {external_id}",
        f"duration_seconds = {duration}",
        "role_session_name = vigilantis-platform",
    ]
    return "\n".join(lines) + "\n"


def write_access_profile(environ: dict[str, str], home: Path) -> bool:
    """접근 역할 프로필을 쓰고 환경을 그 프로필로 맞춘다. 역할이 없으면 아무것도 안 한다."""
    role_arn = environ.get("VIGILANTIS_ACCESS_ROLE_ARN", "").strip()
    if not role_arn:
        return False
    external_id = environ.get("VIGILANTIS_ACCESS_EXTERNAL_ID", "").strip()
    if not external_id:
        # ExternalId 없는 AssumeRole은 신뢰 정책이 거절한다 — 기동 단계에서 원인을 밝힌다
        raise SystemExit("VIGILANTIS_ACCESS_ROLE_ARN이 있는데 VIGILANTIS_ACCESS_EXTERNAL_ID가 비어 있다")
    duration = environ.get("VIGILANTIS_ACCESS_SESSION_SECONDS", "900").strip() or "900"
    config = home / ".aws" / "config"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(access_profile_text(role_arn, external_id, duration), encoding="utf-8")
    environ["AWS_CONFIG_FILE"] = str(config)
    environ["AWS_PROFILE"] = _PROFILE
    # 정적 키가 남아 있으면 Boto3가 프로필보다 먼저 집는다 — 운영 태스크에는 없어야 한다
    environ.pop("AWS_ACCESS_KEY_ID", None)
    environ.pop("AWS_SECRET_ACCESS_KEY", None)
    return True


_TRUE = {"1", "true", "t", "yes", "y", "on"}
_ROUTES_REL = Path(".vigilantis") / "notification-routes.json"


def write_notification_routes(environ: dict[str, str], home: Path) -> bool:
    """NOTIFICATION_ROUTES_JSON(비밀 아님)을 실제 파일로 쓰고 NOTIFICATION_ROUTES_FILE을 맞춘다.

    알림이 꺼져 있거나 JSON이 없으면 아무것도 하지 않는다(꺼진 기동은 설정을 읽지 않는다).
    JSON과 파일 경로가 함께 있으면 어느 쪽이 맞는지 알 수 없어 멈춘다. 저장 전에 런타임과 같은
    RouteRegistry 스키마로 검증한다 — 모르는 키(webhook 값이 들어온 경우 포함)는 저장하지 않는다.
    오류 문구는 고정이며 JSON 내용을 싣지 않는다.
    """
    if environ.get("NOTIFICATION_ENABLED", "").strip().lower() not in _TRUE:
        return False
    raw = environ.get("NOTIFICATION_ROUTES_JSON", "")
    if not raw.strip():
        return False
    if environ.get("NOTIFICATION_ROUTES_FILE", "").strip():
        raise SystemExit("NOTIFICATION_ROUTES_JSON and NOTIFICATION_ROUTES_FILE are both set")
    from notifications.routing import MAX_CONFIG_BYTES, RouteConfigError, RouteRegistry, _no_duplicate_keys

    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
        RouteRegistry.from_mapping(data, {})
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise RouteConfigError("too large")
    except (ValueError, RecursionError):  # RouteConfigError는 ValueError다
        raise SystemExit("NOTIFICATION_ROUTES_JSON is invalid") from None
    path = home / _ROUTES_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    environ["NOTIFICATION_ROUTES_FILE"] = str(path)
    environ.pop("NOTIFICATION_ROUTES_JSON", None)
    return True


def uvicorn_argv(port: str) -> list[str]:
    return ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", port, "--workers", "1"]


def main() -> None:
    home = Path(os.environ.get("HOME", "/root"))
    write_access_profile(os.environ, home)
    write_notification_routes(os.environ, home)
    argv = uvicorn_argv(os.environ.get("PORT", "8000"))
    os.execvp(sys.executable, [sys.executable, "-m", *argv])


if __name__ == "__main__":
    main()
