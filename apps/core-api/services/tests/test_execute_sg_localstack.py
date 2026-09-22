"""SG 삭제·원복 LocalStack 통합 테스트 (Issue #368, ADR-0006).

단계 분기 전수는 test_execute_sg.py가 맡고, 여기서는 **실물에서 SG가 실제로 사라지고
같은 규칙으로 되살아나는가**를 본다. 삭제는 되돌릴 수 없어서, 가짜 클라이언트만으로는
"지워졌다"도 "같은 규칙으로 돌아왔다"도 증명할 수 없다.

**시드 SG(`vigilantis-seed-unused`)를 쓰지 않는다.** 삭제는 자원을 없애는 조치라 공용
시드를 지우면 다음 스캔·다른 테스트가 그 자산을 잃는다. 이 파일이 자기 SG를 만들어 쓰고,
실패해도 남지 않게 정리한다(test_execute_ebs_localstack.py와 같은 이유).

전제 실측(2026-09-22 LocalStack Community) — 이 카드 착수 첫 단계에서 이슈 #368이 지목한
두 항목을 쟀다.

  ① `create_security_group`의 **기본 egress 자동 부착**: 실 AWS와 같다. 생성 직후
     `IpPermissionsEgress`가 전체 허용 1건이고, 같은 규칙을 다시 주입하면
     `InvalidPermission.Duplicate`로 거절된다. 아래 테스트가 그대로 확인한다.
  ② `delete_security_group`의 **`DependencyViolation`**: **흉내 내지 않는다.** ENI에 붙은
     SG도, 다른 SG 규칙이 참조하는 SG도 그냥 지워진다. 그래서 그 갈래는 여기서 재현할 수
     없고 단위 테스트가 거절을 주입해 고정한다(test_execute_sg.py) — 실물 확인은 10/6(화)
     실 AWS 스모크 몫이다(ADR-0006 §4 이월 목록).

그 밖에 확인한 것: 삭제 뒤 조회는 `InvalidGroup.NotFound`, `group-name` + `vpc-id` 필터
조회 동작, 자기 참조 규칙이 `UserIdGroupPairs`에 `{UserId, GroupId}`로 담기는 것.

로컬 실행 전제: LocalStack 기동. 미기동 시 전체 skip.
"""

import os
import sys
import urllib.request
import uuid
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

from schemas.backups import SgFullRulesBackup  # noqa: E402
from schemas.executions import ExecutionEffect  # noqa: E402
from schemas.precheck import PrecheckReasonCode  # noqa: E402
from schemas.runbooks import RunbookId  # noqa: E402
from services.aws import backup as bk  # noqa: E402
from services.aws import executor as ex  # noqa: E402
from services.aws.client import account_id, aws_client, default_region  # noqa: E402

R = PrecheckReasonCode
E = ExecutionEffect

EVIDENCE_ID = "evd-localstack-sg"


def _localstack_up() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _localstack_up(), reason="LocalStack(4566) 미기동 — 통합 테스트 skip"
)


def _group_gone(ec2, group_id: str) -> bool:
    try:
        ec2.describe_security_groups(GroupIds=[group_id])
    except ClientError as exc:
        return exc.response["Error"]["Code"] == "InvalidGroup.NotFound"
    return False


def _delete_quietly(ec2, group_id: str) -> None:
    try:
        ec2.delete_security_group(GroupId=group_id)
    except ClientError:
        pass


def _by_name(ec2, group_name: str, vpc_id: str):
    groups = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [group_name]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    return groups[0] if groups else None


class _Loader:
    """가드레일 ④가 읽는 백업 조회 — 이 파일은 DB를 쓰지 않으므로 손으로 준다."""

    def __init__(self, view):
        self._view = view

    def get(self, backup_record_id):
        return self._view

    def latest_for_target(self, target_arn, backup_type, payload_match=None):
        return self._view


