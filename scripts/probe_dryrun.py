# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# DryRun 지원 여부 실측 스크립트입니다. (Issue #130, ADR-0007 §6)
#
# 확정 10종이 쓰는 AWS 작업 전수에 DryRun=True를 걸어 ① 어떤 예외가 나는지
# ② 자원이 실제로 바뀌었는지를 판정하고, ADR-0007 §Context 표를 재현한다.
#
# **자원 변경 여부 확인이 이 스크립트의 핵심이다.** 예외만 보면 LocalStack의
# create_network_acl_entry처럼 "DryRun 플래그를 무시하고 실제로 수행해 버리는" 작업을
# 놓친다(ADR-0006 §4 5행). 그 경우 확인 단계가 조용히 조치를 집행하고 통과로 기록된다.
#
# 같은 이유로 **파괴적 작업의 대상은 늘 이 스크립트가 만든 자원이다**(probe SG·probe
# EBS). 시드 자산을 대상으로 삼으면 DryRun이 듣지 않는 순간 시드가 삭제·변조된다 —
# 실 AWS 스모크(6–7주차)에서 같은 형태로 돌릴 스크립트라 더욱 그렇다.
#
# 실행 (repo 루트, LocalStack 기동 + scripts/seed_localstack.py 완료 후):
#   PowerShell: $env:AWS_ENDPOINT_URL='http://localhost:4566'; uv run python scripts/probe_dryrun.py
#   bash      : AWS_ENDPOINT_URL=http://localhost:4566 uv run python scripts/probe_dryrun.py
#   PR 첨부용 JSON: ... scripts/probe_dryrun.py --json
#
# 안전 가드(ADR-0006 §2, seed_localstack.py와 같은 방식): 실 AWS에는 실행하지 않는다.
#   엔드포인트 미설정 시 즉시 종료 + LocalStack 헬스체크 선행 확인.
#
# ADR-0007 §6 머지 조건: **런북을 추가하거나 target_api를 바꾸는 PR은 이 스크립트의
# --json 출력을 본문에 첨부한다.** 코드 표와 ADR 표의 정합은 회귀 테스트가 지킨다
# (apps/core-api/services/tests/test_dryrun_support_matrix.py).
# ==============================================================================

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT / "apps" / "core-api"), str(_REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from botocore.exceptions import ClientError, ParamValidationError  # noqa: E402

from services.aws.client import aws_client, endpoint_url  # noqa: E402

# 판정 값 — ADR-0007 §Context 표의 "판정" 칸과 같은 문자열을 쓴다(표 대조가 문자열 비교다).
DRY_RUN = "DryRun"
DESCRIBE_FALLBACK = "조회 대체"

# 관측 값. DryRun 성공은 예외로 온다 — 그 밖의 값은 전부 "DryRun을 쓸 수 없다"는 신호다.
DRY_RUN_SUCCESS = "DryRunOperation"
NO_EXCEPTION = "없음"
PARAM_VALIDATION = "ParamValidationError"


@dataclass(frozen=True)
class TargetApi:
    """확정 10종이 쓰는 AWS 작업 1건. ADR-0007 §Context 표의 한 행이다."""

    operation: str
    runbooks: tuple[str, ...]
    verdict: str


# ADR-0007 §Context 표와 1:1이다. 한쪽만 고치면 회귀 테스트가 잡는다.
TARGET_API_MATRIX: tuple[TargetApi, ...] = (
    TargetApi("ec2.modify_instance_attribute", ("RIGHTSIZING", "REVERT_SIZE"), DRY_RUN),
    TargetApi("ec2.modify_network_interface_attribute", ("ISOLATE", "UNISOLATE"), DRY_RUN),
    TargetApi("ec2.create_security_group", ("SG_RECREATE",), DRY_RUN),
    TargetApi("ec2.delete_security_group", ("SG_DELETE_ISOLATED",), DRY_RUN),
    TargetApi("ec2.authorize_security_group_ingress", ("SG_RECREATE",), DRY_RUN),
    TargetApi("ec2.authorize_security_group_egress", ("SG_RECREATE",), DRY_RUN),
    TargetApi("ec2.create_launch_template", ("ENABLE_AUTOSCALING",), DRY_RUN),
    TargetApi("ec2.create_snapshot", ("EBS_DELETE_UNATTACHED",), DRY_RUN),
    TargetApi("ec2.delete_volume", ("EBS_DELETE_UNATTACHED",), DRY_RUN),
    TargetApi("ec2.create_network_acl_entry", ("NACL_ADD_DENY",), DESCRIBE_FALLBACK),
    TargetApi("ec2.delete_network_acl_entry", ("NACL_RESTORE",), DESCRIBE_FALLBACK),
    TargetApi("elbv2.deregister_targets", ("ISOLATE",), DESCRIBE_FALLBACK),
    TargetApi("elbv2.register_targets", ("UNISOLATE",), DESCRIBE_FALLBACK),
    TargetApi("autoscaling.create_auto_scaling_group", ("ENABLE_AUTOSCALING",), DESCRIBE_FALLBACK),
)


def verdict_for(exception_label: str, resource_changed: bool) -> str:
    """실측 결과 → 판정. DryRunOperation 예외가 났고 자원이 그대로일 때만 DryRun이다."""
    if exception_label == DRY_RUN_SUCCESS and not resource_changed:
        return DRY_RUN
    return DESCRIBE_FALLBACK


# ------------------------------------------------------------------ 안전 가드
def require_localstack() -> str:
    """실 AWS 실행 거부(ADR-0006 §2). DryRun이 듣지 않는 작업은 실제로 자원을 만든다."""
    endpoint = endpoint_url()
    if not endpoint:
        sys.exit(
            "엔드포인트 미설정 — 이 스크립트는 LocalStack 전용이다.\n"
            "  예) AWS_ENDPOINT_URL=http://localhost:4566"
        )
    if "amazonaws.com" in endpoint:
        sys.exit(f"실 AWS 엔드포인트({endpoint})에는 실행하지 않는다.")
    try:
        with urllib.request.urlopen(f"{endpoint}/_localstack/health", timeout=3) as r:
            if r.status != 200:
                raise OSError(f"status {r.status}")
    except Exception as exc:  # noqa: BLE001 — 원인 무관하게 같은 안내로 종료
        sys.exit(
            f"LocalStack 헬스체크 실패({endpoint}): {exc}\n"
            "  docker compose up 으로 localstack 서비스가 기동됐는지 확인할 것."
        )
    return endpoint


# ------------------------------------------------------------------ 실측 대상
PROBE_SG_NAME = "vigilantis-probe-sg"
PROBE_LT_NAME = "vigilantis-probe-lt"
PROBE_VOLUME_NAME = "vigilantis-probe-vol"
# precheck 통합 테스트(test_precheck_localstack.py)가 쓰는 31337과 겹치지 않게 둔다 —
# 둘 다 뒷정리를 하지만 같은 자리를 쓰면 실패 시 서로를 오염시킨다
PROBE_RULE_NUMBER = 31338
PROBE_CIDR = "203.0.113.5/32"


def collect_fixtures() -> dict:
    """시드 자산에서 실측 문맥(인스턴스·ENI·NACL)을 고른다. 없으면 무엇을 먼저 해야
    하는지 알린다. 삭제·변조 대상이 되는 자원은 여기서 고르지 않고 직접 만든다."""
    ec2 = aws_client("ec2")
    instances = [
        i
        for r in ec2.describe_instances()["Reservations"]
        for i in r["Instances"]
        if i["State"]["Name"] == "running"
    ]
    acls = ec2.describe_network_acls()["NetworkAcls"]
    if not instances or not acls:
        sys.exit(
            "실측 대상 부족 — scripts/seed_localstack.py 를 먼저 실행할 것 "
            f"(running EC2 {len(instances)} / NACL {len(acls)})"
        )

    instance = instances[0]
    return {
        "ec2": ec2,
        "instance_id": instance["InstanceId"],
        "instance_type": instance["InstanceType"],
        "vpc_id": instance["VpcId"],
        "availability_zone": instance["Placement"]["AvailabilityZone"],
        "eni_id": instance["NetworkInterfaces"][0]["NetworkInterfaceId"],
        "acl_id": acls[0]["NetworkAclId"],
    }


# ------------------------------------------------------------------ 실측
@dataclass
class ProbeResult:
    operation: str
    runbooks: tuple[str, ...]
    expected: str
    exception: str
    resource_changed: bool

    @property
    def observed(self) -> str:
        return verdict_for(self.exception, self.resource_changed)

    @property
    def matches(self) -> bool:
        return self.observed == self.expected


def attempt(call: Callable[..., Any], observe: Optional[Callable[[], Any]], **params: Any):
    """DryRun=True 호출 1건. (예외 이름, 자원 변경 여부)를 돌려준다.

    observe()는 호출 전후로 같은 것을 재는 함수다 — 값이 달라지면 DryRun이 듣지 않고
    실제로 수행된 것이다.
    """
    before = observe() if observe else None
    label = NO_EXCEPTION
    try:
        call(DryRun=True, **params)
    except ClientError as exc:
        label = str(exc.response.get("Error", {}).get("Code", "")) or "ClientError"
    except ParamValidationError:
        # botocore 클라이언트 단에서 난다 = 그 작업에 DryRun 파라미터가 없다는 뜻
        label = PARAM_VALIDATION
    after = observe() if observe else None
    return label, before != after


def _safe(call: Callable[[], Any], default: Any = None) -> Any:
    """조회 실패(자원 부재 등)를 관측값 하나로 흡수한다."""
    try:
        return call()
    except ClientError:
        return default


# ------------------------------------------------------------------ 실측 전용 자원
def _named_sg_id(ec2, name: str) -> Optional[str]:
    groups = _safe(
        lambda: ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]}]
        )["SecurityGroups"],
        [],
    )
    return groups[0]["GroupId"] if groups else None


