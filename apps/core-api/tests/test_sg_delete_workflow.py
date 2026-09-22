"""SG 삭제·원복 워크플로 통합 테스트 — 실제 PostgreSQL 필요(미기동 시 skip). (Issue #368)

AWS 호출 분기는 services/tests/test_execute_sg.py가 맡고, 여기서는 **배선·순서·판정**을 본다.

  - 실행 함수와 판정 함수가 **짝으로** 등록됐는가 (ADR-0008 §6)
  - 백업이 **AWS 삭제 호출 이전에** commit되는가 — 지운 뒤에는 되살릴 근거를 얻을 수 없다
  - **삭제 실패가 사람 승인 없는 원복을 부르지 않는가** — 이 카드가 먼저 막은 결함이다
  - 소멸 표시된 SG를 대상으로 한 원복이 가드레일 ③을 통과하는가

세 번째 축이 이 파일의 요점이다. `SG_RECREATE`는 ADR-0004가 `HUMAN_ONLY`로 정한 원복이라,
삭제가 `UNKNOWN`으로 끝났다고 시스템이 스스로 SG를 다시 만들어서는 안 된다. 가드레일 ②는
런북별 `trigger_source` 허용 목록을 아직 보지 않으므로, 그 자리를 지키는 것은 스케줄러의
`_AUTO_ROLLBACK_ON_ASSET_CHANGE` 하나뿐이다.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

CORE_API = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
import workflows  # noqa: E402
from db.repositories import assets as assets_repo  # noqa: E402
from db.repositories import executions as exec_repo  # noqa: E402
from db.repositories import incidents as incidents_repo  # noqa: E402
from schemas.api.actions import ExecuteActionRequest, ExecutionStatus  # noqa: E402
from schemas.api.assets import AssetType  # noqa: E402
from schemas.api.incidents import IncidentCategory, IncidentStatus  # noqa: E402
from schemas.backups import BackupType  # noqa: E402
from schemas.candidates import CandidateStatus  # noqa: E402
from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import (  # noqa: E402
    APPROVAL_MODE_BY_ROLLBACK_ID,
    ApprovalMode,
    RunbookId,
)
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
GROUP = "sg-0abc123456789def0"
NEW_GROUP = "sg-0fed987654321cba0"
VPC = "vpc-0abc123456789def0"
GROUP_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{GROUP}"
GROUP_NAME = "vigilantis-seed-unused"

SSH_FROM_BASTION = {
    "IpProtocol": "tcp",
    "FromPort": 22,
    "ToPort": 22,
    "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
}
DEFAULT_EGRESS = dict(ex.DEFAULT_EGRESS_PERMISSION)

# 판정 불가 재시도 — 상한 3회·간격 없음. 운영값(설정)과 무관하게 주기 수로 세게 한다
# (test_dispatcher와 같은 이유, Issue #249)
RETRY_NOW = workflows.VerificationRetryPolicy(max_attempts=3, interval_seconds=0)


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Op",
    )


def dry_run_ok() -> ClientError:
    """DryRun 통과 신호 — 예외 없이 돌아오면 플래그가 안 먹은 것으로 읽힌다."""
    return ClientError({"Error": {"Code": "DryRunOperation"}}, "Op")


class FakeEc2:
    """원본 SG는 살아 있고, 재생성 SG는 만든 직후 기본 egress만 달린 상태."""

    def __init__(self, state):
        self._state = state

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((operation, kwargs))
            if kwargs.get("DryRun"):
                # 가드레일 ④는 DryRunOperation 예외만 통과로 인정한다
                raise self._state["overrides"].get("dry_run", dry_run_ok())
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if operation == "describe_security_groups":
                return self._describe(kwargs)
            if operation == "create_security_group":
                self._state["recreated"] = {
                    "GroupId": NEW_GROUP,
                    "GroupName": kwargs["GroupName"],
                    "VpcId": kwargs["VpcId"],
                    "IpPermissions": [],
                    # AWS가 자동으로 붙이는 전체 허용 egress (2026-09-22 실측)
                    "IpPermissionsEgress": [dict(DEFAULT_EGRESS)],
                }
                return {"GroupId": NEW_GROUP}
            if operation == "revoke_security_group_egress":
                self._state["recreated"]["IpPermissionsEgress"] = []
                return {}
            if operation in (
                "authorize_security_group_ingress",
                "authorize_security_group_egress",
            ):
                key = (
                    "IpPermissions"
                    if operation.endswith("ingress")
                    else "IpPermissionsEgress"
                )
                self._state["recreated"][key] = [
                    dict(permission) for permission in kwargs["IpPermissions"]
                ]
                return {}
            return {}

        return call

    def _describe(self, kwargs):
        """ID 조회와 이름 필터를 가른다 — 실행은 앞을, 판정은 뒤를 쓴다."""
        recreated = self._state["recreated"]
        if kwargs.get("GroupIds") == [NEW_GROUP]:
            return {"SecurityGroups": [dict(recreated)]}
        if kwargs.get("GroupIds") == [GROUP]:
            if self._state["origin_deleted"]:
                raise client_error("InvalidGroup.NotFound", 400)
            return {"SecurityGroups": [dict(self._state["origin_group"])]}
        # group-name + vpc-id 필터 — 재생성 SG를 찾는 판정 경로다
        return {"SecurityGroups": [dict(recreated)] if recreated else []}


@pytest.fixture
def aws(monkeypatch):
    state = {
        "overrides": {},
        "calls": [],
        "origin_deleted": False,
        "recreated": None,
        "origin_group": {
            "GroupId": GROUP,
            "GroupName": GROUP_NAME,
            "Description": "unused security group",
            "VpcId": VPC,
            "IpPermissions": [dict(SSH_FROM_BASTION)],
            "IpPermissionsEgress": [dict(DEFAULT_EGRESS)],
        },
    }

    def factory(service, region=None, **_):
        return FakeEc2(state)

    # 캡처(backup)와 실행(executor)이 같은 가짜를 본다 — 백업 payload와 실행 대상이
    # 갈리면 이 파일이 재현하려는 순서 자체가 성립하지 않는다
    for module in (bk, ex):
        monkeypatch.setattr(module, "aws_client", factory)

    def configure(origin_group=None, **overrides):
        if origin_group is not None:
            state["origin_group"] = origin_group
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    configure.state = state
    return configure


def operations(aws):
    return [name for name, _ in aws.calls]


def collected_sg(db, *, absent: bool = False):
    """가드레일 ③은 수집된 자산만 통과시킨다 — 지워진 SG의 원복도 예외가 아니다."""
    run = assets_repo.start_collection_run(
        db,
        account_id=ACCOUNT,
        region=REGION,
        mode="localstack",
        lookback_days=14,
        period_seconds=3600,
    )
    asset = assets_repo.upsert_asset(
        db,
        arn=GROUP_ARN,
        asset_type=AssetType.SG,
        resource_id=GROUP,
        account_id=ACCOUNT,
        region=REGION,
        spec={"group_name": GROUP_NAME, "vpc_id": VPC},
        collection_run_id=run.collection_run_id,
        collected_at=datetime.now(timezone.utc),
    )
    if absent:
        # 삭제 뒤 첫 수집 회차가 찍는 소멸 표시(#332). 목록 조회만 이 행을 거르고
        # get_asset_by_arn은 그대로 돌려주므로 가드레일 ③은 통과해야 한다
        asset.absent_since = datetime.now(timezone.utc)
        db.flush()
    return asset


@pytest.fixture()
def reserved_delete(db, make_incident, make_candidate, make_execution):
    """SG 삭제 실행 1건 — SECOPS Incident + CLAIMED 후보 위에 선다."""

    def _make(*, collected=True, **kwargs):
        if collected:
            collected_sg(db)
        incident = make_incident(
            db,
            category=IncidentCategory.SECOPS,
            subject_arn=GROUP_ARN,
            status=IncidentStatus.ANALYZING,
        )
        candidate = make_candidate(
            db,
            incident,
            runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED,
            target_arn=GROUP_ARN,
            status=CandidateStatus.CLAIMED,
        )
        incidents_repo.update_incident_status(
            db,
            incident.incident_id,
            expected=incident.status,
            next_status=IncidentStatus.ACTION_IN_PROGRESS,
        )
        execution = make_execution(
            db,
            incident,
            runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED,
            target_arn=GROUP_ARN,
            candidate=candidate,
            **kwargs,
        )
        db.commit()
        return execution

    return _make


def run_delete(db, execution):
    return workflows.run_sg_delete_isolated_execution(db, execution.execution_id)


def judge_delete(db, execution):
    return workflows.judge_sg_delete_isolated(db, execution.execution_id)


def cycle(db, publish=None):
    """스캔 1회 — 세션은 픽스처가 소유한다(test_dispatcher의 cycle과 같은 껍질)."""
    return dispatcher.dispatch_pending(db, publish, RETRY_NOW)


# ------------------------------------------------------------------ 디스패치 배선


def test_both_runbooks_are_registered_as_runner_judge_pairs():
    """짝이 어긋나면 import 시점에 기동이 막힌다(dispatcher.py) — 그 계약을 여기서도 고정한다.

    runner만 등록하면 승인은 접수되는데 실행이 매 주기 미지원으로 넘어가 IN_PROGRESS에
    갇힌다. 이 두 런북이 바로 그 상태였고, 그것이 이 카드의 출발점이다.
    """
    for runbook, runner, judge in (
        (
            RunbookId.RUNBOOK_SG_DELETE_ISOLATED,
            workflows.run_sg_delete_isolated_execution,
            workflows.judge_sg_delete_isolated,
        ),
        (
            RunbookId.RUNBOOK_SG_RECREATE,
            workflows.run_sg_recreate_execution,
            workflows.judge_sg_recreate,
        ),
    ):
        assert dispatcher._RUNNERS[runbook] is runner
        assert dispatcher._JUDGES[runbook] is judge
    assert set(dispatcher._RUNNERS) == set(dispatcher._JUDGES)


def test_sg_delete_is_not_an_auto_rollback_trigger():
    """짝이 있다고 자동 발동이 열리지 않는다 — 기준은 ADR-0004의 승인 정책이다.

    `SG_RECREATE`가 `HUMAN_ONLY`이므로 `SG_DELETE_ISOLATED`의 "적용 여부 불명확"은
    자동 원복이 아니라 현물 판정으로 간다.
    """
    assert (
        APPROVAL_MODE_BY_ROLLBACK_ID[RunbookId.RUNBOOK_SG_RECREATE.value]
        is ApprovalMode.HUMAN_ONLY
    )
    assert (
        RunbookId.RUNBOOK_SG_DELETE_ISOLATED
        not in dispatcher._AUTO_ROLLBACK_ON_ASSET_CHANGE
    )


def test_only_system_approved_rollbacks_open_auto_rollback():
    """이 집합의 유일한 원소가 RIGHTSIZING이다 — 짝(REVERT_SIZE)만 SYSTEM_OR_HUMAN이다.

    EC2_ISOLATE도 짝(UNISOLATE)이 HUMAN_ONLY라 들어오지 않는다. P2 착수 때 같은 결함이
    되살아나지 않도록 여기서 함께 고정한다.
    """
    assert dispatcher._AUTO_ROLLBACK_ON_ASSET_CHANGE == frozenset(
        {RunbookId.RUNBOOK_EC2_RIGHTSIZING}
    )


# ------------------------------------------------------------------ 삭제 실행


def test_backup_is_committed_before_the_delete_call(db, reserved_delete, aws):
    """순서가 계약이다 — 지운 뒤에는 이름도 규칙도 AWS에 다시 물을 수 없다."""
    execution = reserved_delete()

    outcome = run_delete(db, execution)

    assert outcome.succeeded
    assert operations(aws) == ["describe_security_groups", "delete_security_group"]
    record = exec_repo.get_backup_record(db, execution.backup_record_id)
    assert record.backup_type == BackupType.SAVE_SG_FULL_RULES_JSON.value
    assert record.payload["group_name"] == GROUP_NAME
    assert record.payload["ingress_permissions"] == [SSH_FROM_BASTION]
    assert record.payload["group_id"] == GROUP


def test_backup_is_captured_once_per_execution(db, reserved_delete, aws):
    """재시도가 새 레코드를 만들면 "삭제 직전"이 아니라 이미 바뀐 뒤의 상태가 근거가 된다."""
    execution = reserved_delete()

    first = workflows.store_sg_full_rules_backup(db, execution.execution_id)
    second = workflows.store_sg_full_rules_backup(db, execution.execution_id)

    assert first.created and not second.created
    assert first.record.backup_record_id == second.record.backup_record_id


def test_delete_does_not_start_when_the_backup_cannot_be_captured(
    db, reserved_delete, aws
):
    """백업이 없으면 되돌릴 수 없는 변경이 된다 — 삭제를 시작하지 않는다(ADR-0008 §1 ①)."""
    aws(describe_security_groups=client_error("InvalidGroup.NotFound", 400))
    execution = reserved_delete()

    outcome = run_delete(db, execution)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND
    assert "delete_security_group" not in operations(aws)
    assert outcome.steps == ()


def test_dependency_violation_leaves_the_group_and_records_no_change(
    db, reserved_delete, aws
):
    """참조가 남은 SG는 AWS가 거절한다 — 자산이 그대로인 실패로 확정된다. (DoD)"""
    aws(delete_security_group=client_error("DependencyViolation", 400))
    execution = reserved_delete()

    outcome = run_delete(db, execution)

    assert not outcome.succeeded
    assert outcome.steps[-1].effect is E.NOT_APPLIED
    assert not dispatcher._changed_the_asset(outcome)


def test_refuses_to_run_a_finished_execution(db, reserved_delete, aws):
    """끝난 실행을 다시 돌리면 백업 없는 두 번째 변경이 된다."""
    execution = reserved_delete(status=ExecutionStatus.SUCCESS)

    with pytest.raises(ValueError):
        run_delete(db, execution)
    assert operations(aws) == []


# ------------------------------------------------------------------ 삭제 판정


def test_judges_success_when_the_group_is_gone(db, reserved_delete, aws):
    """성공의 경계는 실자산이다 — 단계 기록이 아니라 지금 SG가 있는지가 답한다."""
    execution = reserved_delete()
    run_delete(db, execution)
    aws(describe_security_groups=client_error("InvalidGroup.NotFound", 400))

    judgement = judge_delete(db, execution)

    assert judgement.next_status is ExecutionStatus.SUCCESS


def test_judges_failed_when_the_group_is_still_there(db, reserved_delete, aws):
    execution = reserved_delete()
    run_delete(db, execution)

    judgement = judge_delete(db, execution)

    assert judgement.next_status is ExecutionStatus.FAILED
    assert GROUP in judgement.error_summary


def test_defers_when_the_group_lookup_fails(db, reserved_delete, aws):
    """AWS에 물어보지 못했다 — 확정하면 검증기의 실패가 조치의 실패로 저장된다(#249)."""
    execution = reserved_delete()
    run_delete(db, execution)
    aws(describe_security_groups=EndpointConnectionError(endpoint_url="https://ec2"))

    judgement = judge_delete(db, execution)

    assert judgement.next_status is None
    assert judgement.defer_code is R.PRECHECK_AWS_ERROR


# ------------------------------------------------------------------ 자동 원복 회귀


def test_delete_that_ended_unknown_never_creates_a_rollback_child(
    db, reserved_delete, aws
):
    """**이 카드가 먼저 막은 결함의 회귀 테스트다.** (Issue #368 DoD)

    삭제가 `UNKNOWN`(AWS 5xx·응답 유실로 적용 여부를 모름)으로 끝나면, 짝으로 파생하던
    옛 집합에서는 `ROLLBACK_INITIATED`가 찍히고 **사람 승인 없이** SG가 다시 만들어졌다.
    ADR-0004는 `SG_RECREATE`를 `USER_APPROVAL`·`HUMAN_ONLY`로 정했고, 가드레일 ②는 런북별
    `trigger_source` 허용 목록을 아직 보지 않는다(packages/schemas/guardrails.py 주석).

    두 주기를 돌려 본다 — ① 실행이 UNKNOWN으로 끝나 현물 판정 대기로 남고,
    ② 다음 주기의 판정이 SG가 그대로임을 보고 FAILED로 닫는다. 어느 주기에도 자식
    실행은 생기지 않는다.
    """
    aws(delete_security_group=client_error("InternalError", 500))
    execution = reserved_delete()
    execution_id = execution.execution_id

    first = cycle(db)

    assert first.awaiting_judgement == 1
    assert first.rollback_initiated == 0 and first.rollback_started == 0
    assert exec_repo.get_execution(db, execution_id).status is ExecutionStatus.IN_PROGRESS
    assert exec_repo.list_rollback_children(db, execution_id) == []

    second = cycle(db)

    assert second.judged == 1
    closed = exec_repo.get_execution(db, execution_id)
    assert closed.status is ExecutionStatus.FAILED
    assert exec_repo.list_rollback_children(db, execution_id) == []


# ------------------------------------------------------------------ 원복 경로


def reserve_recreate(db, incident_id):
    """관제자 복구 접수 — HTTP가 쓰는 것과 같은 진입점이다."""
    reservation = workflows.reserve_execution(
        db,
        ExecuteActionRequest(
            incident_id=incident_id,
            runbook_id=RunbookId.RUNBOOK_SG_RECREATE,
            idempotency_key=str(uuid.uuid4()),
        ),
    )
    db.commit()
    return reservation.response.execution_id


@pytest.fixture()
def deleted_origin(db, reserved_delete, aws):
    """삭제가 SUCCESS로 닫힌 원본 1건 — 복구 목록에 [원복]이 서 있는 상태.

    삭제 이후 그 SG는 AWS에 없으므로 가짜도 그 사실을 따른다. 소멸 표시는 인자로
    가른다 — 수집 회차가 돌기 전과 돈 뒤가 다른 상태이고, 둘 다 원복이 가능해야 한다.
    """

    def _make(*, absent=False):
        execution = reserved_delete()
        incident_id, execution_id = execution.incident_id, execution.execution_id
        outcome = run_delete(db, execution)
        assert outcome.succeeded
        workflows.close_execution(
            db, execution_id, next_status=ExecutionStatus.SUCCESS
        )
        if absent:
            asset = assets_repo.get_asset_by_arn(db, GROUP_ARN)
            asset.absent_since = datetime.now(timezone.utc)
        db.commit()
        # 지워진 뒤다 — 원본 ID 조회는 InvalidGroup.NotFound로 답한다
        aws.state["origin_deleted"] = True
        return incident_id, execution_id

    return _make


def test_recreate_runs_the_full_restore_path(db, deleted_origin, aws):
    """생성 → 기본 egress 회수 → 규칙 주입. 접수 때 서버가 백업을 자식에 결속한다."""
    incident_id, origin_id = deleted_origin()

    child_id = reserve_recreate(db, incident_id)
    child = exec_repo.get_execution(db, child_id)

    assert child.parent_execution_id == origin_id
    assert child.backup_record_id is not None

    outcome = workflows.run_sg_recreate_execution(db, child_id)

    assert outcome.succeeded, outcome.error_summary
    assert [step.step_type for step in outcome.steps] == [
        ex.STEP_CREATE_SECURITY_GROUP,
        ex.STEP_REVOKE_DEFAULT_EGRESS,
        ex.STEP_AUTHORIZE_SG_INGRESS,
        ex.STEP_AUTHORIZE_SG_EGRESS,
    ]
    # 원본 ID를 참조하던 자원은 돌아오지 않는다 — 두 ID가 기록에 남아야 관제자가
    # 무엇을 손수 다시 이을지 안다(ADR-0008 §참조 무결성)
    assert NEW_GROUP in outcome.steps[0].result_summary
    assert GROUP in outcome.steps[0].result_summary
    # 재생성 SG의 규칙 집합이 백업과 같다
    assert workflows.judge_sg_recreate(db, child_id).next_status is ExecutionStatus.SUCCESS


def test_recreate_of_an_absent_asset_passes_guardrail_three(db, deleted_origin, aws):
    """③ ARN Match는 소멸 표시된 자산도 통과시킨다 — 목록 조회만 소멸분을 거른다(#332).

    이 전제가 깨지면(③에 소멸 필터가 들어가면) 삭제된 SG의 원복이 통째로 막힌다.
    수집 회차가 이미 돌아 `absent_since`가 찍힌 뒤가 정확히 그 상황이다.
    """
    incident_id, _ = deleted_origin(absent=True)
    assert assets_repo.get_asset_by_arn(db, GROUP_ARN).absent_since is not None
    assert GROUP_ARN not in [
        asset.arn for asset in assets_repo.list_assets(db, asset_type=AssetType.SG)
    ]

    child_id = reserve_recreate(db, incident_id)
    outcome = workflows.run_sg_recreate_execution(db, child_id)

    assert outcome.succeeded, outcome.error_summary
    assert "create_security_group" in operations(aws)


def test_recreate_does_not_start_without_a_bound_backup(
    db, make_incident, make_execution, aws
):
    """원복 값의 원천은 결속된 백업 하나다 — 없으면 자산을 만지지 않는다(ADR-0004 정책 ③)."""
    collected_sg(db)
    incident = make_incident(
        db, category=IncidentCategory.SECOPS, subject_arn=GROUP_ARN,
        status=IncidentStatus.ACTION_IN_PROGRESS,
    )
    origin = make_execution(
        db, incident, runbook_id=RunbookId.RUNBOOK_SG_DELETE_ISOLATED,
        target_arn=GROUP_ARN, status=ExecutionStatus.SUCCESS,
    )
    child = make_execution(
        db, incident, runbook_id=RunbookId.RUNBOOK_SG_RECREATE,
        target_arn=GROUP_ARN, parent=origin,
    )
    db.commit()

    outcome = workflows.run_sg_recreate_execution(db, child.execution_id)

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND
    assert operations(aws) == []


# ------------------------------------------------------------------ 원복 판정


def recreated_child(db, deleted_origin):
    """접수만 된 원복 1건 — 판정이 보는 것은 실행 결과가 아니라 실자산이다."""
    incident_id, _ = deleted_origin()
    return reserve_recreate(db, incident_id)


def test_judges_recreate_failed_when_only_the_group_was_created(
    db, deleted_origin, aws
):
    """그룹이 섰다는 것만으로 성공이라 하면 **규칙 없는 빈 SG**가 복원 완료로 닫힌다.

    자식이 SUCCESS면 원본까지 ROLLED_BACK으로 닫혀 인시던트가 내려가고, 절반만 선 SG를
    다시 볼 자리가 사라진다.
    """
    child_id = recreated_child(db, deleted_origin)
    # 이름·VPC로는 찾히지만 규칙이 비어 있다 — 주입 전에 끊긴 모습이다
    aws.state["recreated"] = {
        "GroupId": NEW_GROUP,
        "GroupName": GROUP_NAME,
        "VpcId": VPC,
        "IpPermissions": [],
        "IpPermissionsEgress": [],
    }

    judgement = workflows.judge_sg_recreate(db, child_id)

    assert judgement.next_status is ExecutionStatus.FAILED
    assert "인바운드" in judgement.error_summary


def test_judges_recreate_failed_when_the_egress_injection_never_ran(
    db, deleted_origin, aws
):
    """인바운드까지 서고 아웃바운드 직전에 끊긴 모습 — 두 방향을 따로 본다.

    한 방향만 대조하면 절반만 복원된 SG가 완료로 닫힌다.
    """
    child_id = recreated_child(db, deleted_origin)
    aws.state["recreated"] = {
        "GroupId": NEW_GROUP,
        "GroupName": GROUP_NAME,
        "VpcId": VPC,
        "IpPermissions": [dict(SSH_FROM_BASTION)],
        "IpPermissionsEgress": [],
    }

    judgement = workflows.judge_sg_recreate(db, child_id)

    assert judgement.next_status is ExecutionStatus.FAILED
    assert "아웃바운드" in judgement.error_summary


def test_judges_recreate_success_when_the_rules_match_the_backup(
    db, deleted_origin, aws
):
    """새 ID로는 찾을 수 없다 — 백업의 이름 + VPC가 재생성 SG를 찾는 유일한 좌표다.

    AWS는 빈 목록 키를 채워 돌려주고 순서도 보장하지 않는다. 그 차이를 불일치로 읽으면
    멀쩡히 복원된 SG가 "원복 미완"으로 확정된다.
    """
    child_id = recreated_child(db, deleted_origin)
    aws.state["recreated"] = {
        "GroupId": NEW_GROUP,
        "GroupName": GROUP_NAME,
        "VpcId": VPC,
        "IpPermissions": [
            {**SSH_FROM_BASTION, "Ipv6Ranges": [], "UserIdGroupPairs": []}
        ],
        "IpPermissionsEgress": [{**DEFAULT_EGRESS, "UserIdGroupPairs": []}],
    }

    judgement = workflows.judge_sg_recreate(db, child_id)

    assert judgement.next_status is ExecutionStatus.SUCCESS


def test_judges_recreate_failed_when_the_group_is_missing(db, deleted_origin, aws):
    child_id = recreated_child(db, deleted_origin)
    aws.state["recreated"] = None

    judgement = workflows.judge_sg_recreate(db, child_id)

    assert judgement.next_status is ExecutionStatus.FAILED
    assert GROUP_NAME in judgement.error_summary


def test_defers_recreate_judgement_when_the_lookup_fails(db, deleted_origin, aws):
    child_id = recreated_child(db, deleted_origin)
    aws(describe_security_groups=EndpointConnectionError(endpoint_url="https://ec2"))

    judgement = workflows.judge_sg_recreate(db, child_id)

    assert judgement.next_status is None
    assert judgement.defer_code is R.PRECHECK_AWS_ERROR
