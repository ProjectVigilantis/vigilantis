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
# 셸 스크립트가 아니라 Python인 이유: 작업 트리가 CRLF라 .sh는 컨테이너에서 깨진다.
# compose 개발 스택은 이 파일을 쓰지 않는다(docker-compose.yml이 --reload 명령을 따로 준다).
# ==============================================================================

from __future__ import annotations

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


def uvicorn_argv(port: str) -> list[str]:
    return ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", port, "--workers", "1"]


def main() -> None:
    write_access_profile(os.environ, Path(os.environ.get("HOME", "/root")))
    argv = uvicorn_argv(os.environ.get("PORT", "8000"))
    os.execvp(sys.executable, [sys.executable, "-m", *argv])


if __name__ == "__main__":
    main()
