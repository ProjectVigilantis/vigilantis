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
  ⑥ 가짜가 **조용히 넓어지지 않는다**(#449). 모델링한 작업(`MODELED`) 밖을 부르면 그
     테스트가 실패한다 — 실행 함수가 새 AWS 작업을 부르기 시작하면 여기서 먼저 드러나고,
     그 작업의 응답 모양은 실 AWS 실측(AWS_SMOKE_RESULT §8)이 처음 본다.
  ⑦ 백업 캡처가 만든 payload가 **실제 `_precheck_unisolate`를 통과**한다 — 생산처(B1)와
     소비처(가드레일 ④)를 한 테스트로 묶는다. 키 이름만 맞추는 대조로는 값 형식이
     어긋나도 초록이다.
  ⑧ 어느 AWS 호출이 실패해도 실행 함수는 **예외를 던지지 않는다**(executor.py 머리말 규약).

**이 파일이 초록이어도 P2는 실 AWS에서 처음 돈다**(MVP 체크리스트 §2-1 B7 · §4). 스텁은
호출 순서와 분기를 고정할 뿐, AWS 응답 모양·IAM 조건·연결 추적 동작은 보지 못한다.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import ValidationError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatcher
import workflows
from schemas.backups import (
    BackupType,
    ScaleInProtectionBackup,
    ScaleInProtectionResult,
    SgAndTgMappingBackup,
)
from schemas.executions import ExecutionEffect, ExecutionStepStatus
from schemas.precheck import PrecheckReasonCode
from schemas.runbooks import (
    APPROVAL_MODE_BY_ROLLBACK_ID,
    ApprovalMode,
    RunbookId,
)
from services.aws import backup as bk
from services.aws import executor as ex

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
    return {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": INSTANCE, "NetworkInterfaces": list(interfaces)}
                ]
            }
        ]
    }


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
                "TargetHealth": {
                    "State": state,
                    **({"Reason": reason} if reason else {}),
                },
            }
            for port, state, reason in entries
        ]
    }


REGISTERED = health((PORT, "healthy", None))
NOT_REGISTERED = health((80, "unused", "Target.NotRegistered"))


# 가짜가 응답을 아는 작업 — 격리·해제·캡처와 해제 가드레일(_precheck_unisolate)이 부르는 것만.
# 여기 없는 작업을 부르면 즉시 실패한다(⑥). 넓히려면 그 작업의 응답 모양을 근거와 함께 더한다.
MODELED = frozenset(
    {
        "ec2.describe_instances",
        "ec2.describe_security_groups",
        "ec2.modify_network_interface_attribute",
        "elbv2.describe_target_groups",
        "elbv2.describe_target_health",
        "elbv2.deregister_targets",
        "elbv2.register_targets",
        "autoscaling.describe_auto_scaling_groups",
        "autoscaling.describe_auto_scaling_instances",
        "autoscaling.set_instance_protection",
    }
)


class FakeAws:
    """ec2·elbv2를 한 기록에 모은다 — 서비스를 넘나드는 **순서**가 검증 대상이다."""

    def __init__(self, service: str, state: dict):
        self._service = service
        self._state = state

    def __getattr__(self, operation):
        key = f"{self._service}.{operation}"
        if key not in MODELED:
            # 실행 함수가 예외를 삼켜도 놓치지 않게 기록해 두고, fixture 정리에서 실패시킨다
            self._state["unmodeled"].append(key)
            raise NotImplementedError(f"가짜 AWS가 모델링하지 않은 작업: {key}")

        def call(**kwargs):
            self._state["calls"].append((key, kwargs))
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
    state = {"overrides": {}, "calls": [], "unmodeled": []}

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
    yield configure
    assert state["unmodeled"] == [], (
        f"모델링하지 않은 AWS 작업 호출: {state['unmodeled']}"
    )


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
            {
                "network_interface_id": PRIMARY_ENI,
                "security_group_ids": [WEB_SG, SSH_SG],
            },
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
            {
                "network_interface_id": PRIMARY_ENI,
                "security_group_ids": [WEB_SG, SSH_SG],
            },
            {"network_interface_id": SECONDARY_ENI, "security_group_ids": [DB_SG]},
        ],
        # ASG 태그가 없는 대상 — 보호 관측은 ASG를 조회하지 않고 NOT_MANAGED로 남는다
        "scale_in_protection": {
            "status": "NOT_MANAGED",
            "asg_name": None,
            "previous_protected": None,
            "autoscaling_execution_id": None,
            "reason": "NOT_ASG_MEMBER",
        },
    }
    # 등록 조회는 파라미터의 TG로, 이 인스턴스로 한정해서 묻는다
    ((_, kwargs),) = [c for c in aws.calls if c[0] == "elbv2.describe_target_health"]
    assert kwargs == {"TargetGroupArn": TG_ARN, "Targets": [{"Id": INSTANCE}]}
    # 캡처는 아무것도 바꾸지 않는다
    assert changes(aws) == []


def test_capture_payload_satisfies_the_unisolate_precheck_keys(aws):
    """가드레일 ④ `_precheck_unisolate`는 `security_group_ids`·`target_group_arn`을 읽는다 —
    캡처가 만든 payload가 그 계약을 그대로 통과해야 원클릭 해제가 막히지 않는다."""
    payload = capture(aws).payload
    assert all(
        ex._conforms(ex._SG_ID, group) for group in payload["security_group_ids"]
    )
    assert ex._conforms(ex._TG_ARN, payload["target_group_arn"])
    assert ex.sg_tg_mapping_backup(payload) == mapping(
        scale_in_protection=ScaleInProtectionBackup(
            status="NOT_MANAGED", reason="NOT_ASG_MEMBER"
        )
    )


