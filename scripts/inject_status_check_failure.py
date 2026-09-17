# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 시연용 Status Check 실패 주입 — T1 7번(자동 원복)을 LocalStack에서 일으킨다. (#349)
#
# 실행 (repo 루트 · 앱 기동 후 · 화면에서 [조치 실행]을 누르기 **전에** 띄워 둔다):
#   PowerShell: $env:AWS_ENDPOINT_URL='http://localhost:4566'; uv run python scripts/inject_status_check_failure.py
#   bash      : AWS_ENDPOINT_URL=http://localhost:4566 uv run python scripts/inject_status_check_failure.py
#   대상 지정 : ... --name <Name 태그>   (기본: 시드의 유일한 다운사이징 후보 vigilantis-seed-idle-dev)
#
# 왜 스크립트인가 — 주입에는 창이 있다(ADR-0006 §4 2행 6차 개정). 다운사이징 실행 주기가 기동
# 요청까지 하고 끝난 뒤, **다음 실행 주기가 2/2 Status Check를 묻기 전**에 인스턴스를 멈춰야
# 판정이 FAILED로 떨어진다. 놓치면 첫 조회가 OK로 끝나 T1이 SUCCESS로 닫히고 원복 장면이 없다.
# 창의 길이는 DISPATCH_INTERVAL_SECONDS에서 실행 주기 소요를 뺀 만큼이고(2026-09-17 앱 기동
# 실측: 10초 주기 약 9.5초 · 2초 주기 약 1.7초), 화면에는 "실행 주기가 끝났다"는 신호가 없어
# 사람이 눈으로 잡을 수 없다. 이 스크립트는 0.1초 간격으로 보고 있다가 그 순간 멈춘다
# (같은 실측 3회에서 기동 확인 → 정지 요청 0.05초).
#
# 무엇을 기다리나 — 인스턴스 유형이 **시작 시점과 달라진 채** pending/running이 되는 순간.
# 실행기(services/aws/executor.py execute_rightsizing)의 마지막 AWS 호출이 start_instances라,
# 그 뒤로는 실행 주기가 AWS를 더 부르지 않는다 — 그 순간 멈춰도 실행 결과와 겹치지 않는다.
#   · 유형 그대로 · running          → 아직 실행 전(대기)
#   · 유형 바뀜 · stopped             → 정지와 기동 사이(대기)
#   · 유형 바뀜 · pending/running     → 기동 요청이 끝났다 → 즉시 stop_instances
# 한 번 주입하면 끝난다. 뒤의 판정·원복은 전부 시스템이 한다.
#
# 안전 가드(ADR-0006 §2): 실 AWS에는 실행되지 않는다 — seed_localstack.py 와 같은 조건이다.
# 프로덕션 코드에는 데모 분기가 없다 — 가짜 AWS의 상태만 바꾼다(ADR-0006 §3).
# ==============================================================================

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Windows 콘솔(cp949)은 em dash 등 출력 시 UnicodeEncodeError로 죽는다 — UTF-8로 강제
sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT / "apps" / "core-api"), str(_REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 엔드포인트·리전·자격증명 해석의 단일 원천(ADR-0006 §3)
from services.aws.client import aws_client, endpoint_url  # noqa: E402

DEFAULT_NAME = "vigilantis-seed-idle-dev"
STARTED_STATES = frozenset({"pending", "running"})
POLL_SECONDS = 0.1
TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class Injection:
    instance_id: str
    start_type: str
    applied_type: str
    elapsed_seconds: float  # 대기 시작부터 정지 요청까지


def _require_localstack() -> str:
    """실 AWS 실행 거부(ADR-0006 §2). 엔드포인트는 클라이언트 팩토리의 해석을 그대로 쓴다."""
    endpoint = endpoint_url()
    if not endpoint:
        sys.exit(
            "AWS_ENDPOINT_URL 미설정 — 이 스크립트는 LocalStack 전용이다.\n"
            "  예) AWS_ENDPOINT_URL=http://localhost:4566"
        )
    if "amazonaws.com" in endpoint:
        sys.exit(f"실 AWS 엔드포인트({endpoint})에는 실패를 주입하지 않는다.")
    return endpoint


def _seed_instance_types() -> dict[str, str]:
    """시드가 만드는 인스턴스의 원래 유형 — 원천은 seed_localstack.INSTANCES 하나다."""
    path = _REPO_ROOT / "scripts" / "seed_localstack.py"
    spec = importlib.util.spec_from_file_location("seed_localstack", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name: instance_type for name, instance_type, *_ in module.INSTANCES}


def find_instance(ec2, name: str) -> dict:
    """Name 태그로 종료되지 않은 인스턴스를 정확히 한 대 찾는다."""
    found = [
        instance
        for reservation in ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [name]},
                {"Name": "instance-state-name",
                 "Values": ["pending", "running", "stopping", "stopped"]},
            ]
        )["Reservations"]
        for instance in reservation["Instances"]
    ]
    if len(found) != 1:
        ids = ", ".join(i["InstanceId"] for i in found) or "없음"
        sys.exit(
            f"중단: {name} 인스턴스가 정확히 한 대가 아니다({ids}).\n"
            "  `docker compose restart localstack` 후 `uv run python scripts/seed_localstack.py`"
        )
    return found[0]


