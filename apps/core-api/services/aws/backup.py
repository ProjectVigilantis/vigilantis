# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 백업 캡처 모듈 — 조치 직전 AWS를 1회 조회해 백업 레코드 payload를 만든다.
# (ADR-0004 롤백 공통 정책 ③, ADR-0008 §1 수명주기·§5 payload 목록)
#
#   capture_instance_spec      SAVE_INSTANCE_SPEC_JSON   RIGHTSIZING → REVERT_SIZE
#   capture_nacl_rule_index    RECORD_NACL_RULE_INDEX    NACL_ADD_DENY → NACL_RESTORE
#   capture_sg_full_rules      SAVE_SG_FULL_RULES_JSON   SG_DELETE_ISOLATED → SG_RECREATE
#   capture_sg_and_tg_mapping  SAVE_CURRENT_SG_AND_TG_MAPPING  EC2_ISOLATE → EC2_UNISOLATE
#
# 이 모듈이 존재하는 이유는 하나다. **자산을 바꾼 뒤에는 되돌릴 근거를 어디서도
# 얻을 수 없다.** `RUNBOOK_EC2_REVERT_SIZE`는 원복 타입을 백업 레코드에서만
# 로드하므로(ADR-0004 정책 ③), 여기서 캡처에 실패하면 조치를 시작해서는 안 된다 —
# Auto-Rollback 셀링포인트가 통째로 근거를 잃는다.
#
# 캡처가 읽는 것은 다르다. 스펙 JSON·SG 전체 규칙·SG/TG 매핑은 **바꾸기 전 값**을 읽고, NACL
# 규칙 index는 삽입이 기존 값을 덮지 않으므로 **넣을 규칙의 좌표**를 적으면서 그 슬롯이
# 비어 있는지 확인한다. 어느 쪽이든 실패하면 조치를 시작하지 않는다는 계약은 같다
# (ADR-0008 §1 ①).
#
# 경계
#   - DB를 모른다. executor.precheck()가 백업 **조회**를 주입받는 것과 같은 이유로
#     여기서도 저장은 하지 않는다 — 캡처가 DB 없이 단위 테스트된다. 저장·결속·
#     커밋 순서는 workflows의 store_*_backup 함수가 소유한다.
#   - 예외를 던지지 않는다. AWS 오류는 errors.reason_code_for()의 공용 표로
#     분류해 사유 코드로 돌려준다 — precheck·실행·롤백이 같은 표를 쓴다.
#
# 백업 4종(ADR-0008 §5)의 캡처가 전부 이 파일에 있다 — 남은 작업 없음.
# SG/TG 매핑은 롤백 precheck(UNISOLATE)가 읽던 키(security_group_ids·target_group_arn)를
# 그대로 두고 ADR-0008 §5의 target_port와 ENI별 목록(network_interfaces)을 더했다
# (schemas.backups.SgAndTgMappingBackup).
# ==============================================================================

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from schemas.autoscaling import (
    EXECUTION_TAG_KEY,
    SOURCE_INSTANCE_TAG_KEY,
    AutoscalingExecutionPlan,
)
from schemas.backups import (
    BackupType,
    EniSecurityGroups,
    InstanceSpecBackup,
    NaclRuleIndexBackup,
    ScaleInProtectionBackup,
    SgAndTgMappingBackup,
    SgFullRulesBackup,
)
from schemas.precheck import PrecheckReasonCode
from schemas.runbook_parameters import (
    NACL_ADD_DENY_EGRESS,
    NACL_DENY_ACTION,
    NACL_PROTOCOL_NUMBERS,
)

from .client import aws_client
from .errors import reason_code_for

logger = logging.getLogger("vigilantis.aws")

R = PrecheckReasonCode


@dataclass(frozen=True)
class BackupCapture:
    """캡처 1회분. payload가 있으면 성공, 없으면 reason_code·detail이 사유다."""

    backup_type: str
    payload: dict | None = None
    reason_code: PrecheckReasonCode | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if (self.payload is None) == (self.reason_code is None):
            raise ValueError("payload와 reason_code 중 정확히 하나만 채웁니다")

    @property
    def captured(self) -> bool:
        return self.payload is not None