@pytest.mark.parametrize(
    "key, outcome, code",
    [
        (
            "ec2.describe_instances",
            client_error("InvalidInstanceID.NotFound"),
            R.PRECHECK_TARGET_NOT_FOUND,
        ),
        (
            "ec2.describe_instances",
            EndpointConnectionError(endpoint_url="x"),
            R.PRECHECK_AWS_ERROR,
        ),
        ("ec2.describe_instances", instance_with(), R.PRECHECK_INVALID_STATE),
        (
            "elbv2.describe_target_health",
            client_error("TargetGroupNotFound"),
            R.PRECHECK_TARGET_NOT_FOUND,
        ),
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
    assert deregister == {
        "TargetGroupArn": TG_ARN,
        "Targets": [{"Id": INSTANCE, "Port": PORT}],
    }
    assert primary == {"NetworkInterfaceId": PRIMARY_ENI, "Groups": [ISOLATION_SG]}
    assert secondary == {"NetworkInterfaceId": SECONDARY_ENI, "Groups": [ISOLATION_SG]}

    assert [(s.sequence, s.step_type, s.status, s.effect) for s in outcome.steps] == [
        (1, ex.STEP_DEREGISTER_TARGET, S.SUCCESS, E.APPLIED),
        (2, ex.STEP_ISOLATE_ENI_SECURITY_GROUPS, S.SUCCESS, E.APPLIED),
    ]
    # 호출 직전 IN_PROGRESS → 종료 결과 순서로 기록된다(회수 규약 — ADR-0008 §7)
    assert [s.status for s in recorded] == [
        S.IN_PROGRESS,
        S.SUCCESS,
        S.IN_PROGRESS,
        S.SUCCESS,
    ]
    assert all(s.affected_arn == INSTANCE_ARN for s in outcome.steps)


def test_isolate_rejected_deregistration_changes_nothing(aws):
    aws.set("elbv2.deregister_targets", client_error("InvalidTarget"))
    outcome, _ = isolate(aws)

    assert not outcome.succeeded
    assert operations(aws) == ["elbv2.deregister_targets"]  # SG는 건드리지 않는다
    (step,) = outcome.steps
    assert (step.step_type, step.status, step.effect) == (
        ex.STEP_DEREGISTER_TARGET,
        S.FAILED,
        E.NOT_APPLIED,
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
        ex.STEP_ISOLATE_ENI_SECURITY_GROUPS,
        S.FAILED,
        E.PARTIAL,
    )
    assert PRIMARY_ENI in swap.error_summary and SECONDARY_ENI in swap.error_summary


def test_isolate_failing_on_the_first_eni_keeps_the_error_classification(aws):
    """한 ENI도 안 바뀌었으면 PARTIAL이 아니다 — 단계 effect는 예외 분류 그대로다.
    (TG 해제는 이미 적용됐으므로 실행 전체로는 자산이 바뀐 채 끝난 것이다.)"""
    aws.set(
        "ec2.modify_network_interface_attribute",
        client_error("UnauthorizedOperation", 403),
    )
    outcome, _ = isolate(aws)

    deregister, swap = outcome.steps
    assert deregister.effect is E.APPLIED
    assert (swap.status, swap.effect) == (S.FAILED, E.NOT_APPLIED)
    assert operations(aws).count("ec2.modify_network_interface_attribute") == 1


def test_isolate_server_error_on_the_first_eni_is_unknown(aws):
    aws.set(
        "ec2.modify_network_interface_attribute", client_error("InternalError", 500)
    )
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
    assert register == {
        "TargetGroupArn": TG_ARN,
        "Targets": [{"Id": INSTANCE, "Port": PORT}],
    }
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
    groups = [
        kw["Groups"]
        for name, kw in aws.calls
        if name.endswith("modify_network_interface_attribute")
    ]
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
        ex.STEP_COMPARE_NETWORK_INTERFACES,
        S.SUCCESS,
        E.NOT_APPLIED,
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
        ex.STEP_REGISTER_TARGET,
        S.FAILED,
        E.NOT_APPLIED,
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
def test_current_target_registration_reads_only_the_backed_up_port(
    aws, response, expected
):
    aws.set("elbv2.describe_target_health", response)
    state, code = ex.current_target_registration(TG_ARN, INSTANCE, PORT, REGION)
    assert code is None
    assert state == expected


def test_sg_tg_mapping_backup_rejects_payload_outside_the_contract():
    assert ex.sg_tg_mapping_backup(mapping().model_dump(mode="json")) == mapping()
    # 롤백 precheck가 읽던 두 키만 있는 옛 모양 — 포트·ENI 목록이 없어 원복 근거가 못 된다
    assert (
        ex.sg_tg_mapping_backup(
            {"security_group_ids": [WEB_SG], "target_group_arn": TG_ARN}
        )
        is None
    )


def test_mapping_contract_ties_the_primary_eni_to_security_group_ids():
    with pytest.raises(ValidationError):
        mapping(security_group_ids=[DB_SG])  # 주 ENI의 SG가 아니다
    with pytest.raises(ValidationError):
        mapping(
            network_interfaces=[
                {
                    "network_interface_id": PRIMARY_ENI,
                    "security_group_ids": [WEB_SG, SSH_SG],
                },
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
        (
            RunbookId.RUNBOOK_EC2_ISOLATE,
            workflows.run_ec2_isolate_execution,
            workflows.judge_ec2_isolate,
        ),
        (
            RunbookId.RUNBOOK_EC2_UNISOLATE,
            workflows.run_ec2_unisolate_execution,
            workflows.judge_ec2_unisolate,
        ),
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
    assert (
        RunbookId.RUNBOOK_EC2_ISOLATE not in dispatcher._AUTO_ROLLBACK_ON_ASSET_CHANGE
    )
    # 성공의 경계가 실행 반환이다 — 다음 주기 판정을 기다리지 않는다
    assert RunbookId.RUNBOOK_EC2_ISOLATE not in dispatcher._AWAIT_JUDGEMENT_ON_SUCCESS
    assert RunbookId.RUNBOOK_EC2_UNISOLATE not in dispatcher._AWAIT_JUDGEMENT_ON_SUCCESS


# ============================================================ ⑥ 가짜의 경계 (#449)


def test_the_fake_fails_on_an_operation_it_does_not_model():
    """모델링하지 않은 작업은 기본 응답(OK)으로 흘려보내지 않는다. 흘려보내면 실행 함수가
    새 AWS 작업을 부르기 시작해도 초록이고, 그 작업은 실 AWS에서 처음 돈다."""
    state = {"overrides": {}, "calls": [], "unmodeled": []}
    with pytest.raises(NotImplementedError):
        _ = FakeAws("ec2", state).terminate_instances
    with pytest.raises(NotImplementedError):
        _ = FakeAws("autoscaling", state).create_auto_scaling_group
    assert state["unmodeled"] == [
        "ec2.terminate_instances",
        "autoscaling.create_auto_scaling_group",
    ]
    assert state["calls"] == []


# ============================================================ ⑦ 백업 → 가드레일 ④ 왕복 (#449)

VPC = "vpc-0abc123456789def0"


def in_vpc(response: dict) -> dict:
    response["Reservations"][0]["Instances"][0]["VpcId"] = VPC
    return response


class OneRecordLoader:
    """백업 레코드 1건짜리 조회 — UNISOLATE는 `backup_record_id`로만 찾는다."""

    def __init__(self, record: ex.BackupRecordView):
        self._record = record

    def get(self, backup_record_id):
        return (
            self._record if backup_record_id == self._record.backup_record_id else None
        )

    def latest_for_target(self, *_args, **_kwargs):
        raise AssertionError("UNISOLATE는 대상 기준 최신 백업으로 찾지 않는다")


def unisolate_precheck_with(captured) -> "ex.PrecheckOutcome":
    record = ex.BackupRecordView(
        backup_record_id="bk-1",
        target_arn=INSTANCE_ARN,
        backup_type=captured.backup_type,
        payload=captured.payload,
    )
    return ex.precheck(
        RunbookId.RUNBOOK_EC2_UNISOLATE,
        INSTANCE_ARN,
        {"instance_id": INSTANCE, "backup_record_id": "bk-1", "evidence_id": "ev-1"},
        backup_loader=OneRecordLoader(record),
    )


def test_captured_payload_passes_the_real_unisolate_precheck(aws):
    """캡처(B1)가 저장한 payload 그대로 가드레일 ④를 통과해야 원클릭 해제가 선다.
    키 이름 대조(위 ①)와 달리 **실제 `_precheck_unisolate`** 가 읽는다."""
    aws.set(
        "ec2.describe_instances",
        in_vpc(
            instance_with(
                eni(SECONDARY_ENI, 1, DB_SG),
                eni(PRIMARY_ENI, 0, WEB_SG, SSH_SG),
            )
        ),
    )
    captured = capture(aws)
    assert captured.captured

    # 해제 직전 — 두 ENI 모두 격리 SG다. 보조 ENI가 응답 앞에 온다
    aws.set(
        "ec2.describe_instances",
        in_vpc(
            instance_with(
                eni(SECONDARY_ENI, 1, ISOLATION_SG),
                eni(PRIMARY_ENI, 0, ISOLATION_SG),
            )
        ),
    )
    aws.set(
        "ec2.modify_network_interface_attribute", client_error("DryRunOperation", 412)
    )
    aws.set(
        "elbv2.describe_target_groups",
        {"TargetGroups": [{"TargetGroupArn": TG_ARN, "VpcId": VPC}]},
    )
    outcome = unisolate_precheck_with(captured)

    assert outcome.passed, outcome
    # DryRun은 **주 ENI**(DeviceIndex 0)에 **주 ENI의 원래 SG**를 댄다 — 응답 순서가 아니다
    (dry_run,) = [
        kw for name, kw in aws.calls if name == "ec2.modify_network_interface_attribute"
    ]
    assert dry_run == {
        "DryRun": True,
        "NetworkInterfaceId": PRIMARY_ENI,
        "Groups": [WEB_SG, SSH_SG],
    }
    (exists,) = [kw for name, kw in aws.calls if name == "ec2.describe_security_groups"]
    assert exists == {"GroupIds": [WEB_SG, SSH_SG]}


def test_unisolate_precheck_refuses_a_target_group_in_another_vpc(aws):
    """왕복의 반대쪽 — 캡처 뒤 TG가 다른 VPC 것으로 보이면 해제를 시작하지 않는다."""
    aws.set(
        "ec2.describe_instances", in_vpc(instance_with(eni(PRIMARY_ENI, 0, WEB_SG)))
    )
    captured = capture(aws)
    aws.set(
        "ec2.modify_network_interface_attribute", client_error("DryRunOperation", 412)
    )
    aws.set(
        "elbv2.describe_target_groups",
        {
            "TargetGroups": [
                {"TargetGroupArn": TG_ARN, "VpcId": "vpc-0fff000011112222f"}
            ]
        },
    )
    outcome = unisolate_precheck_with(captured)
    assert (outcome.passed, outcome.reason_code) == (False, R.PRECHECK_INVALID_STATE)


def test_unisolate_puts_back_exactly_what_the_capture_saw(aws):
    """캡처 → 격리 → 해제 한 바퀴. 해제가 복원한 ENI별 SG와 재등록 좌표가 캡처 시점의
    현물과 같아야 한다 — 격리가 바꾼 집합과 해제가 되돌리는 집합이 같다(T3-7)."""
    before = {
        e["NetworkInterfaceId"]: [g["GroupId"] for g in e["Groups"]]
        for e in TWO_ENIS["Reservations"][0]["Instances"][0]["NetworkInterfaces"]
    }
    backup = ex.sg_tg_mapping_backup(capture(aws).payload)
    assert backup is not None

    isolated, _ = isolate(aws, backup=backup)
    assert isolated.succeeded
    aws.set("ec2.describe_instances", ISOLATED)
    start = len(aws.calls)
    restored, _ = unisolate(aws, backup)
    assert restored.succeeded

    after = {
        kw["NetworkInterfaceId"]: kw["Groups"]
        for name, kw in aws.calls[start:]
        if name == "ec2.modify_network_interface_attribute"
    }
    assert after == before
    (deregister,) = [kw for name, kw in aws.calls if name == "elbv2.deregister_targets"]
    (register,) = [kw for name, kw in aws.calls if name == "elbv2.register_targets"]
    assert register == deregister


# ============================================================ ⑧ 예외를 던지지 않는다 (#449)

FAILURES = [
    pytest.param(client_error("AccessDenied", 403), id="4xx"),
    pytest.param(client_error("InternalFailure", 500), id="5xx"),
    pytest.param(client_error("Throttling", None), id="no-status"),
    pytest.param(EndpointConnectionError(endpoint_url="x"), id="unreachable"),
]


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize(
    "operation", ["elbv2.deregister_targets", "ec2.modify_network_interface_attribute"]
)
def test_isolate_never_raises(aws, operation, failure):
    aws.set(operation, failure)
    outcome, _ = isolate(aws)
    assert not outcome.succeeded
    assert outcome.reason_code is not None


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize(
    "operation",
    [
        "ec2.describe_instances",
        "ec2.modify_network_interface_attribute",
        "elbv2.register_targets",
    ],
)
def test_unisolate_never_raises(aws, operation, failure):
    aws.set("ec2.describe_instances", ISOLATED)
    aws.set(operation, failure)
    outcome, _ = unisolate(aws)
    assert not outcome.succeeded
    assert outcome.reason_code is not None


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize(
    "operation", ["ec2.describe_instances", "elbv2.describe_target_health"]
)
def test_capture_never_raises(aws, operation, failure):
    aws.set(operation, failure)
    result = capture(aws)
    assert not result.captured
    assert result.reason_code is not None


# ============================================================ ⑨ ASG scale-in 보호 (보조 조치)
#
# 보호는 TG/ENI 격리 성공을 바꾸지 않는 **별도 결과**다. 같은 인스턴스의 생존은 보장하지 않으며
# (진행 중인 scale-in·교체·수동 삭제는 못 막는다) 유예 시간도 가정하지 않는다.

from schemas.autoscaling import (
    EXECUTION_TAG_KEY,
    SCALING_POLICY_NAME,
    SOURCE_INSTANCE_TAG_KEY,
    AutoscalingExecutionPlan,
    autoscaling_image_name,
)

ASG_SOURCE = "i-0fed000011112222f"
ASG_EXECUTION = "3f1c2b8a-0d5e-4c1a-9b7e-2a6f8d4c1e90"
ASG = f"vigilantis-asg-{ASG_SOURCE}"
OTHER_ASG = "someone-elses-asg"


def build_plan(**overrides) -> AutoscalingExecutionPlan:
    values = {
        "target_arn": f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{ASG_SOURCE}",
        "execution_id": ASG_EXECUTION,
        "min_size": 1,
        "max_size": 3,
        "desired_capacity": 1,
        "image_name": autoscaling_image_name(ASG_SOURCE, ASG_EXECUTION),
        "launch_template_name": f"vigilantis-lt-{ASG_SOURCE}",
        "launch_template_version": 1,
        "asg_name": ASG,
        "scaling_policy_name": SCALING_POLICY_NAME,
        "cpu_target_percent": 50,
        "default_instance_warmup_seconds": 300,
        "source": {
            "instance_id": ASG_SOURCE,
            "instance_type": "t3.micro",
            "subnet_id": "subnet-0abc123456789def0",
            "security_group_ids": [WEB_SG],
            "iam_instance_profile_arn": None,
            "ebs_optimized": False,
            "detailed_monitoring": False,
            "dedicated_tenancy": False,
            "associate_public_ip_address": True,
            "metadata_options": {
                "http_tokens": "required",
                "http_endpoint": "enabled",
                "http_put_response_hop_limit": 2,
                "http_protocol_ipv6": "disabled",
                "instance_metadata_tags": "disabled",
            },
            "preserved_tags": [{"key": "vigilantis:smoke", "value": "true"}],
        },
    }
    values.update(overrides)
    return AutoscalingExecutionPlan.model_validate(values)


ASG_TAGS = [
    {"Key": EXECUTION_TAG_KEY, "Value": ASG_EXECUTION},
    {"Key": SOURCE_INSTANCE_TAG_KEY, "Value": ASG_SOURCE},
    {"Key": "vigilantis:smoke", "Value": "true"},
]


def asg_member_instance(asg: str = ASG) -> dict:
    response = instance_with(
        eni(PRIMARY_ENI, 0, WEB_SG, SSH_SG), eni(SECONDARY_ENI, 1, DB_SG)
    )
    response["Reservations"][0]["Instances"][0]["Tags"] = [
        {"Key": "aws:autoscaling:groupName", "Value": asg}
    ]
    return response


def asg_groups(
    *, tags=ASG_TAGS, protected=False, members=(INSTANCE,), name=ASG
) -> dict:
    instance = {"LifecycleState": "InService"}
    if protected is not None:
        instance["ProtectedFromScaleIn"] = protected
    return {
        "AutoScalingGroups": [
            {
                "AutoScalingGroupName": name,
                "Tags": tags,
                "Instances": [{"InstanceId": member, **instance} for member in members],
            }
        ]
    }


def asg_instances(
    asg: str = ASG, *, protected: bool = False, member: bool = True
) -> dict:
    if not member:
        return {"AutoScalingInstances": []}
    return {
        "AutoScalingInstances": [
            {
                "InstanceId": INSTANCE,
                "AutoScalingGroupName": asg,
                "ProtectedFromScaleIn": protected,
            }
        ]
    }


MANAGED = ScaleInProtectionBackup(
    status="MANAGED",
    asg_name=ASG,
    previous_protected=False,
    autoscaling_execution_id=ASG_EXECUTION,
)
MANAGED_PROTECTED = MANAGED.model_copy(update={"previous_protected": True})
UNKNOWN = ScaleInProtectionBackup(status="UNKNOWN", reason="LOOKUP_FAILED")


def capture_asg(aws, *, lookup=lambda _: build_plan(), groups=None, **kwargs):
    aws.set("ec2.describe_instances", asg_member_instance())
    aws.set(
        "autoscaling.describe_auto_scaling_groups",
        groups if groups is not None else asg_groups(),
    )
    return bk.capture_sg_and_tg_mapping(
        INSTANCE, REGION, target_group_arn=TG_ARN, plan_lookup=lookup, **kwargs
    )


def protection_of(result) -> dict:
    assert result.captured  # 보조 관측은 SG/TG 캡처의 성패를 바꾸지 않는다
    return result.payload["scale_in_protection"]


def test_capture_records_previous_protection_only_for_verified_managed_member(aws):
    result = capture_asg(aws)
    assert protection_of(result) == {
        "status": "MANAGED",
        "asg_name": ASG,
        "previous_protected": False,
        "autoscaling_execution_id": ASG_EXECUTION,
        "reason": None,
    }
    assert changes(aws) == []  # 캡처는 보호를 쓰지 않는다


@pytest.mark.parametrize("protected", [True, False])
def test_capture_keeps_the_observed_previous_value(aws, protected):
    result = capture_asg(aws, groups=asg_groups(protected=protected))
    assert protection_of(result)["previous_protected"] is protected


@pytest.mark.parametrize(
    "tags, lookup, reason",
    [
        ([], lambda _: build_plan(), "NO_OWNERSHIP_TAGS"),
        (ASG_TAGS, lambda _: None, "NO_SOURCE_PLAN"),
        (
            [t for t in ASG_TAGS if t["Key"] != "vigilantis:smoke"],
            lambda _: build_plan(),
            "OWNERSHIP_MISMATCH",
        ),
        (
            [
                {**t, "Value": "i-0aaa000011112222a"}
                if t["Key"] == SOURCE_INSTANCE_TAG_KEY
                else t
                for t in ASG_TAGS
            ],
            lambda _: build_plan(),
            "OWNERSHIP_MISMATCH",
        ),
        (
            ASG_TAGS,
            lambda _: build_plan(asg_name="vigilantis-asg-other"),
            "OWNERSHIP_MISMATCH",
        ),
    ],
    ids=[
        "no-tags",
        "no-plan",
        "no-smoke-tag",
        "source-mismatch",
        "plan-asg-name-mismatch",
    ],
)
def test_capture_does_not_treat_an_unproven_asg_as_managed(aws, tags, lookup, reason):
    """이름이 비슷하거나 태그만 있다고 우리 스모크 ASG가 아니다 — 소유 근거 셋이 모두 맞아야 한다."""
    result = capture_asg(aws, lookup=lookup, groups=asg_groups(tags=tags))
    assert protection_of(result)["status"] == "NOT_MANAGED"
    assert protection_of(result)["reason"] == reason


def test_capture_without_a_plan_lookup_is_unknown_not_unprotected(aws):
    aws.set("ec2.describe_instances", asg_member_instance())
    aws.set("autoscaling.describe_auto_scaling_groups", asg_groups())
    result = bk.capture_sg_and_tg_mapping(INSTANCE, REGION, target_group_arn=TG_ARN)
    assert protection_of(result)["status"] == "UNKNOWN"
    assert protection_of(result)["previous_protected"] is None


@pytest.mark.parametrize(
    "failure",
    [client_error("AccessDenied", 403), EndpointConnectionError(endpoint_url="x")],
    ids=["denied", "unreachable"],
)
def test_capture_asg_lookup_failure_is_unknown_never_nonmember(aws, failure):
    """SDK 조회 예외를 "ASG 멤버가 아님"으로 읽으면 보호가 필요한 대상이 조용히 빠진다."""
    aws.set("ec2.describe_instances", asg_member_instance())
    aws.set("autoscaling.describe_auto_scaling_groups", failure)
    result = bk.capture_sg_and_tg_mapping(
        INSTANCE, REGION, target_group_arn=TG_ARN, plan_lookup=lambda _: build_plan()
    )
    assert protection_of(result) == {
        "status": "UNKNOWN",
        "asg_name": None,
        "previous_protected": None,
        "autoscaling_execution_id": None,
        "reason": "LOOKUP_FAILED",
    }


def test_capture_plan_lookup_error_is_unknown(aws):
    def boom(_):
        raise RuntimeError("db down")

    assert (
        protection_of(capture_asg(aws, lookup=boom))["reason"] == "PLAN_LOOKUP_FAILED"
    )


@pytest.mark.parametrize(
    "groups, reason",
    [
        ({"AutoScalingGroups": []}, "ASG_NOT_FOUND"),
        (asg_groups(members=("i-0eee000011112222e",)), "MEMBERSHIP_UNVERIFIED"),
        (asg_groups(protected=None), "PRIOR_STATE_UNKNOWN"),
    ],
    ids=["asg-gone", "not-in-instances", "protection-value-missing"],
)
def test_capture_unconfirmable_membership_or_prior_value_is_unknown(
    aws, groups, reason
):
    assert protection_of(capture_asg(aws, groups=groups))["reason"] == reason


def test_old_backup_without_the_field_is_still_a_valid_sg_tg_backup(aws):
    payload = mapping().model_dump(mode="json", exclude={"scale_in_protection"})
    assert "scale_in_protection" not in payload
    restored = ex.sg_tg_mapping_backup(payload)
    assert restored is not None and restored.scale_in_protection is None
    # 옛 백업 = 모름: 보호도 복원도 쓰지 않고 호출도 하지 않는다
    assert ex.protect_scale_in(INSTANCE_ARN, None, record_intent=lambda _: None) is None
    assert (
        ex.restore_scale_in_protection(
            INSTANCE_ARN, restored, parent_result=None, record_intent=lambda _: None
        )
        is None
    )
    assert aws.calls == []


def protect(aws, protection=MANAGED):
    intents: list = []
    result = ex.protect_scale_in(
        INSTANCE_ARN,
        protection,
        record_intent=lambda r: intents.append((len(aws.calls), r)),
    )
    return result, intents


def test_protect_writes_after_checking_membership_and_verifies(aws):
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instances(protected=False), asg_instances(protected=True)],
    )
    result, intents = protect(aws)

    assert operations(aws) == [
        "autoscaling.describe_auto_scaling_instances",
        "autoscaling.set_instance_protection",
        "autoscaling.describe_auto_scaling_instances",
    ]
    ((_, write),) = [c for c in aws.calls if c[0].endswith("set_instance_protection")]
    assert write == {
        "InstanceIds": [INSTANCE],
        "AutoScalingGroupName": ASG,
        "ProtectedFromScaleIn": True,
    }
    assert (
        result.outcome,
        result.reason,
        result.write_may_have_begun,
        result.residual,
    ) == (
        "APPLIED",
        "WRITTEN",
        True,
        False,
    )
    assert result.observed_at.tzinfo is not None
    # 쓰기 직전(조회 1회 뒤)에 "쓰기가 시작됐을 수 있다" 의도가 먼저 기록된다
    ((at, intent),) = intents
    assert at == 1 and intent.write_may_have_begun and intent.outcome == "UNKNOWN"


def test_protection_calls_use_a_single_attempt_client(aws, monkeypatch):
    """SDK가 응답 유실을 재전송하면 우리가 모르는 두 번째 쓰기가 나간다 — 총 시도 1회로 못 박는다."""
    seen: list = []
    original = ex.aws_client

    def spy(service, region=None, **config):
        seen.append((service, config))
        return original(service, region, **config)

    monkeypatch.setattr(ex, "aws_client", spy)
    aws.set(
        "autoscaling.describe_auto_scaling_instances", asg_instances(protected=True)
    )
    protect(aws)
    restore(aws)
    clients = [config for service, config in seen if service == "autoscaling"]
    assert clients and all(c["retries"]["total_max_attempts"] == 1 for c in clients)


@pytest.mark.parametrize(
    "current, outcome, reason",
    [
        (asg_instances(protected=True), "APPLIED", "ALREADY_PROTECTED"),
        (asg_instances(OTHER_ASG, protected=True), "NOT_ATTEMPTED", "MEMBERSHIP_DRIFT"),
        (EndpointConnectionError(endpoint_url="x"), "NOT_ATTEMPTED", "LOOKUP_FAILED"),
    ],
    ids=["still-protected", "moved-asg", "lookup-failed"],
)
def test_already_protected_backup_is_not_reported_as_current_protection(
    aws, current, outcome, reason
):
    """백업의 previous_protected=True는 과거 값이다 — 현재 멤버십·값을 읽어야만 APPLIED다."""
    aws.set("autoscaling.describe_auto_scaling_instances", current)
    result, intents = protect(aws, MANAGED_PROTECTED)
    assert operations(aws) == ["autoscaling.describe_auto_scaling_instances"]
    assert (result.outcome, result.reason) == (outcome, reason)
    assert result.write_may_have_begun is False and intents == []


def test_protect_rewrites_when_the_prior_protection_is_gone(aws):
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instances(protected=False), asg_instances(protected=True)],
    )
    result, _ = protect(aws, MANAGED_PROTECTED)
    assert (
        result.reason == "WRITTEN"
        and "autoscaling.set_instance_protection" in operations(aws)
    )


