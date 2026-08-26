"""스펙 JSON 백업 저장 워크플로 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip).

캡처(services/aws/backup.py)는 단위 테스트가 맡고, 여기서는 **저장·결속·재시도**를
본다. 이 세 가지가 어긋나면 원복이 근거를 잃는다(ADR-0004 롤백 공통 정책 ③).
AWS는 가짜 클라이언트로 갈아 끼운다.
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import workflows  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.incidents import IncidentCategory  # noqa: E402
from schemas.backups import BackupType  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import RunbookId, TriggerSource  # noqa: E402
from services.aws import backup as bk  # noqa: E402

R = PrecheckReasonCode

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"

INSTANCE_RESPONSE = {
    "Reservations": [
        {
            "Instances": [
                {
                    "InstanceId": INSTANCE,
                    "InstanceType": "t3.xlarge",
                    "State": {"Name": "running"},
                    "Placement": {"AvailabilityZone": f"{REGION}a"},
                }
            ]
        }
    ]
}


class FakeEc2:
    def __init__(self, state):
        self._state = state

    def describe_instances(self, **kwargs):
        self._state["calls"].append(kwargs)
        outcome = self._state["outcome"]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def aws(monkeypatch):
    state = {"outcome": INSTANCE_RESPONSE, "calls": [], "clients": []}

    def factory(service, region=None, **_):
        state["clients"].append((service, region))
        return FakeEc2(state)

    monkeypatch.setattr(bk, "aws_client", factory)

    def configure(outcome):
        state["outcome"] = outcome

    configure.calls = state["calls"]
    configure.clients = state["clients"]
    return configure


def _execution(db, *, runbook=RunbookId.RUNBOOK_EC2_RIGHTSIZING, target_arn=INSTANCE_ARN):
    incident = incidents_repo.create_incident(
        db, subject_arn=INSTANCE_ARN, category=IncidentCategory.FINOPS
    )
    return exec_repo.create_execution(
        db,
        incident_id=incident.incident_id,
        runbook_id=runbook,
        target_arn=target_arn,
        trigger_source=TriggerSource.USER_APPROVAL,
    )


# ------------------------------------------------------------------ 저장·결속


def test_backup_is_stored_and_bound_to_the_execution(db, aws):
    execution = _execution(db)

    outcome = workflows.store_instance_spec_backup(db, execution.execution_id)

    assert outcome.stored and outcome.created
    assert outcome.record.backup_type == BackupType.SAVE_INSTANCE_SPEC_JSON.value
    assert outcome.record.target_arn == INSTANCE_ARN
    # 결속되지 않은 백업은 원복이 찾지 못한다 — 저장만으로는 완료가 아니다
    assert execution.backup_record_id == outcome.record.backup_record_id


def test_stored_payload_carries_the_revert_input(db, aws):
    execution = _execution(db)

    outcome = workflows.store_instance_spec_backup(db, execution.execution_id)

    # REVERT_SIZE precheck가 읽는 값이 그대로 DB에 있어야 한다
    assert outcome.record.payload["instance_type"] == "t3.xlarge"
    assert outcome.record.payload["state"] == "running"


def test_capture_targets_the_arn_region_and_resource(db, aws):
    """실행의 target_arn이 가리키는 자원·리전으로만 조회한다 — 다른 리전으로
    나가면 같은 ID의 다른 자원을 스펙으로 기록하게 된다."""
    execution = _execution(
        db, target_arn=f"arn:aws:ec2:us-east-1:{ACCOUNT}:instance/{INSTANCE}"
    )

    workflows.store_instance_spec_backup(db, execution.execution_id)

    assert aws.clients == [("ec2", "us-east-1")]
    assert aws.calls == [{"InstanceIds": [INSTANCE]}]


# ------------------------------------------------------------------ 재시도


def test_second_call_reuses_the_first_backup(db, aws):
    """재시도가 새 레코드를 만들면 '조치 직전'이 아니라 '이미 바뀐 뒤'의 스펙이
    원복 값이 된다 — 그 원복은 아무것도 되돌리지 못한다."""
    execution = _execution(db)
    first = workflows.store_instance_spec_backup(db, execution.execution_id)

    # 두 번째 시도 시점의 AWS 상태는 이미 바뀌어 있다
    changed = {
        "InstanceId": INSTANCE,
        "InstanceType": "t3.medium",
        "State": {"Name": "stopped"},
    }
    aws({"Reservations": [{"Instances": [changed]}]})
    second = workflows.store_instance_spec_backup(db, execution.execution_id)

    assert second.stored and not second.created
    assert second.record.backup_record_id == first.record.backup_record_id
    assert second.record.payload["instance_type"] == "t3.xlarge"
    # 두 번째 호출은 AWS를 다시 부르지 않는다
    assert len(aws.calls) == 1


# ------------------------------------------------------------------ 실패 경로


def test_capture_failure_leaves_no_record_and_reports_the_reason(db, aws):
    """백업이 없으면 조치를 시작하면 안 된다 — 호출부가 이 사유로 실행을 끝낸다."""
    execution = _execution(db)
    aws(ClientError({"Error": {"Code": "UnauthorizedOperation"}}, "DescribeInstances"))

    outcome = workflows.store_instance_spec_backup(db, execution.execution_id)

    assert not outcome.stored
    assert outcome.reason_code is R.PRECHECK_UNAUTHORIZED
    assert execution.backup_record_id is None


def test_non_instance_target_is_rejected_before_calling_aws(db, aws):
    execution = _execution(
        db, target_arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0abc123456789def0"
    )

    outcome = workflows.store_instance_spec_backup(db, execution.execution_id)

    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


def test_missing_execution_is_reported_not_raised(db, aws):
    outcome = workflows.store_instance_spec_backup(db, "00000000-0000-0000-0000-000000000000")
    assert not outcome.stored
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND


def test_wrong_runbook_is_a_wiring_error(db, aws):
    """스펙 JSON 백업을 쓰는 런북은 RIGHTSIZING 하나뿐이다(런북 명세서). 판정으로
    삼키면 다른 런북이 엉뚱한 백업 종류를 달고 조용히 진행된다."""
    execution = _execution(db, runbook=RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED)

    with pytest.raises(ValueError, match="스펙 JSON 백업 대상 런북이 아닙니다"):
        workflows.store_instance_spec_backup(db, execution.execution_id)
