"""rollback.wait_for_status_check() LocalStack 통합 테스트 — 부팅 실패 주입 (ADR-0006 §4 2행 ⓑ).

LocalStack에는 실제 부팅·헬스체크가 없어 `impaired` 검사 결과는 만들 수 없다. 대신 **대상
인스턴스를 stop 시키면** 판정이 `_NOT_BOOTING_STATES` 분기로 떨어진다. 가짜 AWS의 상태만 바꾸는
것이라 프로덕션 코드에 데모 분기가 없다(ADR-0006 §3). 10/1(목) 시연의 T1 7·8·9번(자동 원복)이
이 주입 하나에 기대므로, 에뮬레이터 동작이 바뀌면(이미지 상향 등) 시연 전에 여기서 먼저 깨지게 둔다.

분기 전수는 test_status_check.py(단위)가 맡고, 여기서는 **에뮬레이터가 그 분기의 입력을 실제로
만들어 주는가**만 본다. 2026-09-15 실측(LocalStack 4.14.0):
  - DescribeInstanceStatus 기본 호출은 stopped 인스턴스를 빈 목록으로 돌려준다 — waiter는
    MaxAttempts를 다 쓰고 WaiterError로 끝난다
  - IncludeAllInstances=True 재조회는 `stopped`와 검사 결과 `not-applicable`을 돌려준다 — FAILED
  - running 인스턴스는 첫 시도에 2/2 `ok`다. 대기 도중에는 끼어들 틈이 없으므로 주입은 실행
    주기와 판정 주기 사이에 한다(dispatcher._AWAIT_JUDGEMENT_ON_SUCCESS)

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

from services.aws import rollback as rb  # noqa: E402
from services.aws.client import account_id, aws_client, default_region  # noqa: E402


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)


def _state(ec2, instance_id: str) -> str:
    return ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0][
        "State"
    ]["Name"]


@pytest.fixture
def running_instance():
    """시드 인스턴스 1대를 빌려 쓰고, 끝나면 running으로 되돌려 놓는다.

    같은 자산을 test_precheck_localstack.py·test_execute_localstack.py가 running 전제로
    쓰므로 복원은 선택이 아니다 — 실행 순서에 따라 그쪽이 깨진다.
    """
    ec2 = aws_client("ec2")
    instance = next(
        i
        for r in ec2.describe_instances()["Reservations"]
        for i in r["Instances"]
        if i["State"]["Name"] == "running"
    )
    instance_id = instance["InstanceId"]
    arn = f"arn:aws:ec2:{default_region()}:{account_id()}:instance/{instance_id}"
    yield {"ec2": ec2, "id": instance_id, "arn": arn}

    if _state(ec2, instance_id) != "running":
        ec2.start_instances(InstanceIds=[instance_id])
        ec2.get_waiter("instance_running").wait(
            InstanceIds=[instance_id], WaiterConfig={"Delay": 2, "MaxAttempts": 30}
        )


def test_running_instance_passes_two_of_two(running_instance):
    """주입이 없으면 OK다 — 실패는 주입이 만든 것이지 에뮬레이터가 원래 내는 값이 아니다."""
    fx = running_instance

    outcome = rb.wait_for_status_check(fx["arn"], delay_seconds=1, max_attempts=3)

    assert outcome.verdict is rb.StatusCheckVerdict.OK, outcome.summary


def test_stopped_instance_is_visible_only_with_include_all(running_instance):
    """ADR-0006 §4 2행이 미측정으로 남겨 둔 전제 그 자체.

    기본 호출이 빈 목록이라 waiter만으로는 "없음"과 "멈춤"이 갈리지 않는다. 재조회가
    stopped를 돌려주지 않으면 판정 사유가 PRECHECK_TARGET_NOT_FOUND가 되어 시연 서사가
    "기동 실패"가 아니라 "대상 없음"이 된다.
    """
    fx = running_instance
    fx["ec2"].stop_instances(InstanceIds=[fx["id"]])

    default = fx["ec2"].describe_instance_status(InstanceIds=[fx["id"]])["InstanceStatuses"]
    included = fx["ec2"].describe_instance_status(
        InstanceIds=[fx["id"]], IncludeAllInstances=True
    )["InstanceStatuses"]

    assert default == []
    assert [s["InstanceState"]["Name"] for s in included] == ["stopped"]


def test_stopped_instance_is_judged_a_boot_failure_not_a_missing_target(running_instance):
    """주입이 여는 분기 — FAILED이되 사유가 "대상 없음"이 아니라 "기동 실패"다.

    dispatcher는 이 판정을 ROLLBACK_INITIATED로 확정하고 다음 주기에 REVERT_SIZE를
    AUTO_ON_FAILURE로 발동한다. 그 뒤는 DB 경로라 여기서 보지 않는다.
    """
    fx = running_instance
    fx["ec2"].stop_instances(InstanceIds=[fx["id"]])

    outcome = rb.wait_for_status_check(fx["arn"], delay_seconds=1, max_attempts=2)

    assert outcome.verdict is rb.StatusCheckVerdict.FAILED
    assert outcome.instance_state == "stopped"
    assert outcome.reason_code is None
    assert not outcome.probe_failed