def _fail(backup_type: str, code: PrecheckReasonCode, detail: str) -> BackupCapture:
    logger.warning(
        "backup_capture_failed",
        extra={"backup_type": backup_type, "reason_code": code.value, "detail": detail},
    )
    return BackupCapture(backup_type=backup_type, reason_code=code, detail=detail)


def _describe_instance(instance_id: str, region: str):
    """인스턴스 1건. (인스턴스, 사유 코드) 짝 — 없으면 코드가 채워진다.

    executor._instance와 같은 조회지만 import하지 않는다. 실행 본체가 붙으면
    executor가 이 모듈을 부르게 되므로, 반대 방향 의존을 지금 만들지 않는다.
    """
    try:
        res = aws_client("ec2", region).describe_instances(InstanceIds=[instance_id])
    except (ClientError, BotoCoreError) as exc:
        return None, reason_code_for(exc)
    for reservation in res.get("Reservations") or []:
        for instance in reservation.get("Instances") or []:
            return instance, None
    return None, R.PRECHECK_TARGET_NOT_FOUND


def _optional(value: Any) -> str | None:
    """AWS가 비워 보낸 값은 None으로 둔다 — 빈 문자열을 스펙으로 기록하지 않는다."""
    return value if isinstance(value, str) and value.strip() else None


def capture_instance_spec(instance_id: str, region: str) -> BackupCapture:
    """`SAVE_INSTANCE_SPEC_JSON` — RIGHTSIZING 변경 직전 인스턴스 스펙.

    필수 3종(instance_id·instance_type·state)이 없으면 캡처 실패다. 그 상태로
    조치를 시작하면 원복 값이 없는 변경이 되므로, 여기서 막는 편이 옳다.
    """
    backup_type = BackupType.SAVE_INSTANCE_SPEC_JSON.value
    instance, code = _describe_instance(instance_id, region)
    if code is not None:
        return _fail(backup_type, code, f"인스턴스 조회 실패: {instance_id}")

    try:
        spec = _to_instance_spec(instance)
    except ValidationError as exc:
        # AWS 응답에 원복 필수 값이 없다 — 조회는 됐으므로 대상 상태 문제로 본다
        missing = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
        return _fail(
            backup_type,
            R.PRECHECK_INVALID_STATE,
            f"스펙 필수 값 누락: {', '.join(missing) or '알 수 없음'}",
        )
    return BackupCapture(backup_type=backup_type, payload=spec.model_dump(mode="json"))


def _eip_association_id(instance: Mapping[str, Any]) -> str | None:
    """EIP 연결 ID. 자동 할당 퍼블릭 IPv4에는 없다 — 그 부재가 곧 "정지하면 주소가
    바뀐다"는 뜻이라(ADR-0008 §5) None을 그대로 남긴다.

    AWS는 EIP일 때만 Association에 AssociationId(eipassoc-…)를 채운다. 자동 할당
    주소도 Association 블록 자체는 오지만 소유자가 amazon이고 이 키가 없다.
    """
    for eni in instance.get("NetworkInterfaces") or []:
        if not isinstance(eni, Mapping):
            continue
        association = eni.get("Association")
        if isinstance(association, Mapping):
            found = _optional(association.get("AssociationId"))
            if found is not None:
                return found
    return None


def _to_instance_spec(instance: Mapping[str, Any]) -> InstanceSpecBackup:
    return InstanceSpecBackup(
        instance_id=_optional(instance.get("InstanceId")),
        instance_type=_optional(instance.get("InstanceType")),
        state=_optional((instance.get("State") or {}).get("Name")),
        image_id=_optional(instance.get("ImageId")),
        architecture=_optional(instance.get("Architecture")),
        root_device_type=_optional(instance.get("RootDeviceType")),
        ebs_optimized=(
            bool(instance["EbsOptimized"]) if "EbsOptimized" in instance else None
        ),
        availability_zone=_optional(
            (instance.get("Placement") or {}).get("AvailabilityZone")
        ),
        vpc_id=_optional(instance.get("VpcId")),
        subnet_id=_optional(instance.get("SubnetId")),
        public_ip_address=_optional(instance.get("PublicIpAddress")),
        elastic_ip_association_id=_eip_association_id(instance),
    )


