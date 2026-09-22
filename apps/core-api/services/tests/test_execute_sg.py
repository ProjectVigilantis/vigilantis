"""executor.execute_sg_delete_isolated() · execute_sg_recreate() 단위 테스트 (Issue #368).

AWS 불필요 — boto3 클라이언트를 가짜로 갈아 끼우고 **단계 순서와 effect**를 본다.

이 파일이 지키는 것은 셋이다.

  ① 삭제가 `DependencyViolation`으로 거절되면 `effect=NOT_APPLIED`다. **LocalStack은 이
     거절을 흉내 내지 않으므로**(2026-09-22 실측 — ENI에 붙은 SG도 다른 SG가 참조하는
     SG도 그냥 지워진다) 이 축을 고정할 자리는 여기뿐이다. 실물 확인은 10/6(화) 실 AWS
     스모크 몫이다(ADR-0006 §4 이월).
  ② 원복이 **생성 → 기본 egress 회수 → 규칙 주입** 순으로 돌고, 회수는 백업 내용과
     무관하게 항상 거친다.
  ③ 자기 참조 규칙만 새 ID로 바뀌고 다른 SG를 가리키는 쌍은 그대로다 — 바꾸면 원본이
     허용하지 않던 통신을 우리가 여는 것이 된다.

LocalStack 실물 왕복(삭제 → 원복)은 test_execute_sg_localstack.py가 맡는다.
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

from schemas.backups import SgFullRulesBackup  # noqa: E402
from schemas.executions import ExecutionEffect, ExecutionStepStatus  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from services.aws import executor as ex  # noqa: E402

R = PrecheckReasonCode
S = ExecutionStepStatus
E = ExecutionEffect

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
GROUP = "sg-0abc123456789def0"
NEW_GROUP = "sg-0fed987654321cba0"
OTHER_GROUP = "sg-0111222233334444a"
VPC = "vpc-0abc123456789def0"
GROUP_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{GROUP}"

OK_RESPONSE = {"ResponseMetadata": {"RequestId": "req-ok"}}
CREATE_RESPONSE = {"GroupId": NEW_GROUP, "ResponseMetadata": {"RequestId": "req-create"}}
DEFAULT_EGRESS = dict(ex.DEFAULT_EGRESS_PERMISSION)

SSH_FROM_BASTION = {
    "IpProtocol": "tcp",
    "FromPort": 22,
    "ToPort": 22,
    "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
}


def client_error(code: str, status: int | None = 400) -> ClientError:
    metadata: dict = {"RequestId": "req-err"}
    if status is not None:
        metadata["HTTPStatusCode"] = status
    return ClientError(
        {"Error": {"Code": code, "Message": ""}, "ResponseMetadata": metadata},
        "Op",
    )


def backup(**overrides) -> SgFullRulesBackup:
    values = {
        "group_name": "vigilantis-seed-unused",
        "description": "unused security group",
        "vpc_id": VPC,
        "ingress_permissions": [],
        "egress_permissions": [],
        "group_id": GROUP,
    }
    values.update(overrides)
    return SgFullRulesBackup(**values)


class FakeEc2:
    def __init__(self, state):
        self._state = state

    def __getattr__(self, operation):
        def call(**kwargs):
            self._state["calls"].append((operation, kwargs))
            outcome = self._state["overrides"].get(operation)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome is not None:
                return outcome
            if operation == "describe_security_groups":
                # 기본은 "방금 만든 SG에 AWS가 기본 egress를 붙여 둔" 상태다
                return {
                    "SecurityGroups": [
                        {
                            "GroupId": NEW_GROUP,
                            "GroupName": "vigilantis-seed-unused",
                            "VpcId": VPC,
                            "IpPermissions": [],
                            "IpPermissionsEgress": [dict(DEFAULT_EGRESS)],
                        }
                    ]
                }
            if operation == "create_security_group":
                return CREATE_RESPONSE
            return OK_RESPONSE

        return call


@pytest.fixture
def aws(monkeypatch):
    state = {"overrides": {}, "calls": [], "clients": []}

    def factory(service, region=None, **_):
        state["clients"].append((service, region))
        return FakeEc2(state)

    monkeypatch.setattr(ex, "aws_client", factory)

    def configure(**overrides):
        state["overrides"].update(overrides)

    configure.calls = state["calls"]
    configure.clients = state["clients"]
    return configure


def operations(aws):
    return [name for name, _ in aws.calls]


def kwargs_of(aws, operation: str) -> list[dict]:
    return [payload for name, payload in aws.calls if name == operation]


# ==============================================================================
# 삭제 (RUNBOOK_SG_DELETE_ISOLATED)
# ==============================================================================


def delete(recorded=None, *, target_arn=GROUP_ARN):
    return ex.execute_sg_delete_isolated(
        target_arn, record_step=None if recorded is None else recorded.append
    )


def test_delete_calls_aws_once_and_records_one_step(aws):
    recorded: list = []

    outcome = delete(recorded)

    assert outcome.succeeded
    assert operations(aws) == ["delete_security_group"]
    assert kwargs_of(aws, "delete_security_group") == [{"GroupId": GROUP}]
    assert [step.step_type for step in outcome.steps] == [ex.STEP_DELETE_SECURITY_GROUP]
    assert outcome.steps[0].effect is E.APPLIED
    # AWS 호출 직전 IN_PROGRESS가 먼저 기록된다 — dispatcher의 "단계 0건 = 자산 미변경"
    # 분기가 이 순서에 기댄다
    assert [step.status for step in recorded] == [S.IN_PROGRESS, S.SUCCESS]


def test_delete_does_not_re_check_the_attachment_state(aws):
    """삭제 직전 상태 재확인을 두지 않는다 — 그 판정은 AWS가 한다.

    우리가 조회로 "어디에도 안 붙었는가"를 세면 ENI·다른 SG 규칙·참조 관계 목록이
    우리 쪽에 생기고, 그 순간 AWS가 아는 참조와 갈린다.
    """
    delete()

    assert "describe_security_groups" not in operations(aws)


def test_dependency_violation_is_recorded_as_not_applied(aws):
    """참조가 남은 SG는 AWS가 거절한다 — 4xx라 자산이 그대로인 실패로 확정된다.

    **LocalStack이 흉내 내지 않는 갈래라 이 테스트가 유일한 고정점이다**(2026-09-22 실측).
    NOT_APPLIED가 아니면 되돌릴 것이 없는 실패에 복구 경로가 열린다
    (packages/schemas/executions.py 복구 가능 상태 주석).
    """
    aws(delete_security_group=client_error("DependencyViolation", 400))

    outcome = delete()

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_INVALID_STATE
    assert outcome.steps[-1].effect is E.NOT_APPLIED
    assert outcome.steps[-1].status is S.FAILED


def test_delete_that_ends_unknown_keeps_the_asset_question_open(aws):
    """5xx는 적용 여부를 모른다 — UNKNOWN이라 다음 주기의 현물 판정으로 간다."""
    aws(delete_security_group=client_error("InternalError", 500))

    outcome = delete()

    assert not outcome.succeeded
    assert outcome.steps[-1].effect is E.UNKNOWN


def test_delete_rejects_a_non_security_group_arn(aws):
    outcome = delete(target_arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/vol-0abc123456789def0")

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


# ==============================================================================
# 원복 (RUNBOOK_SG_RECREATE)
# ==============================================================================


def recreate(recorded=None, *, target_arn=GROUP_ARN, restored=None):
    return ex.execute_sg_recreate(
        target_arn,
        backup=restored or backup(),
        record_step=None if recorded is None else recorded.append,
    )


def test_recreate_runs_create_then_revoke_then_authorize(aws):
    """순서가 계약이다 — 회수가 주입보다 뒤면 같은 규칙이 Duplicate로 거절된다."""
    restored = backup(
        ingress_permissions=[SSH_FROM_BASTION], egress_permissions=[dict(DEFAULT_EGRESS)]
    )

    outcome = recreate(restored=restored)

    assert outcome.succeeded
    assert operations(aws) == [
        "create_security_group",
        "describe_security_groups",
        "revoke_security_group_egress",
        "authorize_security_group_ingress",
        "authorize_security_group_egress",
    ]
    assert [step.step_type for step in outcome.steps] == [
        ex.STEP_CREATE_SECURITY_GROUP,
        ex.STEP_REVOKE_DEFAULT_EGRESS,
        ex.STEP_AUTHORIZE_SG_INGRESS,
        ex.STEP_AUTHORIZE_SG_EGRESS,
    ]


def test_recreate_revokes_the_default_egress_even_with_an_empty_backup(aws):
    """회수는 백업 내용과 무관하게 항상 거친다 — 걷어 낼 대상은 AWS가 붙인 것이다.

    백업에 아웃바운드 규칙이 없다고 건너뛰면 **원본보다 넓은 SG**(전체 허용 egress)가
    복원 완료로 남는다.
    """
    outcome = recreate()

    assert outcome.succeeded
    assert operations(aws) == [
        "create_security_group",
        "describe_security_groups",
        "revoke_security_group_egress",
    ]
    assert kwargs_of(aws, "revoke_security_group_egress") == [
        {"GroupId": NEW_GROUP, "IpPermissions": [dict(DEFAULT_EGRESS)]}
    ]


def test_recreate_revokes_whatever_aws_actually_attached(aws):
    """상수로 지우지 않는다 — AWS가 다른 모양을 붙이면 그 규칙이 조용히 남는다."""
    attached = {"IpProtocol": "-1", "Ipv6Ranges": [{"CidrIpv6": "::/0"}]}
    aws(
        describe_security_groups={
            "SecurityGroups": [
                {"GroupId": NEW_GROUP, "IpPermissions": [], "IpPermissionsEgress": [attached]}
            ]
        }
    )

    recreate()

    assert kwargs_of(aws, "revoke_security_group_egress") == [
        {"GroupId": NEW_GROUP, "IpPermissions": [attached]}
    ]


def test_recreate_skips_revocation_when_nothing_was_attached(aws):
    """붙은 것이 없으면 회수할 대상이 없을 뿐 실패가 아니다."""
    aws(
        describe_security_groups={
            "SecurityGroups": [
                {"GroupId": NEW_GROUP, "IpPermissions": [], "IpPermissionsEgress": []}
            ]
        }
    )

    outcome = recreate()

    assert outcome.succeeded
    assert "revoke_security_group_egress" not in operations(aws)
    revoke_step = outcome.steps[1]
    assert revoke_step.step_type == ex.STEP_REVOKE_DEFAULT_EGRESS
    assert revoke_step.effect is E.NOT_APPLIED


def test_recreate_summary_carries_the_original_and_the_new_group_id(aws):
    """원본 ID를 참조하던 자원은 돌아오지 않는다(ADR-0008 §참조 무결성) —
    관제자가 무엇을 손수 다시 이어야 하는지 알려면 두 ID가 기록에 남아야 한다."""
    outcome = recreate()

    summary = outcome.steps[0].result_summary
    assert GROUP in summary and NEW_GROUP in summary


def test_recreate_rebinds_only_the_self_reference(aws):
    """자기 참조는 새 ID로, 다른 SG를 가리키는 쌍은 그대로.

    다른 SG까지 바꾸면 원본이 허용하지 않던 통신을 우리가 여는 것이 된다.
    """
    restored = backup(
        ingress_permissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "UserIdGroupPairs": [
                    {"UserId": ACCOUNT, "GroupId": GROUP},
                    {"UserId": ACCOUNT, "GroupId": OTHER_GROUP},
                ],
            }
        ]
    )

    recreate(restored=restored)

    injected = kwargs_of(aws, "authorize_security_group_ingress")[0]["IpPermissions"]
    assert [pair["GroupId"] for pair in injected[0]["UserIdGroupPairs"]] == [
        NEW_GROUP,
        OTHER_GROUP,
    ]
    # UserId 같은 나머지 필드는 그대로 실어 보낸다
    assert injected[0]["UserIdGroupPairs"][0]["UserId"] == ACCOUNT


def test_recreate_leaves_rules_alone_when_the_backup_has_no_group_id(aws):
    """무엇이 자기 참조인지 가릴 수 없으면 아무것도 바꾸지 않는다 —
    짐작으로 바꾸면 엉뚱한 SG를 여는 쪽이 더 나쁘다."""
    restored = backup(
        group_id=None,
        ingress_permissions=[{"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": GROUP}]}],
    )

    recreate(restored=restored)

    injected = kwargs_of(aws, "authorize_security_group_ingress")[0]["IpPermissions"]
    assert injected[0]["UserIdGroupPairs"][0]["GroupId"] == GROUP


def test_recreate_stops_before_injecting_when_the_new_group_id_is_missing(aws):
    """200을 받았는데 ID가 없다 — 규칙을 주입할 대상을 특정할 수 없다."""
    aws(create_security_group={"ResponseMetadata": {"RequestId": "req-create"}})

    outcome = recreate(restored=backup(ingress_permissions=[SSH_FROM_BASTION]))

    assert not outcome.succeeded
    assert operations(aws) == ["create_security_group"]
    assert outcome.steps[-1].status is S.FAILED


def test_recreate_stops_when_the_new_group_cannot_be_read(aws):
    """회수 없이 주입하면 백업보다 넓은 SG가 남을 수 있다 — 여기서 멈춘다."""
    aws(describe_security_groups=EndpointConnectionError(endpoint_url="https://ec2"))

    outcome = recreate(restored=backup(ingress_permissions=[SSH_FROM_BASTION]))

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_AWS_ERROR
    assert "authorize_security_group_ingress" not in operations(aws)
    # 이미 SG를 만들었으므로 보류가 아니라 **단계가 남은 실패**다
    assert not outcome.deferred and outcome.steps


def test_recreate_that_fails_midway_leaves_the_created_group_recorded(aws):
    """중간에 끊기면 규칙 일부만 선 SG가 남는다 — 원복의 원복은 없으므로(ADR-0008 §6)
    그 사실이 단계 기록에 남아야 사람이 이어받을 수 있다."""
    aws(authorize_security_group_ingress=client_error("UnauthorizedOperation", 403))

    outcome = recreate(restored=backup(ingress_permissions=[SSH_FROM_BASTION]))

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_UNAUTHORIZED
    assert outcome.steps[0].effect is E.APPLIED  # 생성은 적용됐다
    assert outcome.steps[-1].step_type == ex.STEP_AUTHORIZE_SG_INGRESS


def test_recreate_rejects_a_non_security_group_arn(aws):
    outcome = recreate(target_arn=f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/i-0abc123456789def0")

    assert not outcome.succeeded
    assert outcome.reason_code is R.PRECHECK_PARAM_INVALID
    assert aws.calls == []


# ------------------------------------------------------------------ 규칙 대조
def test_permissions_match_ignores_order_and_empty_key_notation():
    """describe 응답은 우리가 보낸 것과 글자 그대로 같지 않다 — AWS가 빈 목록 키를
    채우고 순서도 보장하지 않는다. 그 차이를 불일치로 읽으면 멀쩡한 복원이 미완이 된다."""
    sent = [SSH_FROM_BASTION, {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]
    returned = [
        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}], "UserIdGroupPairs": []},
        {
            **SSH_FROM_BASTION,
            "IpRanges": [{"CidrIp": "10.0.0.0/8", "Description": "bastion"}],
            "Ipv6Ranges": [],
            "PrefixListIds": [],
        },
    ]

    assert ex.sg_permissions_match(returned, sent)


def test_permissions_match_sees_a_widened_rule():
    """허용 범위를 결정하는 축은 전부 본다 — 하나라도 빠뜨리면 넓어진 SG가 완료로 닫힌다."""
    sent = [SSH_FROM_BASTION]
    widened = [{**SSH_FROM_BASTION, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]

    assert not ex.sg_permissions_match(widened, sent)


def test_permissions_match_handles_rules_without_ports():
    """FromPort가 없는 규칙(-1)과 있는 규칙이 섞여도 대조가 끊기지 않는다 —
    튜플 정렬로 비교하면 None과 int가 만나 TypeError가 난다."""
    mixed = [SSH_FROM_BASTION, {"IpProtocol": "-1"}]

    assert ex.sg_permissions_match(list(reversed(mixed)), mixed)


def test_permissions_match_on_empty_lists():
    assert ex.sg_permissions_match([], [])
    assert not ex.sg_permissions_match([SSH_FROM_BASTION], [])