def _ensure_probe_sg(ec2, vpc_id: str) -> str:
    """실측 전용 SG를 실제로 만든다. 시드 SG를 대상으로 삼으면 DryRun이 듣지 않는
    순간 시드 자산이 삭제·변조된다 — 대상은 항상 우리가 만든 것이어야 한다."""
    existing = _named_sg_id(ec2, PROBE_SG_NAME)
    if existing:
        return existing
    return ec2.create_security_group(
        GroupName=PROBE_SG_NAME, Description="probe", VpcId=vpc_id
    )["GroupId"]


def _delete_probe_sg(ec2) -> None:
    group_id = _named_sg_id(ec2, PROBE_SG_NAME)
    if group_id:
        _safe(lambda: ec2.delete_security_group(GroupId=group_id))


def _sg_permissions(ec2, group_id: str):
    def read():
        group = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
        return json.dumps(
            {"in": group.get("IpPermissions", []), "eg": group.get("IpPermissionsEgress", [])},
            sort_keys=True,
            default=str,
        )

    return _safe(read, "absent")


def _probe_volume_id(ec2) -> Optional[str]:
    volumes = _safe(
        lambda: ec2.describe_volumes(
            Filters=[{"Name": "tag:Name", "Values": [PROBE_VOLUME_NAME]}]
        )["Volumes"],
        [],
    )
    return volumes[0]["VolumeId"] if volumes else None