# ------------------------------------------------------------------ NACL 규칙 index
def _describe_network_acl(network_acl_id: str, region: str):
    """NACL 1건. (NACL, 사유 코드) 짝 — 없으면 코드가 채워진다.

    executor._network_acl과 같은 조회지만 import하지 않는다 — _describe_instance와
    같은 이유다(위 주석).
    """
    try:
        res = aws_client("ec2", region).describe_network_acls(
            NetworkAclIds=[network_acl_id]
        )
    except (ClientError, BotoCoreError) as exc:
        return None, reason_code_for(exc)
    acls = res.get("NetworkAcls") or []
    if not acls:
        return None, R.PRECHECK_TARGET_NOT_FOUND
    return acls[0], None


def _slot_taken(acl: Mapping[str, Any], rule_number: int, egress: bool) -> bool:
    """그 규칙 번호 슬롯이 이미 쓰이고 있는가."""
    for entry in acl.get("Entries") or []:
        if not isinstance(entry, Mapping):
            continue
        if (
            entry.get("RuleNumber") == rule_number
            and bool(entry.get("Egress")) == egress
        ):
            return True
    return False


def capture_nacl_rule_index(
    network_acl_id: str,
    region: str,
    *,
    rule_number: int,
    cidr_block: str,
    protocol: str,
) -> BackupCapture:
    """`RECORD_NACL_RULE_INDEX` — NACL_ADD_DENY가 넣을 deny 규칙의 좌표와 fingerprint.

    다른 캡처와 성격이 다르다. 조치 **이전** 값을 읽는 것이 아니라 조치가 **넣을**
    규칙을 적는다 — NACL 규칙 삽입은 기존 값을 덮지 않고 빈 슬롯에 넣는 조치라,
    되돌릴 때 필요한 것이 옛 값이 아니라 "우리가 넣은 것이 어느 것인가"이기 때문이다
    (schemas.backups.NaclRuleIndexBackup).

    그래도 AWS를 조회한다. **슬롯이 비어 있는지 확인하는 것이 이 캡처의 일**이다 —
    이미 쓰이는 번호라면 삽입은 어차피 NetworkAclEntryAlreadyExists로 거절되고(실측),
    무엇보다 그 슬롯의 규칙은 우리 것이 아니다. 확인 없이 레코드를 남기면 남의 규칙을
    가리키는 백업이 생기고, NACL_RESTORE가 그것을 근거로 삭제한다.

    가드레일 ④가 같은 것을 이미 본다(executor._precheck_nacl_add_deny). 그래도 여기서
    다시 보는 이유는 **판정과 실행 사이에 시간이 있기 때문**이다 — 관제자 승인 대기
    동안 제3자가 그 번호를 쓸 수 있고, 그 사이를 메우는 것이 조치 직전 캡처다.

    protocol은 이름 표기로 받아 **AWS 번호 표기로 바꿔 저장한다**. 저장 값이 대조할
    상대가 describe_network_acls의 Protocol이라 축을 맞춰야 한다
    (schemas.runbook_parameters.NACL_PROTOCOL_NUMBERS의 실측 주석).
    """
    backup_type = BackupType.RECORD_NACL_RULE_INDEX.value
    protocol_number = NACL_PROTOCOL_NUMBERS.get(protocol)
    if protocol_number is None:
        # 파라미터 계약(NaclProtocol)이 이미 걸러 내는 값이다 — 여기 오면 배선 문제라
        # 판정으로 돌려주되 사유를 파라미터 쪽으로 분류한다
        return _fail(
            backup_type,
            R.PRECHECK_PARAM_INVALID,
            f"알 수 없는 프로토콜 표기: {protocol}",
        )

    acl, code = _describe_network_acl(network_acl_id, region)
    if code is not None:
        return _fail(backup_type, code, f"NACL 조회 실패: {network_acl_id}")
    if _slot_taken(acl, rule_number, NACL_ADD_DENY_EGRESS):
        return _fail(
            backup_type,
            R.PRECHECK_INVALID_STATE,
            f"규칙 번호 {rule_number}가 이미 사용 중입니다(인바운드)",
        )

    try:
        record = NaclRuleIndexBackup(
            rule_number=rule_number,
            egress=NACL_ADD_DENY_EGRESS,
            cidr_block=cidr_block,
            protocol=protocol_number,
            rule_action=NACL_DENY_ACTION,
        )
    except ValidationError as exc:
        # 값이 계약을 벗어났다 — 이 상태로 삽입하면 되돌릴 근거가 없는 규칙이 남는다
        invalid = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
        return _fail(
            backup_type,
            R.PRECHECK_PARAM_INVALID,
            f"규칙 fingerprint 값이 계약을 벗어났습니다: {', '.join(invalid) or '알 수 없음'}",
        )
    return BackupCapture(
        backup_type=backup_type, payload=record.model_dump(mode="json")
    )