def test_protect_with_unknown_prior_state_writes_nothing_and_leaves_a_residual(aws):
    result, _ = protect(aws, UNKNOWN)
    assert aws.calls == []
    assert (result.outcome, result.reason, result.residual) == (
        "NOT_ATTEMPTED",
        "PRIOR_STATE_UNKNOWN",
        True,
    )


def test_protect_skips_not_managed_targets_entirely(aws):
    result, _ = protect(
        aws, ScaleInProtectionBackup(status="NOT_MANAGED", reason="NOT_ASG_MEMBER")
    )
    assert result is None and aws.calls == []


@pytest.mark.parametrize(
    "lookup, reason",
    [
        (asg_instances(OTHER_ASG), "MEMBERSHIP_DRIFT"),
        (asg_instances(member=False), "MEMBERSHIP_DRIFT"),
        (EndpointConnectionError(endpoint_url="x"), "LOOKUP_FAILED"),
    ],
    ids=["moved-asg", "left-asg", "lookup-failed"],
)
def test_protect_does_not_write_when_membership_is_not_confirmed(aws, lookup, reason):
    aws.set("autoscaling.describe_auto_scaling_instances", lookup)
    result, intents = protect(aws)
    assert changes(aws) == [] and intents == []
    assert (
        result.outcome,
        result.reason,
        result.residual,
        result.write_may_have_begun,
    ) == (
        "NOT_ATTEMPTED",
        reason,
        True,
        False,
    )


