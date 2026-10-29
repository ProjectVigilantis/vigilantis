# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 자산 자동 원복(Auto-Rollback)의 판정 축입니다. 개별 실행의 2/2 Status Check 결과를 보고
# "되돌려야 하는가"를 가릅니다 — 되돌리는 동작 자체는 여기 없습니다.
# 비종료 실행 회수 스캔은 dispatcher.py가, ActionExecution 상태 전이·커밋은
# workflows.py가 소유합니다 — services/aws/backup.py와
# workflows.store_instance_spec_backup()이 나눈 것과 같은 경계입니다.
#
# 현재 범위: 2/2 Status Check 판정 = probe_status_check(). (Issue #240 · #434 1-3)
#   - precheck()·execute_rightsizing()과 같은 규약으로 **예외를 던지지 않는다.**
#     AWS 오류는 errors.reason_code_for() 표로 분류해 결과에 싣는다. 유일한 예외는
#     인스턴스 ARN이 아닌 값이 들어온 경우인데, 그것은 자산 상태에 대한 판정이
#     아니라 호출부 배선 오류다(ADR-0007 §1이 backup_loader 미배선을 예외로 둔
#     것과 같은 구분 — execute_rightsizing이 같은 ARN을 이미 거절하므로 단계가
#     남은 실행이 여기 올 때 인스턴스 ARN이 아닐 수 없다).
#   - 판정 경계는 **인프라 부팅까지다.** 2/2 Status Check는 시스템·인스턴스 도달성
#     확인이며 앱 레벨 Health Check는 별개 축이다. 여기의 OK는 "AWS가 인스턴스를
#     정상 기동시켰다"까지만 말하고 서비스가 살아 있다는 뜻이 아니다.
#   - **기다리지 않는다.** 조회 1회로 지금 관측되는 상태를 돌려주고, "얼마나 더
#     기다릴 것인가"는 판정 주체(workflows.judge_rightsizing_boot)가 기동 요청 시각과
#     예산(status_check_budget)으로 가른다. 전에는 boto3 waiter가 최대 Delay×MaxAttempts
#     (기본 3분) 동안 호출 스레드를 붙잡았고, 실행 디스패처의 main 레인은 겹쳐 돌지
#     않으므로(max_instances=1) 그동안 승인된 다른 조치(NACL 차단 포함)가 전부
#     밀렸다(Issue #434 1-3). 조회 1회는 그 자리를 점유하지 않는다.
#   - 예산은 설정값이다(STATUS_CHECK_WAIT_DELAY_SECONDS × STATUS_CHECK_WAIT_MAX_ATTEMPTS).
#     이름은 waiter 시절의 것이지만 의미는 "기동 요청 뒤 2/2를 기다리는 벽시계 상한"이다
#     — 시연에서 조여야 할 값이라 코드 상수로 굳히지 않고, 예산을 늘려도 디스패처가
#     붙잡히지 않으므로 이제 두 축(대기 길이·스캔 점유)이 따로 움직인다.
#
# 관측 4분기 — 정상 경로도 재조회도 **System·Instance 두 검사를 다 본다**(전에는 waiter
# 통과가 InstanceStatus 하나만 보고 "2/2 통과"로 요약했다, #434 1-3).
#   OK       running이고 두 검사가 모두 ok — 부팅 성공
#   FAILED   정지·종료 계열 state, 또는 어느 검사든 impaired — AWS가 이미 실패로 판정했다
#   PENDING  관측은 했으나 아직 결론 전(initializing·insufficient-data·pending·한쪽만 ok)
#            → 판정 주체가 예산 안이면 다음 주기에 다시 묻고, 넘었으면 TIMED_OUT으로 확정한다
#   PENDING + probe_failed  AWS에 물어보지 못했다 — 관측이 아니므로 자동 원복의 입력이
#            아니고, 판정 불가 보류(Issue #249)로 간다
#   TIMED_OUT은 관측이 아니라 **판정**이다(timed_out) — PENDING 관측에 예산 초과를 더해
#            판정 주체가 만든다. 자산 상태를 실제로 봤으므로 자동 원복의 입력이 된다.
#
# 자동 원복 자체는 이 모듈에 있지 않습니다 (Issue #241). 여기는 "되돌려야 하는가"를
# 가르는 판정까지이고, 되돌리는 동작은 executor.execute_revert_size(), 발동·상태
# 확정은 workflows·dispatcher가 나눠 갖습니다 — AWS 호출은 services/aws/**, 커밋
# 순서는 workflows.py라는 같은 경계입니다.
#
# [남은 작업]
# 1. 롤백 나머지 2종(UNISOLATE·SG_RECREATE)의 트리거 판단 — 둘 다 관제자 발동이라
#    여기 감시가 필요한지부터 정해야 합니다
# 2. AttachedEbs 검사(Nitro 전용 세 번째 검사)는 보지 않습니다 — 확대 여부는 별도 설계
#    판단입니다(Issue #434 1-3)
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum, unique
from typing import Any, Mapping

