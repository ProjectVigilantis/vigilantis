# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 자산 자동 원복(Auto-Rollback) 동작입니다. get_waiter로 개별 실행의 Status Check
# 결과를 보고, 기동 실패·타임아웃이면 BackupRecord의 이전 스펙으로 되돌립니다.
# 비종료 실행 회수 스캔은 dispatcher.py가, ActionExecution 상태 전이·커밋은
# workflows.py가 소유합니다 — services/aws/backup.py와
# workflows.store_instance_spec_backup()이 나눈 것과 같은 경계입니다.
#
# 현재 범위: 2/2 Status Check 판정 = wait_for_status_check(). (Issue #240)
#   - precheck()·execute_rightsizing()과 같은 규약으로 **예외를 던지지 않는다.**
#     AWS 오류는 errors.reason_code_for() 표로 분류해 결과에 싣는다. 유일한 예외는
#     인스턴스 ARN이 아닌 값이 들어온 경우인데, 그것은 자산 상태에 대한 판정이
#     아니라 호출부 배선 오류다(ADR-0007 §1이 backup_loader 미배선을 예외로 둔
#     것과 같은 구분 — execute_rightsizing이 같은 ARN을 이미 거절하므로 단계가
#     남은 실행이 여기 올 때 인스턴스 ARN이 아닐 수 없다).
#   - 판정 경계는 **인프라 부팅까지다.** 2/2 Status Check는 시스템·인스턴스 도달성
#     확인이며 앱 레벨 Health Check는 별개 축이다. 여기의 OK는 "AWS가 인스턴스를
#     정상 기동시켰다"까지만 말하고 서비스가 살아 있다는 뜻이 아니다.
#   - 대기 시간은 설정값이다(STATUS_CHECK_WAIT_*). 시연에서 조여야 할 값이라
#     코드 상수로 굳히지 않는다.
#
# [남은 작업]
# 1. 실패 시 BackupRecord 이전 스펙으로 자동 원복 — RUNBOOK_EC2_REVERT_SIZE 실행과
#    trigger_source=AUTO_ON_FAILURE 발동 경로 (Issue #241)
# 2. 원복 결과를 반환 — ActionExecution 상태 기록·알림은 호출부 소유
#
# 대기는 호출 스레드를 붙잡습니다. 스캔 잡이 겹쳐 돌지 않으므로(dispatcher.py
# max_instances=1) 판정 1건이 최대 Delay×MaxAttempts만큼 다음 스캔을 미룹니다 —
# worker 1개 전제(ADR-0005 실행 토폴로지 미결정)에서 받아들이는 비용이고, 그래서
# 기본값을 waiter 기본(15초×40회=10분)이 아니라 3분으로 줄여 둡니다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, unique
from typing import Any, Mapping, Optional

from botocore.exceptions import BotoCoreError, ClientError, WaiterError

from schemas.precheck import PrecheckReasonCode

from config import get_settings

from .client import aws_client
from .errors import reason_code_for
from .executor import parse_arn

logger = logging.getLogger("vigilantis.aws")

# 2/2 = 시스템 상태 검사 + 인스턴스 상태 검사. boto3의 이 waiter는 둘 다 ok일 때만
# 통과한다(DescribeInstanceStatus의 SystemStatus·InstanceStatus).
WAITER_NAME = "instance_status_ok"
_OP_DESCRIBE_STATUS = "ec2.describe_instance_status"

# 확정적으로 "부팅 성공이 아니다"라고 읽는 인스턴스 상태. pending은 아직 진행
# 중이므로 여기 없다 — 그 구분이 실패와 타임아웃을 가른다.
_NOT_BOOTING_STATES: frozenset[str] = frozenset(
    {"stopping", "stopped", "shutting-down", "terminated"}
)

# 검사 결과가 impaired면 AWS가 이미 이상으로 판정한 것이라 더 기다릴 이유가 없다.
# initializing·insufficient-data는 아직 판정 전이라 타임아웃 쪽이다.
_IMPAIRED = "impaired"


