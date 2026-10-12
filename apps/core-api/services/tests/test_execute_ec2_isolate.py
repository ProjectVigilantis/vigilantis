"""EC2 격리·해제 단위 테스트 — 백업 캡처 · execute_ec2_isolate · execute_ec2_unisolate.
(MVP 체크리스트 §2-1 B1–B3)

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **호출 순서와 단계 effect**를 본다.
LocalStack Community에 `elbv2`가 없어(ADR-0006 §4 · ADR-0007 §Context) 실물 왕복은 실 AWS
스모크 몫이고, 이 파일이 로컬에서 이 경로를 고정하는 유일한 자리다.

이 파일이 지키는 것.

  ① 캡처가 **모든 ENI**를 DeviceIndex 순으로 담고, 주 ENI의 SG가 `security_group_ids`가
     된다(가드레일 ④ `_precheck_unisolate`가 읽는 키). 등록 포트를 담고, 미등록·다중 포트·
     조회 실패면 캡처가 실패해 격리를 시작하지 않는다(ADR-0008 §1 ①).
  ② 격리는 **TG 등록 해제 → ENI SG 교체** 순이다(E2E_DEMO_SCENARIOS §T3-5). 해제 대상
     포트와 ENI 목록은 백업에서 온다.
  ③ ENI가 일부만 바뀐 채 실패하면 단계 effect가 **PARTIAL**이다. 첫 ENI에서 실패하면
     예외 분류 그대로(4xx → NOT_APPLIED)다.
  ④ 해제는 **백업 값으로만** ENI별 SG를 복원하고 백업 포트로 재등록한다. 백업의 ENI가
     인스턴스에 없으면 아무것도 바꾸지 않는다(다른 자산의 SG를 바꾸지 않는다).
  ⑤ 실행 함수와 판정 함수가 디스패치 경로에 **짝으로** 등록되고, 격리가 자동 원복
     트리거가 아니다(짝 UNISOLATE가 HUMAN_ONLY — ADR-0004).
"""

import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import ValidationError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher  # noqa: E402
import workflows  # noqa: E402
from schemas.backups import BackupType, SgAndTgMappingBackup  # noqa: E402
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
INSTANCE = "i-0abc123456789def0"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{INSTANCE}"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/vigilantis-smoke-tg/0123456789abcdef"
ISOLATION_SG = "sg-0150150150150150a"
PRIMARY_ENI = "eni-0aaa111122223333a"
SECONDARY_ENI = "eni-0bbb444455556666b"
WEB_SG = "sg-0aaa111122223333a"
SSH_SG = "sg-0aaa444455556666a"
DB_SG = "sg-0bbb111122223333b"
PORT = 8080  # TG 기본 포트(80)가 아닌 등록 — 백업이 포트를 담아야 하는 이유

OK = {"ResponseMetadata": {"RequestId": "req-ok"}}


def client_error(code: str, status: int | None = 400) -> ClientError:
    metadata: dict = {"RequestId": "req-err"}
    if status is not None:
        metadata["HTTPStatusCode"] = status
    return ClientError(
        {"Error": {"Code": code, "Message": ""}, "ResponseMetadata": metadata}, "Op"
    )


def eni(eni_id: str, device_index: int, *groups: str) -> dict:
    return {
        "NetworkInterfaceId": eni_id,
        "Attachment": {"DeviceIndex": device_index},
        "Groups": [{"GroupId": group, "GroupName": group} for group in groups],
    }


def instance_with(*interfaces: dict) -> dict:
    return {"Reservations": [{"Instances": [{"InstanceId": INSTANCE, "NetworkInterfaces": list(interfaces)}]}]}


# 보조 ENI를 앞에 둔다 — 캡처가 응답 순서가 아니라 DeviceIndex로 주 ENI를 고르는지 본다
TWO_ENIS = instance_with(
    eni(SECONDARY_ENI, 1, DB_SG),
    eni(PRIMARY_ENI, 0, WEB_SG, SSH_SG),
)


