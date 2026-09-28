"""EBS 삭제 LocalStack 통합 테스트 (Issue #369, ADR-0006).

단계 분기 전수는 test_execute_ebs_delete.py가 맡고, 여기서는 **실물에서 스냅숏이
실제로 남고 볼륨이 실제로 사라지는가**, 그리고 **가드레일 ④가 그 볼륨을 통과시키는가**를
본다. 이 런북은 되돌릴 수 없어서, 가짜 클라이언트만으로는 "지워졌다"를 증명할 수 없다.

**시드 볼륨(`vigilantis-seed-unattached`)을 쓰지 않는다.** 삭제는 자원을 없애는 조치라
공용 시드를 지우면 다음 스캔·다른 테스트가 그 자산을 잃는다. 이 파일이 자기 볼륨을
만들어 쓰고, 실패해도 남지 않게 정리한다(test_execute_nacl_localstack.py와 같은 이유).

전제 실측(2026-09-21): LocalStack Community가 `create_snapshot` → `snapshot_completed`
전이와 `delete_volume` 뒤 `InvalidVolume.NotFound`를 실 AWS와 같게 답한다. 다만
**스냅숏을 즉시 completed로 만들기 때문에 대기 상한은 여기서 걸리지 않는다** — 그 축은
단위 테스트가 waiter 실패를 주입해 본다.

로컬 실행 전제: LocalStack 기동. 미기동 시 전체 skip.
"""

import os
import sys
import urllib.request
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

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
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import RunbookId  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws.client import account_id, aws_client, default_region  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

AZ_SUFFIX = "a"


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)


def _volume_gone(ec2, volume_id: str) -> bool:
    try:
        ec2.describe_volumes(VolumeIds=[volume_id])
    except ClientError as exc:
        return exc.response["Error"]["Code"] == "InvalidVolume.NotFound"
    return False


@pytest.fixture
def scratch_volume():
    """미연결 볼륨 1개를 만들어 쓰고, 남으면 지운다. (volume_id, target_arn) 짝."""
    ec2 = aws_client("ec2")
    region = default_region()
    volume = ec2.create_volume(
        AvailabilityZone=f"{region}{AZ_SUFFIX}",
        Size=1,
        VolumeType="gp3",
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [{"Key": "Name", "Value": "vigilantis-test-ebs-delete"}],
            }
        ],
    )
    volume_id = volume["VolumeId"]
    arn = f"arn:aws:ec2:{region}:{account_id()}:volume/{volume_id}"
    try:
        yield volume_id, arn
    finally:
        # 조치가 지웠으면 여기서 할 일이 없다 — 실패한 회차만 정리된다
        if not _volume_gone(ec2, volume_id):
            try:
                ec2.delete_volume(VolumeId=volume_id)
            except ClientError:
                pass


def _snapshots_of(ec2, volume_id: str) -> list[dict]:
    return ec2.describe_snapshots(
        Filters=[{"Name": "volume-id", "Values": [volume_id]}], OwnerIds=["self"]
    )["Snapshots"]


def test_precheck_passes_for_an_unattached_volume(scratch_volume):
    """가드레일 ④ — 실행 경로의 입구가 이 볼륨을 통과시키는가."""
    volume_id, arn = scratch_volume

    outcome = ex.precheck(
        RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED.value,
        target_arn=arn,
        parameters={"volume_id": volume_id, "evidence_id": "EVD-TEST-369"},
    )

    assert outcome.passed, outcome.reason_code


def test_snapshot_is_kept_and_the_volume_is_gone(scratch_volume):
    """실행 1회 — 스냅숏이 남고 볼륨이 사라진다."""
    volume_id, arn = scratch_volume
    ec2 = aws_client("ec2")
    recorded = []

    outcome = ex.execute_ebs_delete_unattached(arn, record_step=recorded.append)

    assert outcome.succeeded, outcome.error_summary
    assert [step.step_type for step in outcome.steps] == [
        ex.STEP_CREATE_SNAPSHOT,
        ex.STEP_WAIT_SNAPSHOT,
        ex.STEP_DELETE_VOLUME,
    ]
    assert all(step.effect is E.APPLIED for step in outcome.steps)

    # 볼륨은 사라졌다 — 판정(judge_ebs_delete_unattached)이 보는 그 축이다
    assert _volume_gone(ec2, volume_id)

    # 스냅숏은 남았다 — 되살릴 유일한 근거다
    snapshots = _snapshots_of(ec2, volume_id)
    assert len(snapshots) == 1
    snapshot_id = snapshots[0]["SnapshotId"]
    assert snapshots[0]["State"] == "completed"
    # 그 ID가 실행 기록에 남아야 관제자가 복구 근거를 찾을 수 있다
    assert snapshot_id in outcome.steps[0].result_summary
    assert snapshot_id in outcome.steps[-1].result_summary

    ec2.delete_snapshot(SnapshotId=snapshot_id)


def test_deleting_a_volume_that_is_already_gone_stays_successful(scratch_volume):
    """같은 볼륨에 두 번 — 두 번째는 지울 것이 없고 스냅숏도 만들지 않는다.

    재실행(ADR-0008 §7)이 실제로 닿는 경로다. 두 번째 회차가 실패로 닫히면 이미 끝난
    조치가 미완으로 기록되고, 스냅숏을 또 만들면 없는 볼륨에 대한 호출이 나간다.
    """
    volume_id, arn = scratch_volume
    ec2 = aws_client("ec2")

    first = ex.execute_ebs_delete_unattached(arn)
    assert first.succeeded, first.error_summary
    snapshot_id = _snapshots_of(ec2, volume_id)[0]["SnapshotId"]

    second = ex.execute_ebs_delete_unattached(arn)

    assert second.succeeded
    assert [step.step_type for step in second.steps] == [ex.STEP_COMPARE_VOLUME_STATE]
    assert second.steps[0].effect is E.NOT_APPLIED
    # 없는 볼륨에 스냅숏을 또 만들지 않았다
    assert len(_snapshots_of(ec2, volume_id)) == 1

    ec2.delete_snapshot(SnapshotId=snapshot_id)
