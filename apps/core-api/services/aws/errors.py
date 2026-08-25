# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# AWS 응답 → 가드레일 ④ 거절 사유 코드 매핑입니다. (Issue #128, ADR-0007 §2)
# precheck()와 실제 실행·rollback이 같은 표를 쓰므로 executor 안에 두지 않는다.
#
# 판정 규약(ADR-0007 §2) — **DryRunOperation 예외가 난 경우에만 통과다.**
# 예외 없이 정상 반환하면 DryRun 플래그가 적용되지 않은 것이므로 거절한다.
# 이 규약이 없으면 "확인 단계가 조용히 실제 실행을 수행하고 통과로 기록되는"
# 상황을 탐지할 수 없다 — LocalStack의 create_network_acl_entry가 실제로 그랬고
# (ADR-0006 §4 5행), 같은 그물이 SDK 변경·신규 런북에도 걸린다.
# ==============================================================================

from __future__ import annotations

import logging
from typing import Any, Callable, Final, Optional

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from schemas.precheck import PrecheckReasonCode

logger = logging.getLogger("vigilantis.aws")

# DryRun 성공은 예외로 돌아온다 — AWS가 정한 규약이다
DRY_RUN_SUCCESS_ERROR_CODE: Final[str] = "DryRunOperation"

_UNAUTHORIZED_CODES: Final[frozenset[str]] = frozenset({"UnauthorizedOperation"})
_INVALID_STATE_CODES: Final[frozenset[str]] = frozenset(
    {"IncorrectInstanceState", "DependencyViolation"}
)
_TARGET_NOT_FOUND_CODES: Final[frozenset[str]] = frozenset({"InvalidTarget"})


def aws_error_code(exc: ClientError) -> str:
    """ClientError의 AWS 오류 코드. 응답에 없으면 빈 문자열."""
    return str(exc.response.get("Error", {}).get("Code", ""))


def reason_code_for(exc: BaseException) -> PrecheckReasonCode:
    """AWS·botocore 예외를 거절 사유 코드로 분류한다(ADR-0007 §2 표).

    ParamValidationError는 botocore 클라이언트 단에서 나므로 네트워크 호출 이전이다 —
    "우리가 만든 파라미터가 그 작업의 계약과 맞지 않는다"는 뜻이라 AWS 오류와 섞지
    않고 PRECHECK_PARAM_INVALID로 분류한다(§2가 그 코드를 둔 이유와 같다).
    """
    if isinstance(exc, ParamValidationError):
        return PrecheckReasonCode.PRECHECK_PARAM_INVALID
    if isinstance(exc, ClientError):
        return _reason_code_for_error_code(aws_error_code(exc))
    if isinstance(exc, BotoCoreError):
        # 엔드포인트 접속 실패·자격증명 부재 등 — AWS에 닿지 못한 경우
        return PrecheckReasonCode.PRECHECK_AWS_ERROR
    raise TypeError(f"AWS 예외가 아닙니다: {type(exc).__name__}")


def _reason_code_for_error_code(code: str) -> PrecheckReasonCode:
    if code in _UNAUTHORIZED_CODES or code.startswith("AccessDenied"):
        return PrecheckReasonCode.PRECHECK_UNAUTHORIZED
    if code in _TARGET_NOT_FOUND_CODES or code.endswith("NotFound"):
        # ec2는 InvalidInstanceID.NotFound 형태, elbv2는 TargetGroupNotFound 형태다
        return PrecheckReasonCode.PRECHECK_TARGET_NOT_FOUND
    if code in _INVALID_STATE_CODES or "InUse" in code:
        return PrecheckReasonCode.PRECHECK_INVALID_STATE
    return PrecheckReasonCode.PRECHECK_AWS_ERROR


def run_dry_run(operation: Callable[..., Any], **params: Any) -> Optional[PrecheckReasonCode]:
    """DryRun=True 호출 1건을 판정한다. 통과면 None, 아니면 거절 사유 코드.

    DryRun=True는 호출부가 아니라 여기서 붙인다 — 빠뜨린 호출이 실제 실행이 되는
    사고를 구조로 막는다. 예외를 밖으로 던지지 않는다(ADR-0007 §1).
    """
    if "DryRun" in params:
        raise ValueError("DryRun은 run_dry_run이 붙입니다 — 호출부에서 넘기지 마십시오")

    operation_name = getattr(operation, "__name__", "unknown")
    try:
        operation(DryRun=True, **params)
    except ClientError as exc:
        code = aws_error_code(exc)
        if code == DRY_RUN_SUCCESS_ERROR_CODE:
            return None
        return _reason_code_for_error_code(code)
    except (ParamValidationError, BotoCoreError) as exc:
        return reason_code_for(exc)

    # 예외 없이 반환됐다 = 플래그가 적용되지 않았다. 자원이 실제로 바뀌었을 수 있다.
    logger.critical(
        "precheck_dry_run_not_applied",
        extra={"aws_operation": operation_name},
    )
    return PrecheckReasonCode.PRECHECK_AWS_ERROR