def test_protect_rejected_write_is_a_failed_residual_not_an_exception(aws):
    aws.set(
        "autoscaling.describe_auto_scaling_instances", asg_instances(protected=False)
    )
    aws.set("autoscaling.set_instance_protection", client_error("ValidationError", 400))
    result, _ = protect(aws)
    assert (
        result.outcome,
        result.reason,
        result.residual,
        result.write_may_have_begun,
    ) == (
        "FAILED",
        "WRITE_REJECTED",
        True,
        True,
    )
    assert not result.may_have_changed_protection  # 확정 거절은 복원 근거가 아니다


@pytest.mark.parametrize(
    "verify, outcome, reason",
    [
        (asg_instances(protected=True), "APPLIED", "WRITTEN"),
        (asg_instances(protected=False), "UNKNOWN", "WRITE_UNCERTAIN"),
        (EndpointConnectionError(endpoint_url="x"), "UNKNOWN", "WRITE_UNCERTAIN"),
        (asg_instances(member=False), "UNKNOWN", "MEMBERSHIP_DRIFT"),
    ],
    ids=["landed", "not-visible", "verify-failed", "left-asg"],
)
def test_protect_lost_write_response_is_decided_by_the_verify_read(
    aws, verify, outcome, reason
):
    """응답이 유실되면 쓰기가 닿았는지 모른다 — 확인 조회가 값을 보여 줄 때만 APPLIED다."""
    aws.set(
        "autoscaling.set_instance_protection", EndpointConnectionError(endpoint_url="x")
    )
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instances(protected=False), verify],
    )
    result, _ = protect(aws)
    assert (result.outcome, result.reason, result.write_may_have_begun) == (
        outcome,
        reason,
        True,
    )
    assert result.residual is (outcome != "APPLIED")
    assert len(operations(aws)) == 3  # 조회 → 쓰기 → 확인: 반복 없이 3회로 끝난다
    assert result.may_have_changed_protection


