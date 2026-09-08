# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 백업 캡처 모듈 — 조치 직전 AWS를 1회 조회해 백업 레코드 payload를 만든다.
# (ADR-0004 롤백 공통 정책 ③, ADR-0008 §1 수명주기·§5 payload 목록)
#
#   capture_instance_spec      SAVE_INSTANCE_SPEC_JSON   RIGHTSIZING → REVERT_SIZE
#   capture_nacl_rule_index    RECORD_NACL_RULE_INDEX    NACL_ADD_DENY → NACL_RESTORE
#
# 이 모듈이 존재하는 이유는 하나다. **자산을 바꾼 뒤에는 되돌릴 근거를 어디서도
# 얻을 수 없다.** `RUNBOOK_EC2_REVERT_SIZE`는 원복 타입을 백업 레코드에서만
# 로드하므로(ADR-0004 정책 ③), 여기서 캡처에 실패하면 조치를 시작해서는 안 된다 —
# Auto-Rollback 셀링포인트가 통째로 근거를 잃는다.
#
# 두 캡처가 읽는 것은 다르다. 스펙 JSON은 **바꾸기 전 값**을 읽고, NACL 규칙 index는
# 삽입이 기존 값을 덮지 않으므로 **넣을 규칙의 좌표**를 적으면서 그 슬롯이 비어 있는지
# 확인한다. 어느 쪽이든 실패하면 조치를 시작하지 않는다는 계약은 같다(ADR-0008 §1 ①).
#
# 경계
#   - DB를 모른다. executor.precheck()가 백업 **조회**를 주입받는 것과 같은 이유로
#     여기서도 저장은 하지 않는다 — 캡처가 DB 없이 단위 테스트된다. 저장·결속·
#     커밋 순서는 workflows의 store_*_backup 함수가 소유한다.
#   - 예외를 던지지 않는다. AWS 오류는 errors.reason_code_for()의 공용 표로
#     분류해 사유 코드로 돌려준다 — precheck·실행·롤백이 같은 표를 쓴다.
#
# [남은 작업] 나머지 백업 2종(SAVE_SG_FULL_RULES_JSON·SAVE_CURRENT_SG_AND_TG_MAPPING).
# payload 형태는 executor의 롤백 precheck가 이미 읽고 있으므로 계약은 정해져
# 있다 — 캡처 함수만 이 파일에 붙이면 된다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from schemas.backups import BackupType, InstanceSpecBackup, NaclRuleIndexBackup
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
    payload: Optional[dict] = None
    reason_code: Optional[PrecheckReasonCode] = None
    detail: Optional[str] = None

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


def _optional(value: Any) -> Optional[str]:
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


def _eip_association_id(instance: Mapping[str, Any]) -> Optional[str]:
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
        availability_zone=_optional((instance.get("Placement") or {}).get("AvailabilityZone")),
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
        if entry.get("RuleNumber") == rule_number and bool(entry.get("Egress")) == egress:
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
    return BackupCapture(backup_type=backup_type, payload=record.model_dump(mode="json"))