# ------------------------------------------------------------------ SG 전체 규칙
def _describe_security_group(group_id: str, region: str):
    """보안 그룹 1건. (SG, 사유 코드) 짝 — 없으면 코드가 채워진다.

    executor._security_group과 같은 조회지만 import하지 않는다 — _describe_instance와
    같은 이유다(위 주석).
    """
    try:
        res = aws_client("ec2", region).describe_security_groups(GroupIds=[group_id])
    except (ClientError, BotoCoreError) as exc:
        return None, reason_code_for(exc)
    groups = res.get("SecurityGroups") or []
    if not groups:
        return None, R.PRECHECK_TARGET_NOT_FOUND
    return groups[0], None


def capture_sg_full_rules(group_id: str, region: str) -> BackupCapture:
    """`SAVE_SG_FULL_RULES_JSON` — SG_DELETE_ISOLATED 삭제 직전 보안 그룹 전체.

    이 캡처가 담는 것이 원복의 **전부**다. SG를 지우고 나면 이름도 설명도 규칙도 AWS에
    다시 물을 수 없고, `RUNBOOK_SG_RECREATE`는 복원 대상 SG ID조차 파라미터로 받지
    않는다(schemas.backups.SgFullRulesBackup). 그래서 여기서 실패하면 삭제를 시작하지
    않는다(ADR-0008 §1 ①).

    규칙 목록은 `describe_security_groups`가 준 모양 그대로 담는다. 해석해서 담으면
    되붓는 쪽(`authorize_security_group_*`)이 받을 모양과 갈리고, 그 어긋남은 SG가 이미
    지워진 뒤에야 드러난다.

    **규칙이 0개인 SG도 정상 캡처다.** 미부착 SG는 대개 규칙이 비어 있고, 그것이 곧
    "이 SG는 아무것도 열지 않는다"는 사실이다 — 빈 목록을 실패로 보면 정작 지워야 할
    대상이 조치 대상에서 빠진다.
    """
    backup_type = BackupType.SAVE_SG_FULL_RULES_JSON.value
    group, code = _describe_security_group(group_id, region)
    if code is not None:
        return _fail(backup_type, code, f"보안 그룹 조회 실패: {group_id}")

    try:
        record = SgFullRulesBackup(
            group_name=_optional(group.get("GroupName")),
            description=_optional(group.get("Description")),
            vpc_id=_optional(group.get("VpcId")),
            ingress_permissions=list(group.get("IpPermissions") or []),
            egress_permissions=list(group.get("IpPermissionsEgress") or []),
            group_id=_optional(group.get("GroupId")),
        )
    except ValidationError as exc:
        # 조회는 됐는데 재생성에 필요한 값이 없다. VPC 밖 SG(EC2-Classic)가 그렇고,
        # 그런 SG는 이 백업으로 되살릴 수 없으므로 삭제도 시작하지 않는다.
        missing = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
        return _fail(
            backup_type,
            R.PRECHECK_INVALID_STATE,
            f"SG 재생성 필수 값 누락: {', '.join(missing) or '알 수 없음'}",
        )
    return BackupCapture(
        backup_type=backup_type, payload=record.model_dump(mode="json")
    )


# ------------------------------------------------------------------ SG·TG 매핑
# describe_target_health가 **등록되지 않은** 대상에도 돌려주는 설명의 사유 코드.
# Targets를 지정해 물으면 미등록 대상도 빈 목록이 아니라 unused / Target.NotRegistered
# 설명으로 온다(ADR-0007 §4 1차 개정 ④ — executor._precheck_isolate와 같은 판별).
TARGET_NOT_REGISTERED = "Target.NotRegistered"


def device_index(eni: Mapping[str, Any]) -> int:
    """정렬 키. DeviceIndex를 못 읽은 ENI는 맨 뒤로 — 주 ENI(0)를 잘못 고르지 않게.

    가드레일 ④의 주 ENI 선택(executor._primary_eni)도 이 키를 쓴다 — 캡처가 `security_group_ids`로
    담은 주 ENI와 DryRun이 대는 ENI가 같아야 하기 때문이다.
    """
    index = (eni.get("Attachment") or {}).get("DeviceIndex")
    return index if isinstance(index, int) else 1 << 30