def health(*entries: tuple[int, str, str | None]) -> dict:
    return {
        "TargetHealthDescriptions": [
            {
                "Target": {"Id": INSTANCE, "Port": port},
                "TargetHealth": {"State": state, **({"Reason": reason} if reason else {})},
            }
            for port, state, reason in entries
        ]
    }


REGISTERED = health((PORT, "healthy", None))
NOT_REGISTERED = health((80, "unused", "Target.NotRegistered"))


class FakeAws:
    """ec2·elbv2를 한 기록에 모은다 — 서비스를 넘나드는 **순서**가 검증 대상이다."""

    def __init__(self, service: str, state: dict):
        self._service = service
        self._state = state

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((f"{self._service}.{operation}", kwargs))
            key = f"{self._service}.{operation}"
            outcome = self._state["overrides"].get(key)
            if isinstance(outcome, list):
                outcome = outcome.pop(0) if outcome else None
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if key == "ec2.describe_instances":
                return TWO_ENIS
            if key == "elbv2.describe_target_health":
                return REGISTERED
            return OK

        return call


@pytest.fixture
def aws(monkeypatch):
    state = {"overrides": {}, "calls": []}

    def factory(service, region=None, **_):
        assert region == REGION
        return FakeAws(service, state)

    monkeypatch.setattr(ex, "aws_client", factory)
    monkeypatch.setattr(bk, "aws_client", factory)

    def configure(**overrides):
        # 키는 "ec2.describe_instances"처럼 서비스.작업 — 키워드 인자로 못 쓰므로 dict로 받는다
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    configure.set = lambda key, value: state["overrides"].__setitem__(key, value)
    return configure


def operations(aws) -> list[str]:
    return [name for name, _ in aws.calls]


def changes(aws) -> list[str]:
    """자산을 바꾸는 호출만 — 조회를 빼고 본다."""
    return [name for name in operations(aws) if ".describe_" not in name]


def mapping(**overrides) -> SgAndTgMappingBackup:
    values = {
        "security_group_ids": [WEB_SG, SSH_SG],
        "target_group_arn": TG_ARN,
        "target_port": PORT,
        "network_interfaces": [
            {"network_interface_id": PRIMARY_ENI, "security_group_ids": [WEB_SG, SSH_SG]},
            {"network_interface_id": SECONDARY_ENI, "security_group_ids": [DB_SG]},
        ],
    }
    values.update(overrides)
    return SgAndTgMappingBackup(**values)


# ============================================================ ① 백업 캡처 (B1)


def capture(aws):
    return bk.capture_sg_and_tg_mapping(INSTANCE, REGION, target_group_arn=TG_ARN)


def test_capture_holds_every_eni_in_device_order_and_the_registered_port(aws):
    result = capture(aws)

    assert result.captured
    assert result.backup_type == BackupType.SAVE_CURRENT_SG_AND_TG_MAPPING.value
    assert result.payload == {
        "security_group_ids": [WEB_SG, SSH_SG],  # 주 ENI(DeviceIndex 0)의 SG
        "target_group_arn": TG_ARN,
        "target_port": PORT,
        "network_interfaces": [
            {"network_interface_id": PRIMARY_ENI, "security_group_ids": [WEB_SG, SSH_SG]},
            {"network_interface_id": SECONDARY_ENI, "security_group_ids": [DB_SG]},
        ],
    }
    # 등록 조회는 파라미터의 TG로, 이 인스턴스로 한정해서 묻는다
    (_, kwargs), = [c for c in aws.calls if c[0] == "elbv2.describe_target_health"]
    assert kwargs == {"TargetGroupArn": TG_ARN, "Targets": [{"Id": INSTANCE}]}
    # 캡처는 아무것도 바꾸지 않는다
    assert changes(aws) == []


def test_capture_payload_satisfies_the_unisolate_precheck_keys(aws):
    """가드레일 ④ `_precheck_unisolate`는 `security_group_ids`·`target_group_arn`을 읽는다 —
    캡처가 만든 payload가 그 계약을 그대로 통과해야 원클릭 해제가 막히지 않는다."""
    payload = capture(aws).payload
    assert all(ex._conforms(ex._SG_ID, group) for group in payload["security_group_ids"])
    assert ex._conforms(ex._TG_ARN, payload["target_group_arn"])
    assert ex.sg_tg_mapping_backup(payload) == mapping()