@unique
class StatusCheckVerdict(str, Enum):
    """2/2 Status Check 판정 3분기."""

    OK = "OK"                # 2/2 통과 — 인프라 부팅까지 정상
    FAILED = "FAILED"        # 확정적으로 부팅 실패 (정지·종료·impaired)
    TIMED_OUT = "TIMED_OUT"  # 제한 시간 안에 결론이 나지 않음


@dataclass(frozen=True)
class StatusCheckOutcome:
    """판정 1건. 상태 기록·원복 발동은 호출부 몫이다(dispatcher.py).

    reason_code가 TIMED_OUT과 함께 채워지면 "2/2가 되지 않았다"가 아니라 **AWS에
    물어보지 못해 결론을 내지 못했다**는 뜻이다. 두 경우 모두 성공으로 확정할 근거가
    없어 같은 분기로 가지만, 자동 원복을 걸 때는 구분이 필요하다 — 일시적인 조회
    실패로 멀쩡한 인스턴스를 되돌리지 않도록 이 값을 남긴다 (Issue #241).
    """

    verdict: StatusCheckVerdict
    summary: str
    reason_code: Optional[PrecheckReasonCode] = None
    instance_state: Optional[str] = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def booted(self) -> bool:
        return self.verdict is StatusCheckVerdict.OK


def _waiter_config(delay_seconds: Optional[int], max_attempts: Optional[int]) -> dict:
    """대기 설정 — 인자가 우선이고, 없으면 프로세스 설정값이다.

    단위 테스트가 설정 캐시를 건드리지 않고도 즉시 끝낼 수 있도록 인자를 연다.
    """
    settings = None
    if delay_seconds is None or max_attempts is None:
        settings = get_settings()
    return {
        "Delay": (
            delay_seconds
            if delay_seconds is not None
            else settings.STATUS_CHECK_WAIT_DELAY_SECONDS
        ),
        "MaxAttempts": (
            max_attempts
            if max_attempts is not None
            else settings.STATUS_CHECK_WAIT_MAX_ATTEMPTS
        ),
    }