def eni_security_groups(instance: Mapping[str, Any]) -> list[EniSecurityGroups]:
    """인스턴스의 ENI별 SG 목록. DeviceIndex 오름차순 — 첫 원소가 주 ENI다.

    응답 해석만 하는 공개 함수다. 실행(executor.current_eni_security_groups)과 판정이 같은 축을 같은
    방법으로 읽어야 하기 때문이다 — 캡처가 본 ENI 집합과 원복·판정이 보는 집합이
    갈리면 "되돌아왔는가"의 답이 자리마다 달라진다. 계약을 벗어난 값(ENI ID 형식·빈
    SG 목록)이면 ValidationError를 그대로 올린다.
    """
    interfaces = [
        eni
        for eni in instance.get("NetworkInterfaces") or []
        if isinstance(eni, Mapping)
    ]
    return [
        EniSecurityGroups(
            network_interface_id=eni.get("NetworkInterfaceId"),
            security_group_ids=[
                group.get("GroupId")
                for group in eni.get("Groups") or []
                if isinstance(group, Mapping)
            ],
        )
        for eni in sorted(interfaces, key=device_index)
    ]


def registered_ports(response: Mapping[str, Any], instance_id: str) -> dict[int, str]:
    """describe_target_health 응답 → 이 인스턴스가 등록된 포트 → TargetHealth State.

    `Target.NotRegistered` 설명은 등록으로 세지 않는다(위 상수 주석). 응답 해석만 하는
    공개 함수다 — 실행·판정(executor.current_target_registration)이 같은 판별로 등록
    여부를 읽어야 하기 때문이다. 조회는 각 모듈이 자기 클라이언트로 한다(_describe_instance
    주석과 같은 이유로 executor를 import하지 않는다).
    """
    ports: dict[int, str] = {}
    for description in response.get("TargetHealthDescriptions") or []:
        target = description.get("Target") or {}
        health = description.get("TargetHealth") or {}
        if target.get("Id") != instance_id:
            continue
        if health.get("Reason") == TARGET_NOT_REGISTERED:
            continue
        port = target.get("Port")
        if isinstance(port, int):
            ports[port] = str(health.get("State") or "")
    return ports


def _registered_ports(target_group_arn: str, instance_id: str, region: str):
    """(등록 포트 → State, 사유 코드) 짝."""
    try:
        res = aws_client("elbv2", region).describe_target_health(
            TargetGroupArn=target_group_arn, Targets=[{"Id": instance_id}]
        )
    except (ClientError, BotoCoreError) as exc:
        return None, reason_code_for(exc)
    return registered_ports(res, instance_id), None


# ASG가 기동한 인스턴스에 AWS가 자동으로 다는 태그. 이 태그가 없는 인스턴스는 ASG를 더
# 조회하지 않는다 — 다른 격리 대상은 호출도 동작도 그대로다.
ASG_NAME_TAG_KEY = "aws:autoscaling:groupName"
# 우리 스모크 울타리 태그(schemas.autoscaling.PRESERVED_SOURCE_TAG_KEYS)
SMOKE_TAG_KEY = "vigilantis:smoke"

# 보조 조치(ASG 보호)의 AWS 호출 한계. 격리가 이 호출 때문에 늘어지지 않게 SDK 재시도와
# 타임아웃을 좁힌다 — 호출 횟수는 각 함수가 고정한다(반복·대기 없음).
AUTOSCALING_BOUNDED_CLIENT: dict[str, Any] = {
    # botocore의 max_attempts는 "재시도 횟수"라 총 시도 수는 total_max_attempts로 못 박는다
    "retries": {"total_max_attempts": 2, "mode": "standard"},
    "connect_timeout": 3,
    "read_timeout": 5,
}
# 보호 **쓰기·확인**용 — SDK가 응답 유실을 재전송하지 않는다(총 1회). 재전송 여부는 우리가
# 확인 조회로 정한다. #446의 기본 클라이언트 설정은 바꾸지 않는다.
AUTOSCALING_PROTECTION_CLIENT: dict[str, Any] = {
    "retries": {"total_max_attempts": 1, "mode": "standard"},
    "connect_timeout": 3,
    "read_timeout": 5,
}

