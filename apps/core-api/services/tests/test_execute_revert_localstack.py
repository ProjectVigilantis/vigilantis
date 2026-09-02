"""executor.execute_revert_size() LocalStack 통합 테스트 (Issue #241, ADR-0006).

단계 분기 전수는 test_execute_revert_size.py가 맡고, 여기서는 **실물에서 타입이 실제로
백업 스펙 값으로 돌아가는가**만 본다. 호출이 받아들여지는 것과 자산이 되돌아가는 것은
다른 이야기이며, 에뮬레이터가 호출을 받아 놓고 아무것도 바꾸지 않은 전례가 있다
(ADR-0006 §4). 자동 원복이 셀링포인트라 이 자리가 조용히 통과하면 게이트 시연에서
처음 발견된다.

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
from services.aws import backup as bk  # noqa: E402
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
    found = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    return found["InstanceType"], found["State"]["Name"]


@pytest.fixture
def downsized_instance():
    """조치가 이미 적용된 자산 — RIGHTSIZING으로 타입을 바꿔 놓고 빌려준다.

    원복 시험의 전제는 "우리가 바꾼 뒤"라, 백업 스펙(조치 이전 타입)과 적용 타입이
    실물에 함께 존재해야 한다. 같은 자산을 test_precheck_localstack.py가 running
    전제로 쓰므로 끝나고 원래대로 되돌려 놓는다.
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
    applied_type = "t3.small" if original_type != "t3.small" else "t3.micro"
    arn = f"arn:aws:ec2:{default_region()}:{account_id()}:instance/{instance_id}"

    # 백업은 조치 직전에 캡처된다 — 실행 순서를 실물에서도 같게 둔다
    capture = bk.capture_instance_spec(instance_id, default_region())
    assert capture.captured, capture.detail
    applied = ex.execute_rightsizing(arn, target_instance_type=applied_type)
    assert applied.succeeded, applied.error_summary

    yield {
        "ec2": ec2,
        "id": instance_id,
        "arn": arn,
        "backup": capture.payload,
        "applied_type": applied_type,
    }

    current_type, current_state = _instance_state(ec2, instance_id)
    if current_type != original_type:
        ex.execute_rightsizing(arn, target_instance_type=original_type)
    elif current_state != "running":
        ec2.start_instances(InstanceIds=[instance_id])
        ec2.get_waiter("instance_running").wait(
            InstanceIds=[instance_id], WaiterConfig={"Delay": 2, "MaxAttempts": 30}
        )


def test_revert_actually_restores_the_backup_instance_type(downsized_instance):
    """호출 성공이 아니라 **반영**을 본다 — 되돌아가지 않으면 자동 원복은 없는 것이다."""
    fx = downsized_instance
    backup = fx["backup"]

    outcome = ex.execute_revert_size(
        fx["arn"],
        restore_instance_type=backup["instance_type"],
        applied_instance_type=fx["applied_type"],
        restore_state=backup["state"],
    )

    assert outcome.succeeded, outcome.error_summary
    restored_type, state = _instance_state(fx["ec2"], fx["id"])
    assert restored_type == backup["instance_type"]
    # 다시 켤지는 백업의 state가 정한다 — 시드 인스턴스는 running이었다
    assert state in {"pending", "running"}


def test_revert_records_every_step_with_its_effect(downsized_instance):
    """실물 경로에서도 단계 기록이 그대로 남는다 — 중단 복구 판정의 좌표다."""
    fx = downsized_instance
    backup = fx["backup"]
    recorded = []

    outcome = ex.execute_revert_size(
        fx["arn"],
        restore_instance_type=backup["instance_type"],
        applied_instance_type=fx["applied_type"],
        restore_state=backup["state"],
        record_step=recorded.append,
    )

    assert [(s.sequence, s.step_type, s.status, s.effect) for s in outcome.steps] == [
        (1, ex.STEP_STOP_INSTANCE, S.SUCCESS, E.APPLIED),
        (2, ex.STEP_MODIFY_INSTANCE_TYPE, S.SUCCESS, E.APPLIED),
        (3, ex.STEP_START_INSTANCE, S.SUCCESS, E.APPLIED),
    ]
    assert [(s.sequence, s.status) for s in recorded][:2] == [
        (1, S.IN_PROGRESS),
        (1, S.SUCCESS),
    ]


def test_third_party_drift_leaves_the_instance_untouched(downsized_instance):
    """제3자가 바꾼 실물 위에서 원복이 멈추는지 본다 (ADR-0008 §3-2 ③).

    대조 축을 실제와 다르게 주어 ③을 만든다 — 현재 타입이 백업 값도, 우리가 적용한
    값이라고 주장한 값도 아닌 상태다. 덮어쓰면 이 단언이 깨진다.
    """
    fx = downsized_instance
    backup = fx["backup"]
    before_type, _ = _instance_state(fx["ec2"], fx["id"])

    outcome = ex.execute_revert_size(
        fx["arn"],
        restore_instance_type=backup["instance_type"],
        applied_instance_type="c5.metal",  # 우리가 바꾼 값이 아니라고 말한다
        restore_state=backup["state"],
    )

    assert not outcome.succeeded
    assert _instance_state(fx["ec2"], fx["id"])[0] == before_type


def test_already_restored_instance_is_left_alone(downsized_instance):
    """되돌릴 것이 없으면 AWS를 부르지 않는다 (ADR-0008 §3-2 ①)."""
    fx = downsized_instance
    backup = fx["backup"]

    # 현재 타입을 백업 값으로 주장해 ①을 만든다
    outcome = ex.execute_revert_size(
        fx["arn"],
        restore_instance_type=fx["applied_type"],
        applied_instance_type=backup["instance_type"],
        restore_state=backup["state"],
    )

    assert outcome.succeeded
    assert [s.step_type for s in outcome.steps] == [ex.STEP_COMPARE_INSTANCE_TYPE]
    assert outcome.steps[0].effect is E.NOT_APPLIED