@pytest.fixture
def scratch_group():
    """미부착 SG 1개를 만들어 쓰고, 남으면 지운다. (group_id, group_name, vpc_id, arn)."""
    ec2 = aws_client("ec2")
    region = default_region()
    vpc_id = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    group_name = f"vigilantis-test-sg-{uuid.uuid4().hex[:8]}"
    group_id = ec2.create_security_group(
        GroupName=group_name, Description="vigilantis sg delete test", VpcId=vpc_id
    )["GroupId"]
    arn = f"arn:aws:ec2:{region}:{account_id()}:security-group/{group_id}"
    try:
        yield group_id, group_name, vpc_id, arn
    finally:
        _delete_quietly(ec2, group_id)
        recreated = _by_name(ec2, group_name, vpc_id)
        if recreated is not None:
            _delete_quietly(ec2, recreated["GroupId"])


# ------------------------------------------------------------------ 착수 전제 실측


def test_aws_attaches_a_wide_open_egress_rule_on_creation(scratch_group):
    """이 런북이 기본 egress 회수 단계를 갖는 이유 — 우리가 요청하지 않아도 붙는다.

    걷어 내지 않으면 백업에 같은 규칙이 있을 때 주입이 Duplicate로 실패하고, 없을 때는
    원본보다 **넓은** SG가 남는다.
    """
    group_id, _, _, _ = scratch_group
    ec2 = aws_client("ec2")

    group = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]

    assert group["IpPermissions"] == []
    assert ex.sg_permissions_match(
        group["IpPermissionsEgress"], [dict(ex.DEFAULT_EGRESS_PERMISSION)]
    )
    with pytest.raises(ClientError) as rejected:
        ec2.authorize_security_group_egress(
            GroupId=group_id, IpPermissions=group["IpPermissionsEgress"]
        )
    assert rejected.value.response["Error"]["Code"] == "InvalidPermission.Duplicate"


# ------------------------------------------------------------------ 삭제 → 원복 왕복


def test_delete_then_recreate_restores_the_same_rule_set(scratch_group):
    """이 카드의 완료 판정 — 삭제가 실경로로 돌고, 원복이 **같은 규칙 집합**을 되세운다.

    규칙에 **자기 참조**를 심는 것이 요점이다. 원본 ID는 삭제와 함께 사라지므로 그대로
    주입하면 `InvalidGroup.NotFound`로 거절된다 — 새 ID로 바꿔 넣어야 원본이 뜻한
    "이 SG를 단 대상끼리 통신"이 같은 뜻으로 돌아온다.
    """
    group_id, group_name, vpc_id, arn = scratch_group
    ec2 = aws_client("ec2")
    ec2.authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
            },
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "UserIdGroupPairs": [{"GroupId": group_id}],
            },
        ],
    )

    # ① 가드레일 ④ — 삭제가 실제로 허용되는가
    outcome = ex.precheck(
        RunbookId.RUNBOOK_SG_DELETE_ISOLATED,
        arn,
        {"group_id": group_id, "evidence_id": EVIDENCE_ID},
    )
    assert outcome.passed, outcome.reason_code

    # ② 백업 캡처 — 삭제 호출보다 먼저다
    capture = bk.capture_sg_full_rules(group_id, default_region())
    assert capture.captured, capture.detail
    restored = SgFullRulesBackup.model_validate(capture.payload)
    assert restored.group_id == group_id
    assert restored.vpc_id == vpc_id
    assert len(restored.ingress_permissions) == 2

    # ③ 삭제 — 실물에서 사라진다
    deleted = ex.execute_sg_delete_isolated(arn)
    assert deleted.succeeded, deleted.error_summary
    assert deleted.steps[0].effect is E.APPLIED
    assert _group_gone(ec2, group_id)

    # ④ 가드레일 ④ — 원복도 실제로 허용되는가(회수·주입 포함)
    view = ex.BackupRecordView(
        backup_record_id="bk-localstack",
        target_arn=arn,
        backup_type=ex.BACKUP_SG_FULL_RULES,
        payload=capture.payload,
    )
    outcome = ex.precheck(
        RunbookId.RUNBOOK_SG_RECREATE,
        arn,
        {"backup_record_id": view.backup_record_id, "evidence_id": EVIDENCE_ID},
        backup_loader=_Loader(view),
    )
    assert outcome.passed, outcome.reason_code

    # ⑤ 원복 — 생성 → 기본 egress 회수 → 규칙 주입
    recreated = ex.execute_sg_recreate(arn, backup=restored)
    assert recreated.succeeded, recreated.error_summary
    assert [step.step_type for step in recreated.steps] == [
        ex.STEP_CREATE_SECURITY_GROUP,
        ex.STEP_REVOKE_DEFAULT_EGRESS,
        ex.STEP_AUTHORIZE_SG_INGRESS,
        ex.STEP_AUTHORIZE_SG_EGRESS,
    ]

    # ⑥ 실물 대조 — 이름·VPC로 찾아 규칙 집합이 백업과 같은지 본다
    live = _by_name(ec2, group_name, vpc_id)
    assert live is not None
    new_group_id = live["GroupId"]
    assert new_group_id != group_id
    assert new_group_id in recreated.steps[0].result_summary
    assert group_id in recreated.steps[0].result_summary

    expected_ingress = ex.rebind_self_reference(
        restored.ingress_permissions,
        original_group_id=group_id,
        new_group_id=new_group_id,
    )
    assert ex.sg_permissions_match(live["IpPermissions"], expected_ingress)
    assert ex.sg_permissions_match(
        live["IpPermissionsEgress"], restored.egress_permissions
    )
    # 자기 참조가 실제로 새 ID를 가리킨다 — 원본 ID가 남아 있으면 주입 자체가 거절됐어야 한다
    self_referencing = [
        pair
        for permission in live["IpPermissions"]
        for pair in permission.get("UserIdGroupPairs", [])
    ]
    assert [pair["GroupId"] for pair in self_referencing] == [new_group_id]


