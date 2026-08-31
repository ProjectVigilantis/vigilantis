"""executor.execute_rightsizing() LocalStack 통합 테스트 (Issue #211, ADR-0006).

단계 분기 전수는 test_execute_rightsizing.py가 맡고, 여기서는 **실물에서 실제로
타입이 바뀌는가**만 본다. 에뮬레이터가 호출을 받아 놓고 아무것도 바꾸지 않는
경우가 실제로 있었기 때문이다(ADR-0006 §4 — create_network_acl_entry). 조용히
통과하면 게이트 시연에서 처음 발견된다.

로컬 실행 전제: LocalStack 기동 + scripts/seed_localstack.py 완료. 미기동 시 전체 skip.
"""

import os
import sys
import urllib.request
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
os.environ.setdefault("AWS_ENDPOINT_URL", ENDPOINT)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws.client import account_id, aws_client, default_region  # noqa: E402

S = ExecutionStepStatus
E = ExecutionEffect


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)


def _instance_state(ec2, instance_id: str) -> tuple[str, str]:
    """(인스턴스 타입, 상태). 실행 전후를 같은 방법으로 읽는다."""
    found = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    return found["InstanceType"], found["State"]["Name"]


@pytest.fixture
def running_instance():
    """시드 인스턴스 1대를 빌려 쓰고, 타입·상태를 원래대로 되돌려 놓는다.

    같은 자산을 test_precheck_localstack.py가 running 전제로 쓰므로 복원은 선택이
    아니다 — 실행 순서에 따라 그쪽이 깨진다.
    """
    ec2 = aws_client("ec2")
    instance = next(
        i
        for r in ec2.describe_instances()["Reservations"]
        for i in r["Instances"]
        if i["State"]["Name"] == "running"
    )
    instance_id = instance["InstanceId"]
    original_type = instance["InstanceType"]
    arn = f"arn:aws:ec2:{default_region()}:{account_id()}:instance/{instance_id}"
    yield {"ec2": ec2, "id": instance_id, "arn": arn, "type": original_type}

    current_type, current_state = _instance_state(ec2, instance_id)
    if current_type != original_type:
        ex.execute_rightsizing(arn, target_instance_type=original_type)
    elif current_state != "running":
        ec2.start_instances(InstanceIds=[instance_id])
        ec2.get_waiter("instance_running").wait(
            InstanceIds=[instance_id], WaiterConfig={"Delay": 2, "MaxAttempts": 30}
        )


def test_rightsizing_actually_changes_the_instance_type(running_instance):
    """호출이 받아들여지는 것과 자산이 바뀌는 것은 다른 이야기다."""
    fx = running_instance
    target_type = "t3.small" if fx["type"] != "t3.small" else "t3.micro"

    outcome = ex.execute_rightsizing(fx["arn"], target_instance_type=target_type)

    assert outcome.succeeded, outcome.error_summary
    changed_type, state = _instance_state(fx["ec2"], fx["id"])
    assert changed_type == target_type
    # 기동 요청까지가 이 함수의 경계다 — 2/2 Status Check 확인은 rollback.py 몫
    assert state in {"pending", "running"}


def test_every_step_is_recorded_with_its_effect(running_instance):
    """자동 원복의 입력이 실물 경로에서도 그대로 만들어지는지 본다."""
    fx = running_instance
    target_type = "t3.small" if fx["type"] != "t3.small" else "t3.micro"
    recorded = []

    outcome = ex.execute_rightsizing(
        fx["arn"], target_instance_type=target_type, record_step=recorded.append
    )

    assert [(s.sequence, s.step_type, s.status, s.effect) for s in outcome.steps] == [
        (1, ex.STEP_STOP_INSTANCE, S.SUCCESS, E.APPLIED),
        (2, ex.STEP_MODIFY_INSTANCE_TYPE, S.SUCCESS, E.APPLIED),
        (3, ex.STEP_START_INSTANCE, S.SUCCESS, E.APPLIED),
    ]
    # 호출 직전 IN_PROGRESS가 먼저 남는다 — 프로세스가 죽어도 진행 지점이 남는 근거
    assert [(s.sequence, s.status) for s in recorded][:2] == [(1, S.IN_PROGRESS), (1, S.SUCCESS)]


def test_missing_instance_is_reported_not_raised(running_instance):
    """실행 경로는 예외를 던지지 않는다 — 없는 대상도 판정으로 돌아온다."""
    arn = f"arn:aws:ec2:{default_region()}:{account_id()}:instance/i-0000000000000dead"

    outcome = ex.execute_rightsizing(arn, target_instance_type="t3.micro")

    assert not outcome.succeeded
    assert outcome.reason_code is not None
    assert outcome.steps[0].status is S.FAILED
