"""스펙 JSON 백업 캡처 LocalStack 통합 테스트 (services/aws/backup.py).

가짜 응답이 아니라 실제 describe_instances 응답으로 캡처한다. 손으로 만든
픽스처는 "AWS가 정말 그 필드를 주는가"를 증명하지 못한다 — 원복 필수 3종이
실물 응답에도 있는지는 여기서만 확인된다.

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

from schemas.backups import BackupType, InstanceSpecBackup  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws.client import aws_client, default_region  # noqa: E402

R = PrecheckReasonCode


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)


@pytest.fixture(scope="module")
def seed_instance():
    ec2 = aws_client("ec2")
    for reservation in ec2.describe_instances()["Reservations"]:
        for instance in reservation["Instances"]:
            if instance["State"]["Name"] == "running":
                return instance
    pytest.skip("running EC2 없음 — scripts/seed_localstack.py 를 먼저 실행할 것")


def test_capture_reproduces_the_live_instance_spec(seed_instance):
    capture = bk.capture_instance_spec(seed_instance["InstanceId"], default_region())

    assert capture.captured, capture.detail
    assert capture.backup_type == BackupType.SAVE_INSTANCE_SPEC_JSON.value
    assert capture.payload["instance_id"] == seed_instance["InstanceId"]
    assert capture.payload["instance_type"] == seed_instance["InstanceType"]
    assert capture.payload["state"] == seed_instance["State"]["Name"]


def test_live_payload_round_trips_through_the_contract(seed_instance):
    """DB에 넣고 원복 시점에 다시 읽는 값이다 — 계약으로 되돌아와야 한다."""
    payload = bk.capture_instance_spec(
        seed_instance["InstanceId"], default_region()
    ).payload

    assert InstanceSpecBackup(**payload).instance_type == seed_instance["InstanceType"]


def test_absent_instance_is_target_not_found():
    capture = bk.capture_instance_spec("i-00000000000000000", default_region())

    assert not capture.captured
    assert capture.reason_code is R.PRECHECK_TARGET_NOT_FOUND