@pytest.mark.parametrize(
    "key, outcome, code",
    [
        ("ec2.describe_instances", client_error("InvalidInstanceID.NotFound"), R.PRECHECK_TARGET_NOT_FOUND),
        ("ec2.describe_instances", EndpointConnectionError(endpoint_url="x"), R.PRECHECK_AWS_ERROR),
        ("ec2.describe_instances", instance_with(), R.PRECHECK_INVALID_STATE),
        ("elbv2.describe_target_health", client_error("TargetGroupNotFound"), R.PRECHECK_TARGET_NOT_FOUND),
        ("elbv2.describe_target_health", NOT_REGISTERED, R.PRECHECK_TARGET_NOT_FOUND),
        (
            "elbv2.describe_target_health",
            health((80, "healthy", None), (PORT, "healthy", None)),
            R.PRECHECK_INVALID_STATE,
        ),
    ],
    ids=[
        "instance-missing",
        "instance-unreachable",
        "no-eni",
        "tg-missing",
        "not-registered",
        "registered-on-two-ports",
    ],
)
def test_capture_fails_instead_of_recording_a_backup_it_cannot_restore_from(
    aws, key, outcome, code
):
    aws.set(key, outcome)
    result = capture(aws)
    assert not result.captured
    assert result.payload is None
    assert result.reason_code is code
    assert changes(aws) == []


def test_capture_fails_when_an_eni_has_no_security_group(aws):
    """빈 SG 목록으로 원복하면 modify_network_interface_attribute가 거절한다 — 캡처에서 막는다."""
    aws.set("ec2.describe_instances", instance_with(eni(PRIMARY_ENI, 0)))
    result = capture(aws)
    assert not result.captured
    assert result.reason_code is R.PRECHECK_INVALID_STATE


# ============================================================ ② 격리 (B2)


def isolate(aws, **kwargs):
    recorded: list = []
    outcome = ex.execute_ec2_isolate(
        INSTANCE_ARN,
        backup=kwargs.pop("backup", mapping()),
        isolation_group_id=kwargs.pop("isolation_group_id", ISOLATION_SG),
        record_step=recorded.append,
    )
    return outcome, recorded


def test_isolate_deregisters_first_then_swaps_every_eni(aws):
    outcome, recorded = isolate(aws)

    assert outcome.succeeded
    assert operations(aws) == [
        "elbv2.deregister_targets",
        "ec2.modify_network_interface_attribute",
        "ec2.modify_network_interface_attribute",
    ]
    deregister, primary, secondary = (kwargs for _, kwargs in aws.calls)
    # 해제는 백업의 등록 그대로 — 포트를 빼면 TG 기본 포트 등록을 찾는다
    assert deregister == {"TargetGroupArn": TG_ARN, "Targets": [{"Id": INSTANCE, "Port": PORT}]}
    assert primary == {"NetworkInterfaceId": PRIMARY_ENI, "Groups": [ISOLATION_SG]}
    assert secondary == {"NetworkInterfaceId": SECONDARY_ENI, "Groups": [ISOLATION_SG]}

    assert [(s.sequence, s.step_type, s.status, s.effect) for s in outcome.steps] == [
        (1, ex.STEP_DEREGISTER_TARGET, S.SUCCESS, E.APPLIED),
        (2, ex.STEP_ISOLATE_ENI_SECURITY_GROUPS, S.SUCCESS, E.APPLIED),
    ]
    # 호출 직전 IN_PROGRESS → 종료 결과 순서로 기록된다(회수 규약 — ADR-0008 §7)
    assert [s.status for s in recorded] == [S.IN_PROGRESS, S.SUCCESS, S.IN_PROGRESS, S.SUCCESS]
    assert all(s.affected_arn == INSTANCE_ARN for s in outcome.steps)