def _ensure_probe_volume(ec2, availability_zone: str) -> str:
    """실측 전용 EBS. delete_volume은 되돌릴 수 없는 작업이라 시드 볼륨에 걸지 않는다."""
    existing = _probe_volume_id(ec2)
    if existing:
        return existing
    return ec2.create_volume(
        AvailabilityZone=availability_zone,
        Size=1,
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [{"Key": "Name", "Value": PROBE_VOLUME_NAME}],
            }
        ],
    )["VolumeId"]


def _delete_probe_volume(ec2) -> None:
    volume_id = _probe_volume_id(ec2)
    if volume_id:
        _safe(lambda: ec2.delete_volume(VolumeId=volume_id))


def _snapshots_of(ec2, volume_id: str) -> list[str]:
    return [
        s["SnapshotId"]
        for s in _safe(
            lambda: ec2.describe_snapshots(
                Filters=[{"Name": "volume-id", "Values": [volume_id]}]
            )["Snapshots"],
            [],
        )
    ]


def _lt_exists(ec2, name: str) -> bool:
    return bool(
        _safe(
            lambda: ec2.describe_launch_templates(LaunchTemplateNames=[name])[
                "LaunchTemplates"
            ],
            [],
        )
    )


def _acl_entries(ec2, acl_id: str):
    def read():
        acl = ec2.describe_network_acls(NetworkAclIds=[acl_id])["NetworkAcls"][0]
        return sorted((e["RuleNumber"], bool(e["Egress"])) for e in acl.get("Entries", []))

    return _safe(read, [])