# 소유 ASG를 가린 저장 계획 조회 — 호출부(workflows)가 DB로 구현한다. 이 모듈은 DB를 모른다.
ManagedPlanLookup = Callable[[str], AutoscalingExecutionPlan | None]


def _unknown(reason: str) -> ScaleInProtectionBackup:
    return ScaleInProtectionBackup(status="UNKNOWN", reason=reason)


def _not_managed(reason: str) -> ScaleInProtectionBackup:
    return ScaleInProtectionBackup(status="NOT_MANAGED", reason=reason)


def _tag_map(items: Any) -> dict[str, Any]:
    return {
        str(tag.get("Key")): tag.get("Value")
        for tag in items or ()
        if isinstance(tag, Mapping)
    }


def capture_scale_in_protection(
    instance: Mapping[str, Any],
    instance_id: str,
    region: str,
    plan_lookup: ManagedPlanLookup | None,
) -> ScaleInProtectionBackup:
    """격리 직전 ASG scale-in 보호 상태 관측. **실패해도 예외·캡처 실패가 되지 않는다.**

    보호는 보조 조치라 이 관측이 SG/TG 백업 캡처의 성패를 바꾸지 않는다 — 못 읽으면
    `UNKNOWN`(보호 안 됨이 아니다)으로 남기고, 그 상태에서는 보호 쓰기도 복원도 없다.

    "managed 스모크 ASG"의 판정은 이름이 아니라 **소유 근거 셋의 일치**다.
      ① 인스턴스가 ASG 멤버임(`aws:autoscaling:groupName` 태그 + ASG 응답의 Instances)
      ② ASG의 `vigilantis:execution-id`·`vigilantis:source-instance` 태그
      ③ 그 실행의 저장된 #446 계획(plan_lookup) — ASG 이름·원본·실행 ID가 같고 스모크 태그가 있음
    하나라도 다르면 `NOT_MANAGED`다.
    """
    asg_name = next(
        (
            tag.get("Value")
            for tag in instance.get("Tags") or ()
            if isinstance(tag, Mapping) and tag.get("Key") == ASG_NAME_TAG_KEY
        ),
        None,
    )
    if not isinstance(asg_name, str) or not asg_name.strip():
        return _not_managed("NOT_ASG_MEMBER")

    try:
        response = aws_client(
            "autoscaling", region, **AUTOSCALING_BOUNDED_CLIENT
        ).describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    except (ClientError, BotoCoreError):
        return _unknown("LOOKUP_FAILED")
    group = next(
        (
            item
            for item in response.get("AutoScalingGroups") or ()
            if isinstance(item, Mapping)
            and item.get("AutoScalingGroupName") == asg_name
        ),
        None,
    )
    if group is None:
        return _unknown("ASG_NOT_FOUND")
    member = next(
        (
            item
            for item in group.get("Instances") or ()
            if isinstance(item, Mapping) and item.get("InstanceId") == instance_id
        ),
        None,
    )
    if member is None:
        return _unknown("MEMBERSHIP_UNVERIFIED")

    tags = _tag_map(group.get("Tags"))
    execution_id = tags.get(EXECUTION_TAG_KEY)
    source_instance = tags.get(SOURCE_INSTANCE_TAG_KEY)
    if not isinstance(execution_id, str) or not isinstance(source_instance, str):
        return _not_managed("NO_OWNERSHIP_TAGS")
    if plan_lookup is None:
        return _unknown("PLAN_LOOKUP_UNAVAILABLE")
    try:
        plan = plan_lookup(execution_id)
    except Exception:  # noqa: BLE001 — 보조 관측이라 DB 오류가 캡처를 깨지 않는다
        logger.warning(
            "scale_in_plan_lookup_failed", extra={"execution_id": execution_id}
        )
        return _unknown("PLAN_LOOKUP_FAILED")
    if plan is None:
        return _not_managed("NO_SOURCE_PLAN")
    smoke = {tag.key: tag.value for tag in plan.source.preserved_tags}.get(
        SMOKE_TAG_KEY
    )
    if (
        plan.execution_id != execution_id
        or plan.asg_name != asg_name
        or plan.source.instance_id != source_instance
        or smoke is None
        or tags.get(SMOKE_TAG_KEY) != smoke
    ):
        return _not_managed("OWNERSHIP_MISMATCH")

    previous = member.get("ProtectedFromScaleIn")
    if not isinstance(previous, bool):
        return _unknown("PRIOR_STATE_UNKNOWN")
    return ScaleInProtectionBackup(
        status="MANAGED",
        asg_name=asg_name,
        previous_protected=previous,
        autoscaling_execution_id=execution_id,
    )