def test_isolate_rejected_deregistration_changes_nothing(aws):
    aws.set("elbv2.deregister_targets", client_error("InvalidTarget"))
    outcome, _ = isolate(aws)

    assert not outcome.succeeded
    assert operations(aws) == ["elbv2.deregister_targets"]  # SG는 건드리지 않는다
    (step,) = outcome.steps
    assert (step.step_type, step.status, step.effect) == (
        ex.STEP_DEREGISTER_TARGET, S.FAILED, E.NOT_APPLIED,
    )


def test_isolate_failing_after_one_eni_changed_is_partial(aws):
    aws.set(
        "ec2.modify_network_interface_attribute",
        [OK, client_error("InvalidNetworkInterfaceID.NotFound")],
    )
    outcome, _ = isolate(aws)

    assert not outcome.succeeded
    deregister, swap = outcome.steps
    assert deregister.effect is E.APPLIED
    # 마지막 예외는 4xx지만 주 ENI는 이미 격리 SG로 바뀌었다 — NOT_APPLIED가 아니다
    assert (swap.step_type, swap.status, swap.effect) == (
        ex.STEP_ISOLATE_ENI_SECURITY_GROUPS, S.FAILED, E.PARTIAL,
    )
    assert PRIMARY_ENI in swap.error_summary and SECONDARY_ENI in swap.error_summary


def test_isolate_failing_on_the_first_eni_keeps_the_error_classification(aws):
    """한 ENI도 안 바뀌었으면 PARTIAL이 아니다 — 단계 effect는 예외 분류 그대로다.
    (TG 해제는 이미 적용됐으므로 실행 전체로는 자산이 바뀐 채 끝난 것이다.)"""
    aws.set("ec2.modify_network_interface_attribute", client_error("UnauthorizedOperation", 403))
    outcome, _ = isolate(aws)

    deregister, swap = outcome.steps
    assert deregister.effect is E.APPLIED
    assert (swap.status, swap.effect) == (S.FAILED, E.NOT_APPLIED)
    assert operations(aws).count("ec2.modify_network_interface_attribute") == 1


def test_isolate_server_error_on_the_first_eni_is_unknown(aws):
    aws.set("ec2.modify_network_interface_attribute", client_error("InternalError", 500))
    outcome, _ = isolate(aws)
    assert outcome.steps[-1].effect is E.UNKNOWN