def _delete_acl_entry(ec2, acl_id: str, rule_number: int) -> None:
    """정리용 삭제. 없는 규칙에는 호출하지 않는다.

    실패할 것을 아는 호출을 그냥 던지면 adaptive 재시도가 대기를 붙여 실측이
    수십 초씩 늘어난다 — 조회 한 번이 훨씬 싸다.
    """
    if (rule_number, False) not in _acl_entries(ec2, acl_id):
        return
    _safe(
        lambda: ec2.delete_network_acl_entry(
            NetworkAclId=acl_id, RuleNumber=rule_number, Egress=False
        )
    )


def _create_acl_entry(ec2, acl_id: str, rule_number: int) -> None:
    ec2.create_network_acl_entry(
        NetworkAclId=acl_id,
        RuleNumber=rule_number,
        Protocol="-1",
        RuleAction="deny",
        Egress=False,
        CidrBlock=PROBE_CIDR,
    )


# ------------------------------------------------------------------ 작업별 실측 절차
def _probe_ec2(fx, operation: str):
    ec2 = fx["ec2"]
    permission = [
        {
            "IpProtocol": "tcp",
            "FromPort": 22,
            "ToPort": 22,
            "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
        }
    ]

    if operation == "modify_instance_attribute":
        # 대상 타입은 현재 값 그대로 준다. running 인스턴스의 타입 변경은 DryRun이
        # 걸리지 않으면 IncorrectInstanceState로 끝나 판정이 섞이기 때문이다 —
        # 대신 "실제로 수행됐다"를 관측으로 잡지 못하는 유일한 행이다.
        def observe():
            return _safe(
                lambda: ec2.describe_instances(InstanceIds=[fx["instance_id"]])[
                    "Reservations"
                ][0]["Instances"][0]["InstanceType"]
            )

        return attempt(
            ec2.modify_instance_attribute,
            observe,
            InstanceId=fx["instance_id"],
            InstanceType={"Value": fx["instance_type"]},
        )

    if operation == "modify_network_interface_attribute":

        def observe():
            return _safe(
                lambda: sorted(
                    g["GroupId"]
                    for g in ec2.describe_network_interfaces(
                        NetworkInterfaceIds=[fx["eni_id"]]
                    )["NetworkInterfaces"][0]["Groups"]
                ),
                [],
            )

        original = observe()
        group = _ensure_probe_sg(ec2, fx["vpc_id"])
        result = attempt(
            ec2.modify_network_interface_attribute,
            observe,
            NetworkInterfaceId=fx["eni_id"],
            Groups=[group],
        )
        # DryRun이 듣지 않아 실제로 붙었다면 되돌린다. 되돌리기 전에는 probe SG가
        # ENI에 물려 있어 삭제되지 않는다 — 순서가 뒤집히면 잔재가 남는다
        if original and observe() != original:
            _safe(
                lambda: ec2.modify_network_interface_attribute(
                    NetworkInterfaceId=fx["eni_id"], Groups=original
                )
            )
        _delete_probe_sg(ec2)
        return result

    if operation == "create_security_group":
        _delete_probe_sg(ec2)
        result = attempt(
            ec2.create_security_group,
            lambda: bool(_named_sg_id(ec2, PROBE_SG_NAME)),
            GroupName=PROBE_SG_NAME,
            Description="probe",
            VpcId=fx["vpc_id"],
        )
        # DryRun이 듣지 않아 실제로 생겼다면 치운다 — 다음 실행이 같은 조건에서 돌아야 한다
        _delete_probe_sg(ec2)
        return result

    if operation == "delete_security_group":
        group = _ensure_probe_sg(ec2, fx["vpc_id"])
        result = attempt(
            ec2.delete_security_group,
            lambda: bool(_named_sg_id(ec2, PROBE_SG_NAME)),
            GroupId=group,
        )
        _delete_probe_sg(ec2)
        return result

    if operation in ("authorize_security_group_ingress", "authorize_security_group_egress"):
        group = _ensure_probe_sg(ec2, fx["vpc_id"])
        result = attempt(
            getattr(ec2, operation),
            lambda: _sg_permissions(ec2, group),
            GroupId=group,
            IpPermissions=permission,
        )
        _delete_probe_sg(ec2)
        return result

    if operation == "create_launch_template":
        result = attempt(
            ec2.create_launch_template,
            lambda: _lt_exists(ec2, PROBE_LT_NAME),
            LaunchTemplateName=PROBE_LT_NAME,
            LaunchTemplateData={"InstanceType": fx["instance_type"]},
        )
        if _lt_exists(ec2, PROBE_LT_NAME):
            _safe(lambda: ec2.delete_launch_template(LaunchTemplateName=PROBE_LT_NAME))
        return result

    if operation == "create_snapshot":
        volume = _ensure_probe_volume(ec2, fx["availability_zone"])
        result = attempt(
            ec2.create_snapshot,
            lambda: len(_snapshots_of(ec2, volume)),
            VolumeId=volume,
        )
        for snapshot_id in _snapshots_of(ec2, volume):
            _safe(lambda sid=snapshot_id: ec2.delete_snapshot(SnapshotId=sid))
        return result

    if operation == "delete_volume":
        volume = _ensure_probe_volume(ec2, fx["availability_zone"])
        result = attempt(
            ec2.delete_volume,
            lambda: bool(_probe_volume_id(ec2)),
            VolumeId=volume,
        )
        _delete_probe_volume(ec2)
        return result

    if operation == "create_network_acl_entry":
        _delete_acl_entry(ec2, fx["acl_id"], PROBE_RULE_NUMBER)
        result = attempt(
            ec2.create_network_acl_entry,
            lambda: _acl_entries(ec2, fx["acl_id"]),
            NetworkAclId=fx["acl_id"],
            RuleNumber=PROBE_RULE_NUMBER,
            Protocol="-1",
            RuleAction="deny",
            Egress=False,
            CidrBlock=PROBE_CIDR,
        )
        _delete_acl_entry(ec2, fx["acl_id"], PROBE_RULE_NUMBER)
        return result

    if operation == "delete_network_acl_entry":
        _delete_acl_entry(ec2, fx["acl_id"], PROBE_RULE_NUMBER)
        _create_acl_entry(ec2, fx["acl_id"], PROBE_RULE_NUMBER)
        result = attempt(
            ec2.delete_network_acl_entry,
            lambda: _acl_entries(ec2, fx["acl_id"]),
            NetworkAclId=fx["acl_id"],
            RuleNumber=PROBE_RULE_NUMBER,
            Egress=False,
        )
        _delete_acl_entry(ec2, fx["acl_id"], PROBE_RULE_NUMBER)
        return result

    raise AssertionError(f"실측 절차가 없는 작업: {operation}")