def test_protection_failure_does_not_change_the_core_isolation_outcome(aws):
    isolated, _ = isolate(aws)
    aws.set(
        "autoscaling.describe_auto_scaling_instances", asg_instances(protected=False)
    )
    aws.set("autoscaling.set_instance_protection", client_error("ValidationError", 400))
    result, _ = protect(aws)
    assert isolated.succeeded and result.outcome == "FAILED"
    assert all(
        s.status is S.SUCCESS for s in isolated.steps
    )  # 격리 단계에 보조 결과가 섞이지 않는다


def parent(**overrides):
    """원본 PROTECT 결과 — 기본은 "쓰기가 시작됐을 수 있다"(의도 기록)."""
    values = {
        "phase": "PROTECT",
        "outcome": "UNKNOWN",
        "reason": "WRITE_UNCERTAIN",
        "asg_name": ASG,
        "instance_id": INSTANCE,
        "write_may_have_begun": True,
        "residual": True,
        "observed_at": datetime.now(UTC),
    }
    values.update(overrides)
    return ScaleInProtectionResult(**values)


def restore(aws, protection=MANAGED, *, parent_result=None):
    intents: list = []
    backup = mapping(scale_in_protection=protection)
    result = ex.restore_scale_in_protection(
        INSTANCE_ARN,
        backup,
        parent_result=parent_result if parent_result is not None else parent(),
        record_intent=lambda r: intents.append((len(aws.calls), r)),
    )
    restore.intents = intents
    return result