def test_recreate_leaves_no_default_egress_when_the_backup_had_none(scratch_group):
    """규칙 0건 SG를 되살리면 규칙 0건이어야 한다 — 회수를 건너뛰면 전체 허용이 남는다."""
    group_id, group_name, vpc_id, arn = scratch_group
    ec2 = aws_client("ec2")
    # 원본에서 기본 egress를 걷어 내 "아무것도 열지 않는 SG"를 만든다
    original = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
    ec2.revoke_security_group_egress(
        GroupId=group_id, IpPermissions=original["IpPermissionsEgress"]
    )

    capture = bk.capture_sg_full_rules(group_id, default_region())
    restored = SgFullRulesBackup.model_validate(capture.payload)
    assert restored.ingress_permissions == [] and restored.egress_permissions == []

    assert ex.execute_sg_delete_isolated(arn).succeeded
    outcome = ex.execute_sg_recreate(arn, backup=restored)
    assert outcome.succeeded, outcome.error_summary

    live = _by_name(ec2, group_name, vpc_id)
    assert live["IpPermissions"] == []
    assert live["IpPermissionsEgress"] == []


def test_judging_a_deleted_group_sees_it_as_gone(scratch_group):
    """종료 판정이 읽는 축 — 삭제 뒤 조회는 `InvalidGroup.NotFound`다."""
    group_id, _, _, arn = scratch_group

    assert ex.execute_sg_delete_isolated(arn).succeeded

    group, code = ex.current_security_group(group_id, default_region())

    assert group is None
    assert code is R.PRECHECK_TARGET_NOT_FOUND


def test_finding_a_group_by_name_returns_nothing_instead_of_an_error(scratch_group):
    """이름 필터는 없는 이름에 오류를 내지 않고 빈 목록을 준다 —
    그래서 대상 부재가 사유 코드가 아니라 (None, None)이다."""
    _, _, vpc_id, _ = scratch_group

    group, code = ex.security_group_by_name(
        f"vigilantis-absent-{uuid.uuid4().hex[:8]}", vpc_id, default_region()
    )

    assert group is None and code is None