from botocore.exceptions import BotoCoreError, ClientError

from schemas.precheck import PrecheckReasonCode

from config import get_settings

from .client import aws_client
from .errors import reason_code_for
from .executor import parse_arn

logger = logging.getLogger("vigilantis.aws")

# 2/2 = 시스템 상태 검사 + 인스턴스 상태 검사(DescribeInstanceStatus의 SystemStatus·
# InstanceStatus). 둘 다 ok여야 통과다(_check_values 순서).
_OP_DESCRIBE_STATUS = "ec2.describe_instance_status"

# 확정적으로 "부팅 성공이 아니다"라고 읽는 인스턴스 상태. pending은 아직 진행
# 중이므로 여기 없다 — 그 구분이 실패와 PENDING을 가른다.
_NOT_BOOTING_STATES: frozenset[str] = frozenset(
    {"stopping", "stopped", "shutting-down", "terminated"}
)

# 검사 결과가 impaired면 AWS가 이미 이상으로 판정한 것이라 더 기다릴 이유가 없다.
# initializing·insufficient-data는 아직 판정 전이라 PENDING 쪽이다.
_IMPAIRED = "impaired"

# 2/2 통과로 읽는 검사 결과 — SystemStatus·InstanceStatus 순서(_check_values)
_TWO_OF_TWO_OK = ["ok", "ok"]

_OK_SUMMARY = "2/2 Status Check 통과(인프라 부팅까지 — 앱 Health Check는 별개 축)"


@unique
class StatusCheckVerdict(str, Enum):
    """2/2 Status Check 판정."""

    OK = "OK"                # 2/2 통과 — 인프라 부팅까지 정상
    FAILED = "FAILED"        # 확정적으로 부팅 실패 (정지·종료·impaired)
    PENDING = "PENDING"      # 아직 결론 전 — 관측했거나(probe_failed=False) 못 물었거나(True)
    TIMED_OUT = "TIMED_OUT"  # 예산 안에 결론이 나지 않음 — PENDING 관측에 예산 초과를 더한 판정


@dataclass(frozen=True)
class StatusCheckOutcome:
    """관측·판정 1건. 상태 기록·원복 발동은 호출부 몫이다(dispatcher.py).

    probe_failed는 **AWS에 물어보지 못해 결론을 내지 못했다**는 뜻이다. 관측된 PENDING
    (아직 2/2 전)·TIMED_OUT(예산을 다 썼다)과 "성공으로 확정할 근거가 없다"는 점은 같지만
    **자동 원복의 입력이 되는지가 다르다.** 검증기의 실패를 자산의 실패로 저장하면 일시적인
    권한·네트워크·스로틀링 오류가 멀쩡한 인스턴스를 되돌린다 — 그래서 호출부는 이 값이
    참이면 확정하지 않고 판정 불가로 기록한다(workflows.judge_rightsizing_boot →
    record_verification_failure).

    reason_code는 보류·실패 양쪽에 실린다(인스턴스 없음도 코드를 단다). 판정 불가를
    가르는 것은 코드의 유무가 아니라 이 값이다. 다만 **판정 불가에는 코드가 반드시
    있다** — 다시 물을지(errors.RETRYABLE_REASON_CODES)와 관제자가 볼 사유가 그 코드로
    갈리기 때문이다 (Issue #249).
    """

    verdict: StatusCheckVerdict
    summary: str
    reason_code: PrecheckReasonCode | None = None
    instance_state: str | None = None
    probe_failed: bool = False
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.probe_failed and self.verdict is not StatusCheckVerdict.PENDING:
            # 자산 상태를 본 적이 없는데 실패·성공·타임아웃으로 확정된 결과는 만들 수 없다
            raise ValueError("조회 실패 판정은 PENDING과 함께여야 합니다")
        if self.probe_failed and self.reason_code is None:
            # 재시도 여부를 가르는 축이 없으면 보류가 영원한 보류가 된다 (Issue #249)
            raise ValueError("조회 실패 판정에는 사유 코드가 필요합니다")

    @property
    def booted(self) -> bool:
        return self.verdict is StatusCheckVerdict.OK

    @property
    def undecided(self) -> bool:
        """관측은 했는데 아직 결론이 없다 — 예산 안이면 다시 묻고, 넘었으면 timed_out()."""
        return self.verdict is StatusCheckVerdict.PENDING and not self.probe_failed