def test_restore_rereads_core_state_then_writes_only_the_captured_value(aws):
    aws.set("ec2.describe_instances", TWO_ENIS)
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instances(protected=True), asg_instances(protected=False)],
    )
    result = restore(aws)
    assert operations(aws) == [
        "ec2.describe_instances",  # ENI SG를 다시 읽는다
        "elbv2.describe_target_health",  # TG 등록을 다시 읽는다
        "autoscaling.describe_auto_scaling_instances",
        "autoscaling.set_instance_protection",
        "autoscaling.describe_auto_scaling_instances",
    ]
    ((_, write),) = [c for c in aws.calls if c[0].endswith("set_instance_protection")]
    assert (
        write["ProtectedFromScaleIn"] is False and write["AutoScalingGroupName"] == ASG
    )
    assert (result.phase, result.outcome, result.reason) == (
        "RESTORE",
        "APPLIED",
        "RESTORED",
    )
    # 복원도 쓰기 직전에 의도를 남긴다(ASG 조회 1회 뒤 = 호출 3건째)
    ((at, intent),) = restore.intents
    assert (
        at == 3
        and intent.phase == "RESTORE"
        and intent.write_may_have_begun
        and intent.outcome == "UNKNOWN"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"ec2.describe_instances": ISOLATED},  # SG가 아직 격리 SG
        {"elbv2.describe_target_health": NOT_REGISTERED},  # 재등록 안 됨
        {
            "elbv2.describe_target_health": health((PORT, "draining", None))
        },  # 드레이닝은 등록이 아니다
        {"ec2.describe_instances": EndpointConnectionError(endpoint_url="x")},
        {"elbv2.describe_target_health": EndpointConnectionError(endpoint_url="x")},
    ],
    ids=[
        "sg-not-back",
        "not-registered",
        "draining",
        "eni-read-failed",
        "tg-read-failed",
    ],
)
def test_restore_does_not_release_protection_unless_core_state_is_reread_and_restored(
    aws, overrides
):
    for key, value in overrides.items():
        aws.set(key, value)
    result = restore(aws)
    assert not [n for n in operations(aws) if n.startswith("autoscaling.")]
    assert (result.outcome, result.reason, result.residual) == (
        "NOT_ATTEMPTED",
        "CORE_STATE_UNVERIFIED",
        True,
    )
    assert restore.intents == []