def _first_status(response: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(response, Mapping):
        return None
    for item in response.get("InstanceStatuses") or []:
        if isinstance(item, Mapping):
            return item
    return None


def _check_values(status: Mapping[str, Any]) -> list[str]:
    """시스템·인스턴스 두 검사의 상태 문자열."""
    values: list[str] = []
    for key in ("SystemStatus", "InstanceStatus"):
        block = status.get(key)
        if isinstance(block, Mapping) and isinstance(block.get("Status"), str):
            values.append(block["Status"])
    return values


def _describe_summary(state: str, checks: list[str]) -> str:
    return f"{'/'.join(checks) or '검사 결과 없음'} (상태 {state or '알 수 없음'})"


def _classify_failure(ec2: Any, instance_id: str) -> StatusCheckOutcome:
    """waiter가 통과하지 못했다 — 실패인지 타임아웃인지 한 번 더 물어 가른다.

    waiter만으로는 갈리지 않는다. instance_status_ok에는 실패 acceptor가 없어
    impaired든 initializing이든 똑같이 MaxAttempts를 소진하고, 게다가
    DescribeInstanceStatus는 기본적으로 running 인스턴스만 돌려주므로 기동에
    실패해 stopped로 떨어진 인스턴스는 **빈 응답**으로만 나타난다. 그래서
    IncludeAllInstances로 한 번 더 조회해 상태를 직접 읽는다.
    """
    try:
        response = ec2.describe_instance_status(
            InstanceIds=[instance_id], IncludeAllInstances=True
        )
    except (ClientError, BotoCoreError) as exc:
        code = reason_code_for(exc)
        if code is PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND:
            # 인스턴스가 없으면 2/2는 영원히 오지 않는다 — 기다릴 이유가 없다
            return StatusCheckOutcome(
                verdict=StatusCheckVerdict.FAILED,
                summary=f"인스턴스를 찾을 수 없습니다: {instance_id}",
                reason_code=code,
            )
        return StatusCheckOutcome(
            verdict=StatusCheckVerdict.TIMED_OUT,
            summary=f"상태 조회 실패로 판정 보류: {type(exc).__name__}",
            reason_code=code,
        )

    status = _first_status(response)
    if status is None:
        return StatusCheckOutcome(
            verdict=StatusCheckVerdict.FAILED,
            summary=f"상태 응답에 인스턴스가 없습니다: {instance_id}",
            reason_code=PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND,
        )

    state = str((status.get("InstanceState") or {}).get("Name") or "")
    checks = _check_values(status)
    if state in _NOT_BOOTING_STATES:
        return StatusCheckOutcome(
            verdict=StatusCheckVerdict.FAILED,
            summary=f"기동 실패 — 인스턴스 상태가 {state}입니다",
            instance_state=state,
        )
    if _IMPAIRED in checks:
        return StatusCheckOutcome(
            verdict=StatusCheckVerdict.FAILED,
            summary=f"Status Check 실패 — {_describe_summary(state, checks)}",
            instance_state=state or None,
        )
    return StatusCheckOutcome(
        verdict=StatusCheckVerdict.TIMED_OUT,
        summary=f"제한 시간 안에 2/2에 도달하지 못했습니다 — {_describe_summary(state, checks)}",
        instance_state=state or None,
    )


def wait_for_status_check(
    target_arn: str,
    *,
    delay_seconds: Optional[int] = None,
    max_attempts: Optional[int] = None,
) -> StatusCheckOutcome:
    """기동한 인스턴스의 2/2 Status Check를 확인한다 — 3분기 판정. (Issue #240)

    **성공 판정의 경계는 인프라 부팅까지다.** 2/2가 통과했다는 것은 AWS가 시스템과
    인스턴스 도달성을 정상으로 본다는 뜻이고, 그 위에서 앱이 살아 있는지는 별개
    축이다(앱 Health Check). 부팅 성공·앱 비정상을 SUCCESS로 오판정하지 않도록
    이 함수의 어떤 판정 문구도 서비스 정상을 주장하지 않는다.

    예외를 던지지 않는다. AWS 오류는 errors.reason_code_for() 표로 분류해 결과에
    싣는다 — 판정 하나가 예외로 끊기면 같은 스캔의 나머지 실행이 함께 멈춘다.
    """
    target = parse_arn(target_arn)
    if target is None or target.resource_type != "instance":
        # 배선 오류다(파일 헤더). 판정으로 삼키면 멀쩡한 실행에 "기동 실패"
        # 기록이 붙고, 그 기록이 자동 원복의 입력이 된다.
        raise ValueError(f"인스턴스 ARN이 아닙니다: {target_arn}")

    instance_id = target.resource_id
    ec2 = aws_client("ec2", target.region)
    config = _waiter_config(delay_seconds, max_attempts)

    try:
        ec2.get_waiter(WAITER_NAME).wait(InstanceIds=[instance_id], WaiterConfig=config)
    except WaiterError:
        outcome = _classify_failure(ec2, instance_id)
        logger.warning(
            "status_check_not_ok",
            extra={
                "instance_id": instance_id,
                "verdict": outcome.verdict.value,
                "aws_operation": _OP_DESCRIBE_STATUS,
            },
        )
        return outcome
    except (ClientError, BotoCoreError) as exc:
        # waiter를 만들거나 부르는 단계에서 끊긴 경우 — 자산 상태를 본 적이 없으므로
        # 실패로 확정하지 않는다
        return StatusCheckOutcome(
            verdict=StatusCheckVerdict.TIMED_OUT,
            summary=f"Status Check 확인 실패로 판정 보류: {type(exc).__name__}",
            reason_code=reason_code_for(exc),
        )

    return StatusCheckOutcome(
        verdict=StatusCheckVerdict.OK,
        summary="2/2 Status Check 통과(인프라 부팅까지 — 앱 Health Check는 별개 축)",
        instance_state="running",
    )
