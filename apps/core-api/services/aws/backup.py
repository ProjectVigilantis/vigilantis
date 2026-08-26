# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 스펙 JSON 백업 모듈 — 조치 직전 대상 자산의 현재 상태를 읽어 백업 레코드
# payload를 만든다. (ADR-0004 롤백 공통 정책 ③, 런북 명세서 FinOps-01)
#
# 이 모듈이 존재하는 이유는 하나다. **자산을 바꾼 뒤에는 바꾸기 전 값을 어디서도
# 얻을 수 없다.** `RUNBOOK_EC2_REVERT_SIZE`는 원복 타입을 백업 레코드에서만
# 로드하므로(ADR-0004 정책 ③), 여기서 캡처에 실패하면 조치를 시작해서는 안 된다 —
# Auto-Rollback 셀링포인트가 통째로 근거를 잃는다.
#
# 경계
#   - DB를 모른다. executor.precheck()가 백업 **조회**를 주입받는 것과 같은 이유로
#     여기서도 저장은 하지 않는다 — 캡처가 DB 없이 단위 테스트된다. 저장·결속·
#     커밋 순서는 workflows.store_instance_spec_backup이 소유한다.
#   - 예외를 던지지 않는다. AWS 오류는 errors.reason_code_for()의 공용 표로
#     분류해 사유 코드로 돌려준다 — precheck·실행·롤백이 같은 표를 쓴다.
#
# [남은 작업] 나머지 백업 3종(SAVE_SG_FULL_RULES_JSON·SAVE_CURRENT_SG_AND_TG_MAPPING·
# RECORD_NACL_RULE_INDEX). payload 형태는 executor의 롤백 precheck가 이미 읽고
# 있으므로 계약은 정해져 있다 — 캡처 함수만 이 파일에 붙이면 된다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from schemas.backups import BackupType, InstanceSpecBackup
from schemas.precheck import PrecheckReasonCode

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
    )
