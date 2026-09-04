"""스펙 JSON 백업 캡처 단위 테스트 (services/aws/backup.py, ADR-0004 정책 ③).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 캡처 결과만 검증한다
(test_precheck_dispatch.py와 같은 방식). LocalStack 실물 확인은
test_backup_localstack.py가 맡는다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from schemas.backups import BackupType  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import backup as bk  # noqa: E402

R = PrecheckReasonCode

REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"

FULL_INSTANCE = {
    "InstanceId": INSTANCE,
    "InstanceType": "t3.xlarge",
    "State": {"Code": 16, "Name": "running"},
    "ImageId": "ami-0abc123456789def0",
    "Architecture": "x86_64",
    "RootDeviceType": "ebs",
    "EbsOptimized": False,
    "Placement": {"AvailabilityZone": "ap-northeast-2a"},
    "VpcId": "vpc-0abc123456789def0",
    "SubnetId": "subnet-0abc123456789def0",
    # 캡처 대상이 아닌 필드 — payload에 새어 들어가면 안 된다
    "KeyName": "vigilantis-key",
    "SecurityGroups": [{"GroupId": "sg-0abc123456789def0"}],
}


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "DescribeInstances")


class FakeEc2:
    def __init__(self, outcome, calls):
        self._outcome = outcome
        self.calls = calls

    def describe_instances(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


@pytest.fixture
def aws(monkeypatch):
    """backup.py가 쓰는 클라이언트를 가짜로 갈아 끼운다. 기본은 정상 응답."""
    state = {
        "outcome": {"Reservations": [{"Instances": [FULL_INSTANCE]}]},
        "calls": [],
        "clients": [],
    }

    def factory(service, region=None, **_):
        state["clients"].append((service, region))
        return FakeEc2(state["outcome"], state["calls"])

    monkeypatch.setattr(bk, "aws_client", factory)

    def configure(outcome):
        state["outcome"] = outcome

    configure.calls = state["calls"]
    configure.clients = state["clients"]
    return configure


# ------------------------------------------------------------------ 성공 경로


def test_capture_carries_the_declared_backup_type(aws):
    capture = bk.capture_instance_spec(INSTANCE, REGION)
    assert capture.captured
    assert capture.backup_type == BackupType.SAVE_INSTANCE_SPEC_JSON.value
    assert capture.reason_code is None


def test_capture_records_the_revert_inputs(aws):
    payload = bk.capture_instance_spec(INSTANCE, REGION).payload
    assert payload["instance_id"] == INSTANCE
    assert payload["instance_type"] == "t3.xlarge"
    # 타입 변경은 중지 상태에서만 된다 — 원복이 "다시 켜야 하는가"를 이 값으로 본다
    assert payload["state"] == "running"


def test_capture_keeps_only_the_declared_fields(aws):
    """모델이 extra=forbid라 응답을 통째로 싣지 않는다는 사실을 고정한다."""
    payload = bk.capture_instance_spec(INSTANCE, REGION).payload
    assert set(payload) == {
        "instance_id",
        "instance_type",
        "state",
        "image_id",
        "architecture",
        "root_device_type",
        "ebs_optimized",
        "availability_zone",
        "vpc_id",
        "subnet_id",
        # 원복 값이 아니라 한계 고지의 근거다 — 되돌려도 퍼블릭 IPv4는 돌아오지
        # 않으므로, 조치 이전 주소를 여기 남기지 않으면 영영 알 수 없다 (ADR-0008 §5)
        "public_ip_address",
        "elastic_ip_association_id",
    }


# ------------------------------------------------- 한계 고지 근거 (ADR-0008 §5)


def test_capture_records_the_public_ip_before_the_change(aws):
    """되돌려도 퍼블릭 IPv4는 돌아오지 않는다 — 조치 이전 주소를 남기지 않으면
    조치 후에는 영영 알 수 없어 관제자에게 사실대로 말할 수 없다."""
    aws({"Reservations": [{"Instances": [{**FULL_INSTANCE, "PublicIpAddress": "3.35.1.1"}]}]})

    payload = bk.capture_instance_spec(INSTANCE, REGION).payload

    assert payload["public_ip_address"] == "3.35.1.1"


def test_capture_records_the_eip_association_when_present(aws):
    """EIP가 붙어 있었으면 주소가 유지된다 — 위 고지의 반대 근거다."""
    aws(
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            **FULL_INSTANCE,
                            "PublicIpAddress": "3.35.1.1",
                            "NetworkInterfaces": [
                                {"Association": {"AssociationId": "eipassoc-1"}}
                            ],
                        }
                    ]
                }
            ]
        }
    )

    payload = bk.capture_instance_spec(INSTANCE, REGION).payload

    assert payload["elastic_ip_association_id"] == "eipassoc-1"


def test_auto_assigned_public_ip_leaves_the_eip_association_empty(aws):
    """자동 할당 주소에는 AssociationId가 없다 — 그 부재가 곧 "정지하면 바뀐다"다."""
    aws(
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            **FULL_INSTANCE,
                            "PublicIpAddress": "3.35.1.1",
                            "NetworkInterfaces": [
                                {"Association": {"IpOwnerId": "amazon", "PublicIp": "3.35.1.1"}}
                            ],
                        }
                    ]
                }
            ]
        }
    )

    payload = bk.capture_instance_spec(INSTANCE, REGION).payload

    assert payload["public_ip_address"] == "3.35.1.1"
    assert payload["elastic_ip_association_id"] is None


def test_missing_public_ip_does_not_block_the_capture(aws):
    """부가 항목의 부재는 조치를 막지 않는다 — 되돌리지 못하는 것과 다른 사건이다."""
    capture = bk.capture_instance_spec(INSTANCE, REGION)

    assert capture.captured
    assert capture.payload["public_ip_address"] is None


def test_capture_uses_the_region_it_was_given(aws):
    """target_arn의 리전으로 조회해야 한다 — 기본 리전으로 고정하면 두 번째
    리전 자산이 '없는 자원'이 된다(ADR-0007 §5 ③과 같은 이유)."""
    bk.capture_instance_spec(INSTANCE, "us-east-1")
    assert aws.clients == [("ec2", "us-east-1")]
    assert aws.calls == [{"InstanceIds": [INSTANCE]}]


def test_optional_fields_stay_none_when_aws_omits_them(aws):
    required = {k: FULL_INSTANCE[k] for k in ("InstanceId", "InstanceType", "State")}
    aws({"Reservations": [{"Instances": [required]}]})
    payload = bk.capture_instance_spec(INSTANCE, REGION).payload
    assert payload["image_id"] is None
    assert payload["ebs_optimized"] is None
    # 부가 정보가 비어 있는 것만으로 조치를 막지는 않는다
    assert payload["instance_type"] == "t3.xlarge"


def test_blank_strings_are_stored_as_none(aws):
    aws({"Reservations": [{"Instances": [{**FULL_INSTANCE, "ImageId": "  "}]}]})
    assert bk.capture_instance_spec(INSTANCE, REGION).payload["image_id"] is None


# ------------------------------------------------------------------ 실패 경로


@pytest.mark.parametrize(
    "error,expected",
    [
        (client_error("InvalidInstanceID.NotFound"), R.PRECHECK_TARGET_NOT_FOUND),
        (client_error("UnauthorizedOperation"), R.PRECHECK_UNAUTHORIZED),
        (client_error("Throttling"), R.PRECHECK_AWS_ERROR),
        (EndpointConnectionError(endpoint_url="http://x"), R.PRECHECK_AWS_ERROR),
    ],
)
def test_aws_errors_become_reason_codes_not_exceptions(aws, error, expected):
    """precheck와 같은 표(errors.reason_code_for)를 쓴다 — 예외로 새지 않는다."""
    aws(error)
    capture = bk.capture_instance_spec(INSTANCE, REGION)
    assert not capture.captured
    assert capture.reason_code is expected
    assert capture.payload is None


def test_missing_instance_is_target_not_found(aws):
    aws({"Reservations": []})
    assert bk.capture_instance_spec(INSTANCE, REGION).reason_code is R.PRECHECK_TARGET_NOT_FOUND


@pytest.mark.parametrize("field", ["InstanceId", "InstanceType", "State"])
def test_missing_revert_input_fails_the_capture(aws, field):
    """원복 필수 값이 없으면 백업이 아니다 — 조치를 시작하기 전에 막는다."""
    partial = {k: v for k, v in FULL_INSTANCE.items() if k != field}
    aws({"Reservations": [{"Instances": [partial]}]})
    capture = bk.capture_instance_spec(INSTANCE, REGION)
    assert not capture.captured
    assert capture.reason_code is R.PRECHECK_INVALID_STATE
    assert "누락" in capture.detail


def test_capture_result_cannot_be_both_success_and_failure():
    """호출부가 captured 하나만 보고 분기한다 — 두 값이 동시에 차면 그 분기가 거짓말이 된다."""
    with pytest.raises(ValueError):
        bk.BackupCapture(backup_type="X", payload={"a": 1}, reason_code=R.PRECHECK_AWS_ERROR)
    with pytest.raises(ValueError):
        bk.BackupCapture(backup_type="X")