def _probe_other(service: str, operation: str, fx):
    """elbv2·autoscaling — DryRun 파라미터 자체가 없어 botocore 단에서 걸린다.

    Community에 서비스가 없어도 결과가 같다: ParamValidationError는 네트워크 호출 전이다.
    """
    client = aws_client(service)
    payloads = {
        "deregister_targets": {
            "TargetGroupArn": "arn:aws:elasticloadbalancing:x:y:targetgroup/z/1",
            "Targets": [{"Id": fx["instance_id"]}],
        },
        "register_targets": {
            "TargetGroupArn": "arn:aws:elasticloadbalancing:x:y:targetgroup/z/1",
            "Targets": [{"Id": fx["instance_id"]}],
        },
        "create_auto_scaling_group": {
            "AutoScalingGroupName": "vigilantis-probe-asg",
            "MinSize": 1,
            "MaxSize": 1,
            "LaunchTemplate": {"LaunchTemplateName": PROBE_LT_NAME},
        },
    }
    return attempt(getattr(client, operation), None, **payloads[operation])


def probe(row: TargetApi, fx) -> ProbeResult:
    service, _, operation = row.operation.partition(".")
    label, changed = (
        _probe_ec2(fx, operation) if service == "ec2" else _probe_other(service, operation, fx)
    )
    return ProbeResult(row.operation, row.runbooks, row.verdict, label, changed)