@pytest.mark.parametrize(
    "parent_result",
    [
        parent(
            outcome="FAILED", reason="WRITE_REJECTED"
        ),  # 확정 거절: 값을 바꾸지 않았다
        parent(
            outcome="NOT_ATTEMPTED",
            reason="MEMBERSHIP_DRIFT",
            write_may_have_begun=False,
        ),
        parent(
            outcome="APPLIED",
            reason="ALREADY_PROTECTED",
            write_may_have_begun=False,
            residual=False,
        ),
    ],
    ids=["rejected", "never-wrote", "someone-elses-protection"],
)
def test_restore_does_not_undo_protection_we_did_not_change(aws, parent_result):
    result = restore(aws, parent_result=parent_result)
    assert aws.calls == []
    assert (result.outcome, result.reason, result.residual) == (
        "NOT_ATTEMPTED",
        "NO_PROTECTION_WRITE",
        False,
    )


@pytest.mark.parametrize(
    "parent_result",
    [
        parent(),  # 시도 의도만 남은 채 끊김
        parent(outcome="APPLIED", reason="WRITTEN", residual=False),  # 쓰기 확인
        parent(outcome="UNKNOWN", reason="WRITE_UNCERTAIN"),  # 응답 유실
    ],
    ids=["intent-only", "written", "lost-response"],
)
def test_restore_proceeds_when_our_write_may_have_changed_the_value(aws, parent_result):
    aws.set(
        "autoscaling.describe_auto_scaling_instances", asg_instances(protected=False)
    )
    result = restore(aws, parent_result=parent_result)
    assert (result.outcome, result.reason) == ("APPLIED", "ALREADY_RESTORED")