@pytest.mark.parametrize(
    "target_arn, isolation_group_id",
    [
        (f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{WEB_SG}", ISOLATION_SG),
        (INSTANCE_ARN, "not-a-group"),
    ],
    ids=["not-an-instance", "bad-isolation-group"],
)
def test_isolate_rejects_before_touching_aws(aws, target_arn, isolation_group_id):
    outcome = ex.execute_ec2_isolate(
        target_arn, backup=mapping(), isolation_group_id=isolation_group_id
    )
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert outcome.steps == ()
    assert aws.calls == []


# ============================================================ ③ 격리 해제 (B3)


def unisolate(aws, backup=None):
    recorded: list = []
    outcome = ex.execute_ec2_unisolate(
        INSTANCE_ARN, backup=backup or mapping(), record_step=recorded.append
    )
    return outcome, recorded


ISOLATED = instance_with(
    eni(PRIMARY_ENI, 0, ISOLATION_SG),
    eni(SECONDARY_ENI, 1, ISOLATION_SG),
)


def test_unisolate_restores_each_eni_from_the_backup_then_reregisters_on_its_port(aws):
    aws.set("ec2.describe_instances", ISOLATED)
    outcome, recorded = unisolate(aws)

    assert outcome.succeeded
    assert changes(aws) == [
        "ec2.modify_network_interface_attribute",
        "ec2.modify_network_interface_attribute",
        "elbv2.register_targets",
    ]
    primary, secondary, register = (
        kwargs for name, kwargs in aws.calls if ".describe_" not in name
    )
    # ENI마다 **그 ENI의** 원래 SG — 주 ENI 목록을 보조 ENI에 붓지 않는다
    assert primary == {"NetworkInterfaceId": PRIMARY_ENI, "Groups": [WEB_SG, SSH_SG]}
    assert secondary == {"NetworkInterfaceId": SECONDARY_ENI, "Groups": [DB_SG]}
    assert register == {"TargetGroupArn": TG_ARN, "Targets": [{"Id": INSTANCE, "Port": PORT}]}
    assert [(s.sequence, s.step_type, s.effect) for s in outcome.steps] == [
        (1, ex.STEP_RESTORE_ENI_SECURITY_GROUPS, E.APPLIED),
        (2, ex.STEP_REGISTER_TARGET, E.APPLIED),
    ]
    # 진행하는 경우 대조는 단계로 남지 않는다(STEP_COMPARE_NETWORK_INTERFACES 주석)
    assert ex.STEP_COMPARE_NETWORK_INTERFACES not in {s.step_type for s in recorded}


def test_unisolate_values_come_only_from_the_backup(aws):
    """현물에 무엇이 붙어 있든 복원 값은 백업이다(ADR-0004 정책 ③). 현물의 SG를 다시
    읽어 붓는 구현이면 격리 SG가 그대로 남는다."""
    aws.set("ec2.describe_instances", ISOLATED)
    other = mapping(
        security_group_ids=[DB_SG],
        target_port=9090,
        network_interfaces=[
            {"network_interface_id": PRIMARY_ENI, "security_group_ids": [DB_SG]},
            {"network_interface_id": SECONDARY_ENI, "security_group_ids": [WEB_SG]},
        ],
    )
    unisolate(aws, other)
    groups = [kw["Groups"] for name, kw in aws.calls if name.endswith("modify_network_interface_attribute")]
    assert groups == [[DB_SG], [WEB_SG]]
    (register,) = [kw for name, kw in aws.calls if name == "elbv2.register_targets"]
    assert register["Targets"] == [{"Id": INSTANCE, "Port": 9090}]


def test_unisolate_refuses_when_a_backup_eni_is_no_longer_on_the_instance(aws):
    """보조 ENI는 떼어 다른 인스턴스에 붙일 수 있다 — ENI ID로만 바꾸면 남의 SG를 바꾼다."""
    aws.set("ec2.describe_instances", instance_with(eni(PRIMARY_ENI, 0, ISOLATION_SG)))
    outcome, _ = unisolate(aws)

    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert changes(aws) == []
    (step,) = outcome.steps
    assert (step.step_type, step.status, step.effect) == (
        ex.STEP_COMPARE_NETWORK_INTERFACES, S.SUCCESS, E.NOT_APPLIED,
    )
    assert SECONDARY_ENI in outcome.error_summary


def test_unisolate_defers_when_it_cannot_look_at_the_instance(aws):
    aws.set("ec2.describe_instances", EndpointConnectionError(endpoint_url="x"))
    outcome, recorded = unisolate(aws)
    assert outcome.deferred
    assert outcome.steps == () and recorded == []
    assert changes(aws) == []


def test_unisolate_without_the_instance_fails_without_steps(aws):
    aws.set("ec2.describe_instances", client_error("InvalidInstanceID.NotFound"))
    outcome, _ = unisolate(aws)
    assert outcome.reason_code is R.PRECHECK_TARGET_NOT_FOUND
    assert not outcome.deferred and outcome.steps == ()


def test_unisolate_partial_restore_stops_before_reregistering(aws):
    aws.set("ec2.describe_instances", ISOLATED)
    aws.set(
        "ec2.modify_network_interface_attribute",
        [OK, client_error("InvalidGroup.NotFound")],
    )
    outcome, _ = unisolate(aws)

    assert not outcome.succeeded
    (restore,) = outcome.steps
    assert (restore.status, restore.effect) == (S.FAILED, E.PARTIAL)
    # 네트워크 상태가 반쯤인 대상을 서비스에 다시 넣지 않는다
    assert "elbv2.register_targets" not in operations(aws)


def test_unisolate_reregistration_failure_keeps_the_restored_sg_step(aws):
    aws.set("ec2.describe_instances", ISOLATED)
    aws.set("elbv2.register_targets", client_error("InvalidTarget"))
    outcome, _ = unisolate(aws)

    restore, register = outcome.steps
    assert (restore.status, restore.effect) == (S.SUCCESS, E.APPLIED)
    assert (register.step_type, register.status, register.effect) == (
        ex.STEP_REGISTER_TARGET, S.FAILED, E.NOT_APPLIED,
    )


# ============================================================ 판정이 읽는 현물 축


def test_current_eni_security_groups_maps_each_eni(aws):
    found, code = ex.current_eni_security_groups(INSTANCE, REGION)
    assert code is None
    assert found == {PRIMARY_ENI: [WEB_SG, SSH_SG], SECONDARY_ENI: [DB_SG]}


@pytest.mark.parametrize(
    "response, expected",
    [
        (REGISTERED, "healthy"),
        (health((PORT, "draining", "Target.DeregistrationInProgress")), "draining"),
        (health((PORT, "unused", "Target.NotRegistered")), None),
        (health((80, "healthy", None)), None),  # 다른 포트의 등록은 이 등록이 아니다
    ],
    ids=["registered", "draining", "not-registered", "other-port"],
)
def test_current_target_registration_reads_only_the_backed_up_port(aws, response, expected):
    aws.set("elbv2.describe_target_health", response)
    state, code = ex.current_target_registration(TG_ARN, INSTANCE, PORT, REGION)
    assert code is None
    assert state == expected


def test_sg_tg_mapping_backup_rejects_payload_outside_the_contract():
    assert ex.sg_tg_mapping_backup(mapping().model_dump(mode="json")) == mapping()
    # 롤백 precheck가 읽던 두 키만 있는 옛 모양 — 포트·ENI 목록이 없어 원복 근거가 못 된다
    assert ex.sg_tg_mapping_backup({"security_group_ids": [WEB_SG], "target_group_arn": TG_ARN}) is None


def test_mapping_contract_ties_the_primary_eni_to_security_group_ids():
    with pytest.raises(ValidationError):
        mapping(security_group_ids=[DB_SG])  # 주 ENI의 SG가 아니다
    with pytest.raises(ValidationError):
        mapping(
            network_interfaces=[
                {"network_interface_id": PRIMARY_ENI, "security_group_ids": [WEB_SG, SSH_SG]},
                {"network_interface_id": PRIMARY_ENI, "security_group_ids": [DB_SG]},
            ]
        )
    with pytest.raises(ValidationError):
        mapping(target_port=0)


# ============================================================ ⑤ 디스패치 등록


def test_isolate_and_unisolate_are_registered_as_runner_and_judge_pairs():
    """runner만 등록하면 실행 도중 끊긴 격리가 재실행도 종료도 되지 않고 IN_PROGRESS에
    남는다(ADR-0008 §6)."""
    for runbook, runner, judge in (
        (RunbookId.RUNBOOK_EC2_ISOLATE, workflows.run_ec2_isolate_execution, workflows.judge_ec2_isolate),
        (RunbookId.RUNBOOK_EC2_UNISOLATE, workflows.run_ec2_unisolate_execution, workflows.judge_ec2_unisolate),
    ):
        assert dispatcher._RUNNERS[runbook] is runner
        assert dispatcher._JUDGES[runbook] is judge
    assert set(dispatcher._RUNNERS) == set(dispatcher._JUDGES)


def test_isolate_is_not_an_auto_rollback_trigger():
    """짝 UNISOLATE는 HUMAN_ONLY다(ADR-0004) — 격리가 자산을 바꾼 채 실패해도 시스템이
    스스로 해제하지 않고 현물 판정으로 간다."""
    assert (
        APPROVAL_MODE_BY_ROLLBACK_ID[RunbookId.RUNBOOK_EC2_UNISOLATE.value]
        is ApprovalMode.HUMAN_ONLY
    )
    assert RunbookId.RUNBOOK_EC2_ISOLATE not in dispatcher._AUTO_ROLLBACK_ON_ASSET_CHANGE
    # 성공의 경계가 실행 반환이다 — 다음 주기 판정을 기다리지 않는다
    assert RunbookId.RUNBOOK_EC2_ISOLATE not in dispatcher._AWAIT_JUDGEMENT_ON_SUCCESS
    assert RunbookId.RUNBOOK_EC2_UNISOLATE not in dispatcher._AWAIT_JUDGEMENT_ON_SUCCESS