# ------------------------------------------------------------------ 보고
def run_all(fx=None) -> list[ProbeResult]:
    """전 작업 실측. 회귀 테스트도 이 함수를 쓴다."""
    fx = fx or collect_fixtures()
    return [probe(row, fx) for row in TARGET_API_MATRIX]


def _print_table(results: list[ProbeResult]) -> None:
    print(f"{'AWS 작업':<44} {'예외':<24} {'자원변경':<9} {'판정':<10} 표와 일치")
    print("-" * 100)
    for r in results:
        changed = "변경됨" if r.resource_changed else "없음"
        print(
            f"{r.operation:<44} {r.exception:<24} {changed:<9} {r.observed:<10} "
            f"{'예' if r.matches else '아니오 (표=' + r.expected + ')'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="확정 10종 target_api의 DryRun 지원 여부 실측 (ADR-0007 §6)"
    )
    parser.add_argument("--json", action="store_true", help="PR 첨부용 JSON으로 출력")
    args = parser.parse_args()

    endpoint = require_localstack()
    results = run_all()
    mismatched = [r.operation for r in results if not r.matches]
    changed = [r.operation for r in results if r.resource_changed]

    if args.json:
        print(
            json.dumps(
                {
                    "endpoint": endpoint,
                    "results": [
                        {
                            "operation": r.operation,
                            "runbooks": list(r.runbooks),
                            "exception": r.exception,
                            "resource_changed": r.resource_changed,
                            "observed": r.observed,
                            "expected": r.expected,
                        }
                        for r in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"[probe] endpoint={endpoint}\n")
        _print_table(results)
        if changed:
            print(
                "\n[경고] DryRun을 무시하고 자원을 실제로 바꾼 작업: "
                + ", ".join(changed)
                + "\n  이 작업들은 확인 단계가 조치를 집행한다는 뜻이다 — 조회 대체가 필수다."
            )
        if not mismatched:
            print("\n[결과] 표와 일치")

    if mismatched:
        # 표와 어긋나면 실패로 끝낸다 — CI·수동 실행 어느 쪽에서도 눈에 띄게.
        # JSON은 stdout에 그대로 두고 사유만 stderr로 보낸다(첨부본이 오염되지 않게)
        print(
            "[결과] 표와 불일치 — ADR-0007 §Context 표와 scripts/probe_dryrun.py "
            "TARGET_API_MATRIX를 함께 갱신할 것: " + ", ".join(mismatched),
            file=sys.stderr,
        )
    sys.exit(1 if mismatched else 0)


if __name__ == "__main__":
    main()