@pytest.mark.parametrize("protection", [None, UNKNOWN])
def test_restore_with_uncertain_old_state_does_nothing(aws, protection):
    assert restore(aws, protection) is None
    assert aws.calls == []


@pytest.mark.parametrize(
    "lookup, reason",
    [
        (asg_instances(OTHER_ASG, protected=True), "MEMBERSHIP_DRIFT"),
        (EndpointConnectionError(endpoint_url="x"), "LOOKUP_FAILED"),
    ],
)
def test_restore_does_not_write_on_membership_drift_or_lookup_failure(
    aws, lookup, reason
):
    aws.set("autoscaling.describe_auto_scaling_instances", lookup)
    result = restore(aws)
    assert changes(aws) == []
    assert (result.outcome, result.reason, result.residual) == (
        "NOT_ATTEMPTED",
        reason,
        True,
    )


def test_restore_failure_is_a_residual_result(aws):
    aws.set(
        "autoscaling.describe_auto_scaling_instances", asg_instances(protected=True)
    )
    aws.set("autoscaling.set_instance_protection", client_error("ValidationError", 400))
    result = restore(aws)
    assert (result.outcome, result.reason, result.residual) == (
        "FAILED",
        "WRITE_REJECTED",
        True,
    )


def test_restore_lost_response_is_unknown_not_success(aws):
    aws.set(
        "autoscaling.set_instance_protection", EndpointConnectionError(endpoint_url="x")
    )
    aws.set(
        "autoscaling.describe_auto_scaling_instances",
        [asg_instances(protected=True), asg_instances(protected=True)],
    )
    result = restore(aws)
    assert (result.outcome, result.reason, result.residual) == (
        "UNKNOWN",
        "WRITE_UNCERTAIN",
        True,
    )