def status_check_budget() -> timedelta:
    """기동 요청 뒤 2/2를 기다리는 벽시계 상한 — 설정값 둘의 곱이다(파일 헤더)."""
    settings = get_settings()
    return timedelta(
        seconds=settings.STATUS_CHECK_WAIT_DELAY_SECONDS
        * settings.STATUS_CHECK_WAIT_MAX_ATTEMPTS
    )


def _first_status(response: Any) -> Mapping[str, Any] | None:
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


def _classify(ec2: Any, instance_id: str) -> StatusCheckOutcome:
    """조회 1회로 OK·FAILED·PENDING·판정 불가를 가른다.

    DescribeInstanceStatus는 기본적으로 running 인스턴스만 돌려주므로 기동에 실패해
    stopped로 떨어진 인스턴스는 **빈 응답**으로만 나타난다. 그래서 IncludeAllInstances로
    물어 상태를 직접 읽는다.
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
            verdict=StatusCheckVerdict.PENDING,
            summary=f"상태 조회 실패로 판정 보류: {type(exc).__name__}",
            reason_code=code,
            probe_failed=True,
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
    if state == "running" and checks == _TWO_OF_TWO_OK:
        return StatusCheckOutcome(
            verdict=StatusCheckVerdict.OK,
            summary=_OK_SUMMARY,
            instance_state=state,
        )
    return StatusCheckOutcome(
        verdict=StatusCheckVerdict.PENDING,
        summary=f"아직 2/2 전 — {_describe_summary(state, checks)}",
        instance_state=state or None,
    )


def probe_status_check(target_arn: str) -> StatusCheckOutcome:
    """기동한 인스턴스의 2/2 Status Check를 **지금** 관측한다 — 기다리지 않는다. (Issue #240 · #434 1-3)

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
    outcome = _classify(aws_client("ec2", target.region), instance_id)
    if not outcome.booted:
        logger.warning(
            "status_check_not_ok",
            extra={
                "instance_id": instance_id,
                "verdict": outcome.verdict.value,
                "probe_failed": outcome.probe_failed,
                "reason_code": outcome.reason_code.value if outcome.reason_code else None,
                "aws_operation": _OP_DESCRIBE_STATUS,
            },
        )
    return outcome


def timed_out(outcome: StatusCheckOutcome, waited: timedelta) -> StatusCheckOutcome:
    """관측된 PENDING에 예산 초과를 더해 TIMED_OUT으로 확정한다 — 자동 원복의 입력이다.

    판정 주체만 부른다. 조회 실패(probe_failed)에는 쓸 수 없다 — 자산 상태를 본 적이
    없는 것을 예산이 지났다는 이유로 실패로 확정하면, 조회가 막힌 동안 부팅을 마친
    인스턴스를 되돌린다.
    """
    if not outcome.undecided:
        raise ValueError("TIMED_OUT은 관측된 PENDING에서만 만듭니다")
    return replace(
        outcome,
        verdict=StatusCheckVerdict.TIMED_OUT,
        summary=(
            f"제한 시간({int(waited.total_seconds())}초) 안에 2/2에 도달하지 못했습니다 — "
            f"{outcome.summary}"
        ),
    )