def _current(ec2, instance_id: str) -> tuple[str, str]:
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    return instance["InstanceType"], instance["State"]["Name"]


def wait_and_stop(
    ec2,
    instance_id: str,
    start_type: str,
    *,
    poll_seconds: float = POLL_SECONDS,
    timeout_seconds: float = TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_change: Optional[Callable[[float, str, str], None]] = None,
) -> Optional[Injection]:
    """유형이 바뀐 채 기동되는 순간 정지를 요청한다. 시간 안에 그 순간이 없으면 None."""
    started = clock()
    last = None
    while clock() - started < timeout_seconds:
        current = _current(ec2, instance_id)
        if current != last and on_change is not None:
            on_change(clock() - started, *current)
        last = current
        instance_type, state = current
        if instance_type != start_type and state in STARTED_STATES:
            ec2.stop_instances(InstanceIds=[instance_id])
            return Injection(instance_id, start_type, instance_type, clock() - started)
        sleep(poll_seconds)
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="시연용 Status Check 실패 주입 (LocalStack 전용)")
    parser.add_argument("--name", default=DEFAULT_NAME, help=f"대상 Name 태그 (기본 {DEFAULT_NAME})")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS,
                        help=f"[조치 실행]을 기다릴 최대 시간(초, 기본 {TIMEOUT_SECONDS:g})")
    args = parser.parse_args(argv)

    _require_localstack()
    ec2 = aws_client("ec2")
    instance = find_instance(ec2, args.name)
    instance_id = instance["InstanceId"]
    start_type, state = instance["InstanceType"], instance["State"]["Name"]

    if state != "running":
        sys.exit(
            f"중단: {args.name}({instance_id})가 running이 아니다({state}) — 실행기는 멈춰 있던 "
            "인스턴스를 다시 켜지 않으므로 주입할 순간이 오지 않는다.\n"
            "  `docker compose restart localstack` 후 시드를 다시 돌릴 것"
        )
    seed_type = _seed_instance_types().get(args.name)
    if seed_type is not None and start_type != seed_type:
        sys.exit(
            f"중단: {args.name}의 유형이 시드와 다르다(현재 {start_type} != 시드 {seed_type}) — "
            "이전 시연에서 이미 바뀐 상태다. 이대로 기다리면 변화를 알아보지 못한다.\n"
            "  `docker compose restart localstack` 후 시드를 다시 돌릴 것"
        )

    print(f"대상: {args.name} ({instance_id}) · 현재 {start_type} {state}")
    print(f"대기 중 — 화면에서 [조치 실행]을 누르세요 (최대 {args.timeout:g}초)", flush=True)

    def show(elapsed: float, instance_type: str, current_state: str) -> None:
        print(f"  +{elapsed:6.2f}s  {instance_type} {current_state}", flush=True)

    injection = wait_and_stop(
        ec2, instance_id, start_type, timeout_seconds=args.timeout, on_change=show
    )
    if injection is None:
        print(f"시간 초과 — {args.timeout:g}초 안에 다운사이징 기동이 보이지 않았다. 주입하지 않았다.")
        return 1
    print(
        f"주입: stop_instances (+{injection.elapsed_seconds:.2f}s · "
        f"{injection.start_type} → {injection.applied_type} 기동 직후)"
    )
    print("→ 다음 실행 주기의 2/2 판정이 FAILED로 떨어지고 자동 원복이 이어진다. 이 창은 닫아도 된다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