def capture_sg_and_tg_mapping(
    instance_id: str,
    region: str,
    *,
    target_group_arn: str,
    plan_lookup: ManagedPlanLookup | None = None,
) -> BackupCapture:
    """`SAVE_CURRENT_SG_AND_TG_MAPPING` — EC2_ISOLATE 격리 직전 ENI별 SG와 TG 등록.

    격리는 ENI의 SG를 격리 SG로 **덮어쓰고** TG에서 대상을 뺀다. 조치 뒤에는 원래 SG도
    등록 포트도 AWS에 다시 물을 수 없으므로 여기서 실패하면 격리를 시작하지 않는다
    (ADR-0008 §1 ①) — `RUNBOOK_EC2_UNISOLATE`는 원복 값을 이 백업에서만 읽는다.

    **ENI는 전부 담는다.** 격리 실행이 인스턴스의 모든 ENI를 바꾸므로 원복도 ENI 단위다
    (schemas.backups.SgAndTgMappingBackup의 `network_interfaces`).

    **TG 등록은 포트까지 담는다**(ADR-0008 §5 `target_port`). 등록이 없으면 실패다 —
    가드레일 ④가 같은 조건(`Target.NotRegistered`는 미등록)으로 이미 거르지만, 판정과
    실행 사이에 대상이 빠졌을 수 있다(capture_nacl_rule_index가 슬롯을 다시 보는 것과
    같은 이유). 미등록 대상을 격리하면 재등록할 포트가 없어 해제가 원상 복구가 아니다.

    **같은 TG에 여러 포트로 등록된 인스턴스는 받지 않는다.** 계약의 `target_port`는
    하나라(ADR-0008 §5), 한 포트만 담으면 해제가 나머지 등록을 되살리지 못한다 — 되돌릴
    수 없는 변경을 만들지 않도록 시작 전에 멈춘다.
    """
    backup_type = BackupType.SAVE_CURRENT_SG_AND_TG_MAPPING.value
    instance, code = _describe_instance(instance_id, region)
    if code is not None:
        return _fail(backup_type, code, f"인스턴스 조회 실패: {instance_id}")

    try:
        interfaces = eni_security_groups(instance)
    except ValidationError:
        return _fail(
            backup_type,
            R.PRECHECK_INVALID_STATE,
            "ENI 식별자 또는 SG 목록을 읽지 못했습니다",
        )
    if not interfaces:
        return _fail(backup_type, R.PRECHECK_INVALID_STATE, "인스턴스에 ENI가 없습니다")

    ports, code = _registered_ports(target_group_arn, instance_id, region)
    if code is not None:
        return _fail(backup_type, code, "Target Group 등록 조회 실패")
    if not ports:
        return _fail(
            backup_type,
            R.PRECHECK_TARGET_NOT_FOUND,
            "대상이 Target Group에 등록돼 있지 않습니다",
        )
    if len(ports) > 1:
        return _fail(
            backup_type,
            R.PRECHECK_INVALID_STATE,
            f"같은 Target Group에 여러 포트로 등록돼 있습니다({sorted(ports)})",
        )

    try:
        record = SgAndTgMappingBackup(
            security_group_ids=list(interfaces[0].security_group_ids),
            target_group_arn=target_group_arn,
            target_port=next(iter(ports)),
            network_interfaces=interfaces,
            # 보조 관측 — 실패·미확인이어도 위 SG/TG 캡처는 그대로 성공이다
            scale_in_protection=capture_scale_in_protection(
                instance, instance_id, region, plan_lookup
            ),
        )
    except ValidationError as exc:
        invalid = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
        return _fail(
            backup_type,
            R.PRECHECK_INVALID_STATE,
            f"SG/TG 매핑이 계약을 벗어났습니다: {', '.join(invalid) or '알 수 없음'}",
        )
    return BackupCapture(
        backup_type=backup_type, payload=record.model_dump(mode="json")
    )
