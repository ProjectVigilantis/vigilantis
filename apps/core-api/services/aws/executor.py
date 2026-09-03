# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# Boto3 기반 AWS 제어 모듈입니다. 확정 10종 Runbook(ADR-0002 본편 7 + ADR-0004
# 롤백 3)만 다루며, 여기 없는 ID는 실행 경로에 진입할 수 없습니다.
#
# 현재 범위: 가드레일 ④ AWS Dry-Run 판정 = precheck(). (Issue #129, ADR-0007)
#   - 예외를 던지지 않는다. AWS 오류·미구현·파라미터 문제를 모두 PrecheckOutcome로
#     반환한다(§1). 유일한 예외는 backup_loader 미배선인데, 그것은 페이로드에 대한
#     판정이 아니라 호출부 배선 오류다 — FAIL로 남기면 멀쩡한 명령에 거절 기록이
#     붙는다. (ai/guardrails.py가 미배선 검증 문맥을 NotImplementedError로 막는 것과
#     같은 구분이다.)
#   - DryRun은 errors.run_dry_run()이 붙이고, DryRunOperation 예외가 난 경우에만
#     통과로 인정한다(§2).
#   - DryRun을 쓸 수 없는 작업은 환경과 무관하게 조회로 대체한다(§4). LocalStack일
#     때만 조회하도록 나누는 것은 ADR-0006 §3이 금지한다.
#
# 실행 범위: RUNBOOK_EC2_RIGHTSIZING = execute_rightsizing(). (Issue #211, §실행)
#            RUNBOOK_EC2_REVERT_SIZE  = execute_revert_size(). (Issue #241, §원복)
#   - precheck과 같은 규약으로 예외를 던지지 않는다. 단계별 결과는 ExecutionStepResult로
#     돌려주고, 저장·커밋 순서는 workflows.py가 소유한다.
#   - 원복은 되돌릴 값을 인자로만 받는다 — 백업 레코드 조회는 호출부(workflows) 몫이다.
#     원천이 하나라는 정책(ADR-0004 정책 ③)은 값을 뽑는 자리가 하나일 때만 성립한다.
#
# [남은 작업]
# 1. 나머지 8종 실행 함수 — 백업이 필요한 런북은 백업 commit 이후에만 진입한다
# 2. 롤백 나머지 2종(RUNBOOK_EC2_UNISOLATE·RUNBOOK_SG_RECREATE) 실행도 executor 경유 —
#    트리거 판단·감시는 rollback.py 담당
#
# 파라미터 계약의 원천은 packages/schemas/runbook_parameters.py의 typed 모델이다(#154).
# 형식 위반은 ① Schema Check에서 먼저 걸리고, 여기 _validate_params는 같은 모델로 한 번
# 더 본다 — ④를 타는 경로가 그것만이 아니기 때문이다. **롤백 3종도 ①을 거친다**
# (ADR-0004 정책 ①, Issue #241): ①이 문맥별 파라미터 계약을 골라 대조하므로
# (ai/guardrails.py `_PARAMETER_MODELS_BY_CONTEXT`) 후보가 없는 원복 명령도 통과한다.
# 후보(RunbookCandidateDraft)를 여기 parameters로 바꾸는 변환은
# runbook_parameters.py의 build_precheck_parameters()이며, 원복 명령의 parameters는
# 이미 실행 파라미터 계약의 값이라 변환이 없다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Protocol

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError
from pydantic import BaseModel, TypeAdapter, ValidationError

from schemas.backups import BackupType
from schemas.executions import (
    ExecutionEffect,
    ExecutionStepResult,
    ExecutionStepStatus,
)
from schemas.precheck import (
    PrecheckOutcome,
    PrecheckReasonCode,
    VerificationMethod,
    build_verification_summary,
)
from schemas.runbook_parameters import (
    EbsDeleteUnattachedParameters,
    Ec2EnableAutoscalingParameters,
    Ec2IsolateParameters,
    Ec2RevertSizeParameters,
    Ec2RightsizingParameters,
    Ec2UnisolateParameters,
    NaclAddDenyParameters,
    NaclRestoreParameters,
    SecurityGroupId,
    SgDeleteIsolatedParameters,
    SgRecreateParameters,
    TargetGroupArn,
)
from schemas.runbooks import RunbookId

from .client import aws_client
from .errors import aws_error_code, reason_code_for, run_dry_run

logger = logging.getLogger("vigilantis.aws")

R = PrecheckReasonCode
M = VerificationMethod

# DryRun이 확인해 주는 것과 확인해 주지 않는 것.
# 실측: 존재하지 않는 SG로 delete_security_group을 DryRun해도 DryRunOperation이
# 돌아온다. 즉 DryRun 통과는 호출 권한과 파라미터 형식의 증명이지 대상 존재의
# 증명이 아니다 — "확인:" 절에 대상 유효를 쓰면 거짓이 된다.
_DRY_RUN_VERIFIES = "호출 권한과 파라미터 형식(DryRun)"
_DRY_RUN_MISSES = "대상 자원 존재와 현재 상태(DryRun 비검증)"
# 조회 대체 경로는 어느 환경에서도 IAM 권한을 확인하지 못한다(ADR-0007 §3).
_DESCRIBE_MISSES = "IAM 권한(조회 대체 경로)"


# ------------------------------------------------------------------ 백업 레코드
# 원복 값의 유일한 원천은 DB 백업 레코드다(ADR-0004 롤백 공통 정책 ③). executor는
# 읽기만 하고 트랜잭션을 소유하지 않으므로 조회를 주입받는다 — precheck()가 DB 없이
# 단위 테스트되는 것도 같은 이유다. 기록하는 쪽은 services/aws/backup.py(캡처)와
# workflows.store_instance_spec_backup(저장·결속·커밋)이다.


@dataclass(frozen=True)
class BackupRecordView:
    """db.models.BackupRecord의 읽기 전용 투영."""

    backup_record_id: str
    target_arn: str
    backup_type: str
    payload: Mapping[str, Any]


class BackupRecordLoader(Protocol):
    def get(self, backup_record_id: str) -> Optional[BackupRecordView]:
        """ID로 백업 레코드 1건. 없으면 None."""

    def latest_for_target(
        self,
        target_arn: str,
        backup_type: str,
        payload_match: Optional[Mapping[str, Any]] = None,
    ) -> Optional[BackupRecordView]:
        """대상 자원의 최신 백업 1건. 없으면 None.

        NACL_RESTORE처럼 backup_record_id를 파라미터로 받지 않는 런북이 쓴다
        (ADR-0007 §5 파라미터 표 기준 — 코드 소재는 schemas.runbook_parameters).

        payload_match가 있으면 payload의 해당 키가 전부 같은 레코드만 후보다.
        한 자원에 조치가 누적되면 대상의 최신 하나만으로는 복원 대상을 고를 수
        없다 — NACL 하나에 deny 규칙이 둘 이상 쌓이면 오래된 규칙은 복원할 수
        없게 된다(최신 백업이 항상 다른 규칙을 가리키므로).
        """


# 백업 종류 — 어휘의 원천은 schemas.backups.BackupType이다(ADR-0004
# safety_and_rollback.backup_action). 읽는 쪽(여기)과 만드는 쪽(services/aws/backup.py)이
# 문자열을 각자 적으면 오타 하나로 백업이 조회되지 않는다.
BACKUP_INSTANCE_SPEC = BackupType.SAVE_INSTANCE_SPEC_JSON.value
BACKUP_SG_FULL_RULES = BackupType.SAVE_SG_FULL_RULES_JSON.value
BACKUP_SG_AND_TG_MAPPING = BackupType.SAVE_CURRENT_SG_AND_TG_MAPPING.value
BACKUP_NACL_RULE_INDEX = BackupType.RECORD_NACL_RULE_INDEX.value


# ------------------------------------------------------------------ 백업 payload 확인
# 파라미터가 아니라 우리가 저장한 백업 JSON을 보는 자리다(구조의 원천은
# services/aws/backup.py). 식별자 형식은 파라미터 계약과 같아야 하므로 같은 타입을
# 재사용한다 — 패턴을 두 번 적으면 한쪽만 고쳐진 채로 남는다.
_SG_ID = TypeAdapter(SecurityGroupId)
_TG_ARN = TypeAdapter(TargetGroupArn)


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _conforms(adapter: TypeAdapter, value: Any) -> bool:
    try:
        adapter.validate_python(value)
    except ValidationError:
        return False
    return True


# ------------------------------------------------------------------ ARN
@dataclass(frozen=True)
class ParsedArn:
    partition: str
    service: str
    region: str
    account_id: str
    resource_type: str
    resource_id: str


def parse_arn(arn: str) -> Optional[ParsedArn]:
    """arn:aws:ec2:<region>:<account>:<type>/<id> 형태만 받는다.

    수집기가 만드는 포맷과 같다(services/collector.py의 _arn) — 가드레일 ③이
    대조하는 문자열도 이것이다.

    공개 함수다. 실행 흐름이 target_arn에서 리전·자원 ID를 꺼낼 때 같은 해석을
    써야 하기 때문이다(workflows의 스펙 JSON 백업) — 파서가 둘이면 precheck가
    본 자원과 조치·백업이 향하는 자원이 갈릴 수 있다.
    """
    if not isinstance(arn, str):
        return None
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        return None
    resource_type, sep, resource_id = parts[5].partition("/")
    if not sep or not resource_type or not resource_id:
        return None
    return ParsedArn(parts[1], parts[2], parts[3], parts[4], resource_type, resource_id)


# ------------------------------------------------------------------ 런북 명세
@dataclass(frozen=True)
class _Spec:
    """런북 1종의 precheck 명세. 파라미터 계약과 확인 방식을 한 자리에 모은다."""

    params_model: type[BaseModel]           # 파라미터 계약(packages/schemas, #154)
    resource_type: str                      # target_arn이 가리켜야 하는 자원 유형
    primary_param: Optional[str]            # target_arn의 자원 ID와 같아야 하는 키
    method: VerificationMethod
    operations: tuple[str, ...]
    handler: str                            # 아래 핸들러 함수 이름
    arn_params: tuple[str, ...] = ()        # 계정·파티션·리전을 대조할 ARN 파라미터
    backup_type: Optional[str] = None       # 백업 레코드가 필요한 런북만
    # 대상으로 백업을 찾을 때 payload와 값이 같아야 하는 파라미터 키
    backup_match_params: tuple[str, ...] = ()


RUNBOOK_SPECS: Mapping[str, _Spec] = {
    RunbookId.RUNBOOK_EC2_ISOLATE.value: _Spec(
        params_model=Ec2IsolateParameters,
        resource_type="instance",
        primary_param="instance_id",
        arn_params=("target_group_arn",),
        method=M.MIXED,
        operations=(
            "ec2.modify_network_interface_attribute",
            "ec2.describe_security_groups",
            "elbv2.describe_target_health",
        ),
        handler="_precheck_isolate",
    ),
    RunbookId.RUNBOOK_NACL_ADD_DENY.value: _Spec(
        params_model=NaclAddDenyParameters,
        resource_type="network-acl",
        primary_param="network_acl_id",
        method=M.DESCRIBE,
        operations=("ec2.describe_network_acls",),
        handler="_precheck_nacl_add_deny",
    ),
    RunbookId.RUNBOOK_NACL_RESTORE.value: _Spec(
        params_model=NaclRestoreParameters,
        resource_type="network-acl",
        primary_param="network_acl_id",
        method=M.DESCRIBE,
        operations=("ec2.describe_network_acls",),
        handler="_precheck_nacl_restore",
        backup_type=BACKUP_NACL_RULE_INDEX,
        # backup_record_id를 받지 않는 유일한 롤백 런북이라 rule index로 특정한다
        backup_match_params=("rule_number", "egress"),
    ),
    RunbookId.RUNBOOK_SG_DELETE_ISOLATED.value: _Spec(
        params_model=SgDeleteIsolatedParameters,
        resource_type="security-group",
        primary_param="group_id",
        method=M.DRY_RUN,
        operations=("ec2.delete_security_group",),
        handler="_precheck_sg_delete",
    ),
    RunbookId.RUNBOOK_EC2_RIGHTSIZING.value: _Spec(
        params_model=Ec2RightsizingParameters,
        resource_type="instance",
        primary_param="instance_id",
        method=M.DRY_RUN,
        operations=("ec2.modify_instance_attribute",),
        handler="_precheck_rightsizing",
    ),
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING.value: _Spec(
        params_model=Ec2EnableAutoscalingParameters,
        resource_type="instance",
        primary_param="instance_id",
        method=M.MIXED,
        operations=(
            "ec2.create_launch_template",
            "ec2.describe_instances",
            "autoscaling.describe_auto_scaling_groups",
        ),
        handler="_precheck_enable_autoscaling",
    ),
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED.value: _Spec(
        params_model=EbsDeleteUnattachedParameters,
        resource_type="volume",
        primary_param="volume_id",
        method=M.DRY_RUN,
        operations=("ec2.create_snapshot", "ec2.delete_volume"),
        handler="_precheck_ebs_delete",
    ),
    RunbookId.RUNBOOK_EC2_UNISOLATE.value: _Spec(
        params_model=Ec2UnisolateParameters,
        resource_type="instance",
        primary_param="instance_id",
        method=M.MIXED,
        operations=(
            "ec2.modify_network_interface_attribute",
            "ec2.describe_security_groups",
            # ADR-0007 §4는 describe_target_health를 적었지만 그 응답에는 VpcId가 없다 —
            # "대상이 같은 VPC"를 확인할 수 있는 조회는 describe_target_groups다.
            "elbv2.describe_target_groups",
        ),
        handler="_precheck_unisolate",
        backup_type=BACKUP_SG_AND_TG_MAPPING,
    ),
    RunbookId.RUNBOOK_SG_RECREATE.value: _Spec(
        params_model=SgRecreateParameters,
        resource_type="security-group",
        primary_param=None,     # 복원 대상은 백업 레코드가 가리킨다
        method=M.DRY_RUN,
        operations=(
            "ec2.create_security_group",
            "ec2.authorize_security_group_ingress",
            "ec2.authorize_security_group_egress",
        ),
        handler="_precheck_sg_recreate",
        backup_type=BACKUP_SG_FULL_RULES,
    ),
    RunbookId.RUNBOOK_EC2_REVERT_SIZE.value: _Spec(
        params_model=Ec2RevertSizeParameters,
        resource_type="instance",
        primary_param="instance_id",
        method=M.DRY_RUN,
        operations=("ec2.modify_instance_attribute",),
        handler="_precheck_revert_size",
        backup_type=BACKUP_INSTANCE_SPEC,
    ),
}

# ADR-0007 §1 — 가드레일이 호출 전에 거를 수 있도록 함께 노출한다
IMPLEMENTED_RUNBOOK_IDS: frozenset[str] = frozenset(RUNBOOK_SPECS)


# ------------------------------------------------------------------ 판정 조립
@dataclass(frozen=True)
class _Ctx:
    """한 번의 precheck 호출이 다루는 값. 핸들러는 이것만 본다."""

    runbook_id: str
    spec: _Spec
    target_arn: str
    target: ParsedArn
    params: Mapping[str, Any]
    backup: Optional[BackupRecordView]


def _summary(spec: _Spec, verified, unverified) -> str:
    return build_verification_summary(
        spec.method, operations=spec.operations, verified=verified, unverified=unverified
    )


def _ok(ctx: _Ctx, *, verified, unverified) -> PrecheckOutcome:
    return PrecheckOutcome(
        passed=True, verification_summary=_summary(ctx.spec, verified, unverified)
    )


def _fail(ctx: _Ctx, code: PrecheckReasonCode, *, verified, unverified) -> PrecheckOutcome:
    logger.warning(
        "precheck_rejected",
        extra={"runbook_id": ctx.runbook_id, "reason_code": code.value},
    )
    return PrecheckOutcome(
        passed=False,
        reason_code=code,
        verification_summary=_summary(ctx.spec, verified, unverified),
    )


def _reject(spec: _Spec, code: PrecheckReasonCode, detail: str) -> PrecheckOutcome:
    """AWS에 닿기 전에 끝난 거절. 확인한 것이 없다는 사실을 요약에 남긴다."""
    return PrecheckOutcome(
        passed=False,
        reason_code=code,
        verification_summary=_summary(
            spec, [f"없음({detail})"], ["AWS 대상 상태", "IAM 권한"]
        ),
    )


def _call(operation: Callable[..., Any], **kwargs: Any):
    """조회 호출 1건. 예외를 사유 코드로 바꿔 (응답, 코드) 짝으로 돌려준다."""
    try:
        return operation(**kwargs), None
    except (ClientError, BotoCoreError) as exc:
        return None, reason_code_for(exc)


# ------------------------------------------------------------------ 파라미터 계약
def _validate_params(spec: _Spec, params: Mapping[str, Any]) -> Optional[str]:
    """계약 위반 사유 문자열. 문제가 없으면 None.

    판정은 packages/schemas의 typed 모델이 한다(#154). 키 집합이 정확히 일치해야
    한다는 규칙(extra="forbid" + 전 키 required)이 ADR-0007 §5 ①("롤백 3종은 원복
    값을 파라미터로 받지 않는다")을 그대로 강제한다 — 원본 SG 규칙·인스턴스 타입이
    실려 오면 알 수 없는 키가 되어 여기서 거절된다.

    AI 후보는 ① Schema Check가 같은 계약을 먼저 본다. 여기서 한 번 더 보는 이유는
    ④를 타는 경로가 그것만이 아니기 때문이다 — 롤백 3종과 시스템 트리거는 ①을
    거치지 않는다(ADR-0004 정책 ②).
    """
    try:
        spec.params_model.model_validate(params)
    except ValidationError as exc:
        return _describe_violation(exc)
    return None


def _describe_violation(exc: ValidationError) -> str:
    """검증 오류를 요약 한 줄로. 이전 술어 표가 내던 문구와 우선순위를 유지한다."""
    missing: list[str] = []
    unknown = 0
    invalid: list[str] = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"])
        if err["type"] == "missing":
            missing.append(location)
        elif err["type"] == "extra_forbidden":
            unknown += 1
        elif location:
            invalid.append(location)
        else:
            # 필드에 매이지 않는 규칙(min_size ≤ max_size 등). 문구는 계약이 지은
            # 것이라 그대로 실어도 payload 문자열이 새지 않는다.
            invalid.append(err["msg"].removeprefix("Value error, "))

    if missing:
        return f"필수 키 누락: {', '.join(sorted(missing))}"
    # 알 수 없는 키의 이름은 payload가 지은 문자열이라 요약에 싣지 않는다 — 개수만 남긴다
    if unknown:
        return f"허용되지 않은 키 {unknown}개"
    return f"값 형식 위반: {', '.join(sorted(set(invalid)))}"


def _validate_scope(spec: _Spec, target: ParsedArn, params: Mapping[str, Any]) -> Optional[str]:
    """파라미터가 target_arn과 같은 자원·같은 계정을 가리키는지 확인한다.

    가드레일 ③ ARN Match는 target_arn 하나만 본다. 파라미터에 리소스 ID가 여럿
    들어오는 런북에서는 그 사이가 비므로 여기서 한 번 더 막는다(§5 ②,
    Scope Escalation 2차 방어).
    """
    if target.resource_type != spec.resource_type:
        return f"target_arn 자원 유형 불일치({spec.resource_type} 필요)"

    primary = spec.primary_param
    if primary and params[primary] != target.resource_id:
        return f"{primary}가 target_arn과 다른 자원을 가리킴"

    for key in spec.arn_params:
        parsed = parse_arn(params[key])
        if parsed is None:
            return f"{key} ARN 형식 위반"
        if (parsed.partition, parsed.account_id) != (target.partition, target.account_id):
            return f"{key}가 target_arn과 다른 계정을 가리킴"
        if parsed.region != target.region:
            # 클라이언트를 target_arn의 리전으로 만들므로, 다른 리전 ARN이 실리면
            # 그 자원은 조회 자체가 되지 않는다 — 오판정 대신 여기서 거절한다
            return f"{key}가 target_arn과 다른 리전을 가리킴"
    return None


# ------------------------------------------------------------------ 런북별 판정
# 실행 시 만들 자원의 이름 규칙 — 실행 카드에서 확정할 잠정안이다. precheck는
# "같은 이름이 이미 있는가"만 보므로 규칙이 바뀌어도 판정 구조는 그대로다.
def _launch_template_name(instance_id: str) -> str:
    return f"vigilantis-lt-{instance_id}"


def _asg_name(instance_id: str) -> str:
    return f"vigilantis-asg-{instance_id}"


def _dry_run_chain(ctx: _Ctx, calls, *, verified=(), unverified=()) -> PrecheckOutcome:
    """DryRun 호출을 차례로 걸고 하나라도 거절되면 그 사유로 끝낸다."""
    for operation, kwargs in calls:
        code = run_dry_run(operation, **kwargs)
        if code is not None:
            return _fail(
                ctx, code, verified=["없음(DryRun 거절)"], unverified=[_DRY_RUN_MISSES]
            )
    return _ok(
        ctx,
        verified=[_DRY_RUN_VERIFIES, *verified],
        unverified=[_DRY_RUN_MISSES, *unverified],
    )


def _instance(instance_id: str, region: str):
    """인스턴스 1건. (인스턴스, 코드) 짝 — 없으면 코드가 채워진다."""
    res, code = _call(
        aws_client("ec2", region).describe_instances, InstanceIds=[instance_id]
    )
    if code is not None:
        return None, code
    for reservation in res.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            return instance, None
    return None, R.PRECHECK_TARGET_NOT_FOUND


def _primary_eni(instance: Mapping[str, Any]) -> Optional[str]:
    for interface in instance.get("NetworkInterfaces", []):
        eni = interface.get("NetworkInterfaceId")
        if eni:
            return eni
    return None


# --- DryRun 전면 (ADR-0007 §Context 표) ---


def _precheck_rightsizing(ctx: _Ctx) -> PrecheckOutcome:
    ec2 = aws_client("ec2", ctx.target.region)
    return _dry_run_chain(
        ctx,
        [
            (
                ec2.modify_instance_attribute,
                {
                    "InstanceId": ctx.params["instance_id"],
                    "InstanceType": {"Value": ctx.params["target_instance_type"]},
                },
            )
        ],
        # current_instance_type이 실제 스펙과 같은지는 보지 않는다 — 제안 생성과 실행
        # 사이의 상태 변화는 ADR-0007 범위 밖(승인·실행 시점 재검증)이다.
        unverified=["현재 스펙이 current_instance_type과 같은지"],
    )


def _precheck_revert_size(ctx: _Ctx) -> PrecheckOutcome:
    restore_type = ctx.backup.payload.get("instance_type")
    if not _non_empty_str(restore_type):
        return _fail(
            ctx,
            R.PRECHECK_PARAM_INVALID,
            verified=["없음(백업 레코드에 instance_type 없음)"],
            unverified=[_DRY_RUN_MISSES],
        )
    ec2 = aws_client("ec2", ctx.target.region)
    return _dry_run_chain(
        ctx,
        [
            (
                ec2.modify_instance_attribute,
                {
                    "InstanceId": ctx.params["instance_id"],
                    "InstanceType": {"Value": restore_type},
                },
            )
        ],
        verified=["원복 스펙을 백업 레코드에서 로드"],
    )


def _precheck_sg_delete(ctx: _Ctx) -> PrecheckOutcome:
    ec2 = aws_client("ec2", ctx.target.region)
    return _dry_run_chain(
        ctx, [(ec2.delete_security_group, {"GroupId": ctx.params["group_id"]})]
    )


def _precheck_ebs_delete(ctx: _Ctx) -> PrecheckOutcome:
    ec2 = aws_client("ec2", ctx.target.region)
    volume_id = ctx.params["volume_id"]
    return _dry_run_chain(
        ctx,
        [
            (ec2.create_snapshot, {"VolumeId": volume_id}),
            (ec2.delete_volume, {"VolumeId": volume_id}),
        ],
        # "available 상태 2차 검증"은 실행 직전 단계의 몫이다
        unverified=["볼륨 available 상태"],
    )


def _precheck_sg_recreate(ctx: _Ctx) -> PrecheckOutcome:
    payload = ctx.backup.payload
    group_name, description, vpc_id = (
        payload.get("group_name"),
        payload.get("description"),
        payload.get("vpc_id"),
    )
    if not all(_non_empty_str(v) for v in (group_name, description, vpc_id)):
        return _fail(
            ctx,
            R.PRECHECK_PARAM_INVALID,
            verified=["없음(백업 레코드에 그룹 정의 없음)"],
            unverified=[_DRY_RUN_MISSES],
        )
    if not all(
        isinstance(payload.get(key), list)
        for key in ("ingress_permissions", "egress_permissions")
    ):
        return _fail(
            ctx,
            R.PRECHECK_PARAM_INVALID,
            verified=["없음(백업 레코드에 규칙 목록 없음)"],
            unverified=[_DRY_RUN_MISSES],
        )

    ec2 = aws_client("ec2", ctx.target.region)
    # ADR-0007 §Context 표 4·5행은 authorize 2종도 DryRun 대상으로 뒀다. create만
    # 보고 통과시키면 빈 SG만 만들어지고 규칙 복원이 권한 부족으로 실패하는 경로를
    # precheck가 그대로 통과시킨다 — ④가 막아야 할 실패가 실행 중에 난다.
    # 그룹이 아직 없는 시점에도 성립한다: 존재하지 않는 GroupId로도
    # DryRunOperation이 돌아온다(#133 ① 실측). 실 AWS 확인은 6-7주차 스모크.
    calls = [
        (
            ec2.create_security_group,
            {"GroupName": group_name, "Description": description, "VpcId": vpc_id},
        )
    ]
    for operation, permissions in (
        (ec2.authorize_security_group_ingress, payload["ingress_permissions"]),
        (ec2.authorize_security_group_egress, payload["egress_permissions"]),
    ):
        # 빈 목록으로 authorize를 부르면 DryRun 이전에 파라미터 오류가 난다.
        # 복원할 규칙이 없는 방향은 실행도 하지 않으므로 검증 대상이 아니다.
        if permissions:
            calls.append(
                (
                    operation,
                    {
                        "GroupId": ctx.target.resource_id,
                        "IpPermissions": permissions,
                    },
                )
            )
    return _dry_run_chain(
        ctx,
        calls,
        verified=["백업 레코드의 그룹 정의와 규칙 목록 구조"],
        unverified=["재생성된 그룹에 규칙이 실제로 주입된 결과"],
    )


# --- 조회 대체 (ADR-0007 §4) ---


def _network_acl(acl_id: str, region: str):
    res, code = _call(
        aws_client("ec2", region).describe_network_acls, NetworkAclIds=[acl_id]
    )
    if code is not None:
        return None, code
    acls = res.get("NetworkAcls", [])
    if not acls:
        return None, R.PRECHECK_TARGET_NOT_FOUND
    return acls[0], None


def _find_entry(acl: Mapping[str, Any], rule_number: int, egress: bool):
    for entry in acl.get("Entries", []):
        if entry.get("RuleNumber") == rule_number and bool(entry.get("Egress")) == egress:
            return entry
    return None


def _precheck_nacl_add_deny(ctx: _Ctx) -> PrecheckOutcome:
    acl, code = _network_acl(ctx.params["network_acl_id"], ctx.target.region)
    if code is not None:
        return _fail(
            ctx, code, verified=["없음(NACL 조회 실패)"], unverified=[_DESCRIBE_MISSES]
        )
    # ADD_DENY는 인바운드 차단 규칙이다 — ADR-0007 §5 파라미터 표에 egress가 없다
    if _find_entry(acl, ctx.params["rule_number"], egress=False) is not None:
        return _fail(
            ctx,
            R.PRECHECK_INVALID_STATE,
            verified=["NACL 존재"],
            unverified=[_DESCRIBE_MISSES],
        )
    return _ok(
        ctx,
        verified=["NACL 존재", "규칙 번호 미사용(인바운드)", "CIDR/프로토콜 형식"],
        unverified=[_DESCRIBE_MISSES, "삽입 자체의 AWS 검증(DryRun 미지원 작업)"],
    )


def _precheck_nacl_restore(ctx: _Ctx) -> PrecheckOutcome:
    acl, code = _network_acl(ctx.params["network_acl_id"], ctx.target.region)
    if code is not None:
        return _fail(
            ctx, code, verified=["없음(NACL 조회 실패)"], unverified=[_DESCRIBE_MISSES]
        )

    rule_number, egress = ctx.params["rule_number"], ctx.params["egress"]
    entry = _find_entry(acl, rule_number, egress)
    if entry is None:
        return _fail(
            ctx,
            R.PRECHECK_TARGET_NOT_FOUND,
            verified=["NACL 존재"],
            unverified=[_DESCRIBE_MISSES],
        )
    if entry.get("RuleAction") != "deny":
        return _fail(
            ctx,
            R.PRECHECK_INVALID_STATE,
            verified=["NACL 존재", "대상 규칙 존재"],
            unverified=[_DESCRIBE_MISSES],
        )
    # 삭제 대상이 우리가 넣은 그 규칙인지 — 백업 레코드의 rule index와 대조한다
    payload = ctx.backup.payload
    if payload.get("rule_number") != rule_number or bool(payload.get("egress")) != egress:
        return _fail(
            ctx,
            R.PRECHECK_PARAM_INVALID,
            verified=["NACL 존재", "대상 규칙이 deny 상태"],
            unverified=[_DESCRIBE_MISSES],
        )
    return _ok(
        ctx,
        verified=["NACL 존재", "대상 규칙이 deny 상태", "백업 레코드 rule index 일치"],
        unverified=[_DESCRIBE_MISSES, "삭제 자체의 AWS 검증(DryRun 미지원 작업)"],
    )


def _precheck_isolate(ctx: _Ctx) -> PrecheckOutcome:
    instance, code = _instance(ctx.params["instance_id"], ctx.target.region)
    if code is not None:
        return _fail(ctx, code, verified=["없음(인스턴스 조회 실패)"], unverified=[_DESCRIBE_MISSES])
    eni = _primary_eni(instance)
    if eni is None:
        return _fail(
            ctx,
            R.PRECHECK_INVALID_STATE,
            verified=["인스턴스 존재"],
            unverified=[_DESCRIBE_MISSES],
        )

    isolation_group_id = ctx.params["isolation_group_id"]
    code = run_dry_run(
        aws_client("ec2", ctx.target.region).modify_network_interface_attribute,
        NetworkInterfaceId=eni,
        Groups=[isolation_group_id],
    )
    if code is not None:
        return _fail(
            ctx, code, verified=["인스턴스와 ENI 존재"], unverified=[_DESCRIBE_MISSES]
        )

    _, code = _call(
        aws_client("ec2", ctx.target.region).describe_security_groups, GroupIds=[isolation_group_id]
    )
    if code is not None:
        return _fail(
            ctx,
            code,
            verified=["인스턴스와 ENI 존재", "ENI 교체 DryRun 통과"],
            unverified=[_DESCRIBE_MISSES],
        )

    res, code = _call(
        aws_client("elbv2", ctx.target.region).describe_target_health,
        TargetGroupArn=ctx.params["target_group_arn"],
        Targets=[{"Id": ctx.params["instance_id"]}],
    )
    if code is not None:
        return _fail(
            ctx,
            code,
            verified=["인스턴스와 ENI 존재", "ENI 교체 DryRun 통과", "격리용 SG 존재"],
            unverified=[_DESCRIBE_MISSES],
        )
    # Targets로 지정한 대상이 등록돼 있지 않아도 AWS는 빈 목록이 아니라
    # unused / Target.NotRegistered 설명을 돌려준다. 목록이 비었는지만 보면
    # 수집 이후 이미 이탈한 대상이 등록된 것으로 통과한다 — §4 ISOLATE 통과
    # 조건 ③이 요구하는 것은 대상이 실제로 등록돼 있는지다.
    registered = [
        description
        for description in res.get("TargetHealthDescriptions") or []
        if (description.get("TargetHealth") or {}).get("Reason")
        != "Target.NotRegistered"
    ]
    if not registered:
        return _fail(
            ctx,
            R.PRECHECK_TARGET_NOT_FOUND,
            verified=["인스턴스와 ENI 존재", "ENI 교체 DryRun 통과", "격리용 SG 존재"],
            unverified=[_DESCRIBE_MISSES],
        )
    return _ok(
        ctx,
        verified=["ENI 교체 DryRun 통과", "격리용 SG 존재", "TG 존재와 대상 등록"],
        unverified=[_DESCRIBE_MISSES, "이탈(deregister) 자체의 AWS 검증"],
    )


def _precheck_unisolate(ctx: _Ctx) -> PrecheckOutcome:
    payload = ctx.backup.payload
    restore_groups = payload.get("security_group_ids")
    target_group_arn = payload.get("target_group_arn")
    if not (
        isinstance(restore_groups, list)
        and restore_groups
        and all(_conforms(_SG_ID, group) for group in restore_groups)
        and _conforms(_TG_ARN, target_group_arn)
    ):
        return _fail(
            ctx,
            R.PRECHECK_PARAM_INVALID,
            verified=["없음(백업 레코드에 복원 대상 SG/TG 없음)"],
            unverified=[_DESCRIBE_MISSES],
        )

    instance, code = _instance(ctx.params["instance_id"], ctx.target.region)
    if code is not None:
        return _fail(ctx, code, verified=["없음(인스턴스 조회 실패)"], unverified=[_DESCRIBE_MISSES])
    eni = _primary_eni(instance)
    if eni is None:
        return _fail(
            ctx,
            R.PRECHECK_INVALID_STATE,
            verified=["인스턴스 존재"],
            unverified=[_DESCRIBE_MISSES],
        )

    code = run_dry_run(
        aws_client("ec2", ctx.target.region).modify_network_interface_attribute,
        NetworkInterfaceId=eni,
        Groups=restore_groups,
    )
    if code is not None:
        return _fail(
            ctx, code, verified=["인스턴스와 ENI 존재"], unverified=[_DESCRIBE_MISSES]
        )

    # GroupIds에 없는 SG가 하나라도 있으면 InvalidGroup.NotFound가 난다
    _, code = _call(
        aws_client("ec2", ctx.target.region).describe_security_groups, GroupIds=list(restore_groups)
    )
    if code is not None:
        return _fail(
            ctx,
            code,
            verified=["인스턴스와 ENI 존재", "SG 복원 DryRun 통과"],
            unverified=[_DESCRIBE_MISSES],
        )

    res, code = _call(
        aws_client("elbv2", ctx.target.region).describe_target_groups, TargetGroupArns=[target_group_arn]
    )
    if code is not None:
        return _fail(
            ctx,
            code,
            verified=["인스턴스와 ENI 존재", "SG 복원 DryRun 통과", "복원 대상 SG 현존"],
            unverified=[_DESCRIBE_MISSES],
        )
    groups = res.get("TargetGroups", [])
    if not groups or groups[0].get("VpcId") != instance.get("VpcId"):
        return _fail(
            ctx,
            R.PRECHECK_INVALID_STATE,
            verified=["인스턴스와 ENI 존재", "SG 복원 DryRun 통과", "복원 대상 SG 현존"],
            unverified=[_DESCRIBE_MISSES],
        )
    return _ok(
        ctx,
        verified=["SG 복원 DryRun 통과", "복원 대상 SG 전부 현존", "TG가 인스턴스와 같은 VPC"],
        unverified=[_DESCRIBE_MISSES, "재등록(register) 자체의 AWS 검증"],
    )


def _precheck_enable_autoscaling(ctx: _Ctx) -> PrecheckOutcome:
    instance_id = ctx.params["instance_id"]
    instance, code = _instance(instance_id, ctx.target.region)
    if code is not None:
        return _fail(ctx, code, verified=["없음(인스턴스 조회 실패)"], unverified=[_DESCRIBE_MISSES])
    if instance.get("State", {}).get("Name") != "running":
        return _fail(
            ctx,
            R.PRECHECK_INVALID_STATE,
            verified=["인스턴스 존재"],
            unverified=[_DESCRIBE_MISSES],
        )

    code = run_dry_run(
        aws_client("ec2", ctx.target.region).create_launch_template,
        LaunchTemplateName=_launch_template_name(instance_id),
        LaunchTemplateData={"InstanceType": instance.get("InstanceType")},
    )
    if code is not None:
        return _fail(
            ctx,
            code,
            verified=["인스턴스 running"],
            unverified=[_DESCRIBE_MISSES],
        )

    res, code = _call(
        aws_client("autoscaling", ctx.target.region).describe_auto_scaling_groups,
        AutoScalingGroupNames=[_asg_name(instance_id)],
    )
    if code is not None:
        return _fail(
            ctx,
            code,
            verified=["인스턴스 running", "Launch Template DryRun 통과"],
            unverified=[_DESCRIBE_MISSES],
        )
    if res.get("AutoScalingGroups"):
        return _fail(
            ctx,
            R.PRECHECK_INVALID_STATE,
            verified=["인스턴스 running", "Launch Template DryRun 통과"],
            unverified=[_DESCRIBE_MISSES],
        )
    return _ok(
        ctx,
        verified=[
            "인스턴스 running",
            "Launch Template DryRun 통과",
            "동명 ASG 부재",
            "min_size/max_size 범위",
        ],
        unverified=[_DESCRIBE_MISSES, "ASG 생성 자체의 AWS 검증(DryRun 미지원 작업)"],
    )


_HANDLERS: Mapping[str, Callable[[_Ctx], PrecheckOutcome]] = {
    "_precheck_isolate": _precheck_isolate,
    "_precheck_nacl_add_deny": _precheck_nacl_add_deny,
    "_precheck_nacl_restore": _precheck_nacl_restore,
    "_precheck_sg_delete": _precheck_sg_delete,
    "_precheck_rightsizing": _precheck_rightsizing,
    "_precheck_enable_autoscaling": _precheck_enable_autoscaling,
    "_precheck_ebs_delete": _precheck_ebs_delete,
    "_precheck_unisolate": _precheck_unisolate,
    "_precheck_sg_recreate": _precheck_sg_recreate,
    "_precheck_revert_size": _precheck_revert_size,
}


# ------------------------------------------------------------------ 진입점
def _load_backup(
    spec: _Spec,
    target_arn: str,
    params: Mapping[str, Any],
    loader: Optional[BackupRecordLoader],
):
    """(백업 레코드, 거절 결과) — 둘 중 하나만 채워진다.

    loader 미배선은 페이로드 문제가 아니라 호출부 배선 오류이므로 예외로 막는다.
    FAIL로 남기면 멀쩡한 원복 요청에 거절 기록이 붙는다.
    """
    if loader is None:
        raise RuntimeError(
            f"{spec.backup_type} 조회가 필요한 런북인데 backup_loader가 배선되지 않았습니다"
        )

    record_id = params.get("backup_record_id")
    if record_id:
        record = loader.get(record_id)
    else:
        # 대상으로 찾는 런북은 어느 조치의 백업인지까지 좁혀야 한다
        match = {key: params[key] for key in spec.backup_match_params}
        record = loader.latest_for_target(
            target_arn, spec.backup_type, match or None
        )
    if record is None:
        return None, _reject(spec, R.PRECHECK_TARGET_NOT_FOUND, "백업 레코드 없음")
    if record.backup_type != spec.backup_type:
        return None, _reject(spec, R.PRECHECK_PARAM_INVALID, "백업 레코드 종류 불일치")
    if record.target_arn != target_arn:
        # 다른 자원의 백업으로 원복하려는 시도 — Scope Escalation
        return None, _reject(
            spec, R.PRECHECK_PARAM_INVALID, "백업 레코드가 다른 자원을 가리킴"
        )
    return record, None


def precheck(
    runbook_id: "RunbookId | str",
    target_arn: str,
    parameters: "Mapping[str, Any] | BaseModel",
    *,
    backup_loader: Optional[BackupRecordLoader] = None,
) -> PrecheckOutcome:
    """가드레일 ④ AWS Dry-Run 판정 (ADR-0007 §1).

    동기 함수다 — boto3가 동기이고 collector의 기존 클라이언트 규약과 같다. async
    문맥에서는 호출부가 threadpool로 감싼다.

    예외를 던지지 않는다. AWS 오류·미구현 런북·파라미터 문제는 전부 PrecheckOutcome
    으로 돌아온다. 유일한 예외가 backup_loader 미배선인데, 원복 계열 런북
    (NACL_RESTORE·UNISOLATE·SG_RECREATE·REVERT_SIZE)에만 필요하다.

    반환값은 GuardrailStepResult와 1:1로 매핑된다.
    """
    if isinstance(parameters, BaseModel):
        # 변환 경로(schemas.runbook_parameters.build_precheck_parameters)의 반환
        # 모델을 그대로 받는다. 판정·핸들러는 §1대로 Mapping을 전제하므로 경계에서
        # 한 번에 떨어뜨린다 — 호출부마다 model_dump를 요구하면 잊은 곳에서
        # TypeError가 난다.
        parameters = parameters.model_dump()
    runbook_value = (
        runbook_id.value if isinstance(runbook_id, RunbookId) else str(runbook_id)
    )
    spec = RUNBOOK_SPECS.get(runbook_value)
    if spec is None:
        # ② Action Whitelist가 먼저 거르므로 방어적 경로다. AWS를 부르지 않으므로
        # 방식 표기는 형식상의 값이다.
        return PrecheckOutcome(
            passed=False,
            reason_code=R.PRECHECK_NOT_IMPLEMENTED,
            verification_summary=build_verification_summary(
                M.DESCRIBE,
                verified=["없음(미구현 런북)"],
                unverified=["AWS 대상 상태", "IAM 권한"],
            ),
        )

    problem = _validate_params(spec, parameters)
    if problem is not None:
        return _reject(spec, R.PRECHECK_PARAM_INVALID, problem)

    target = parse_arn(target_arn)
    if target is None:
        return _reject(spec, R.PRECHECK_PARAM_INVALID, "target_arn 형식 위반")

    problem = _validate_scope(spec, target, parameters)
    if problem is not None:
        return _reject(spec, R.PRECHECK_PARAM_INVALID, problem)

    backup = None
    if spec.backup_type is not None:
        backup, rejected = _load_backup(spec, target_arn, parameters, backup_loader)
        if rejected is not None:
            return rejected

    ctx = _Ctx(runbook_value, spec, target_arn, target, parameters, backup)
    return _HANDLERS[spec.handler](ctx)


# ==============================================================================
# 실행 (Issue #211)
# ==============================================================================
# precheck과 같은 규약이다 — **예외를 던지지 않는다.** AWS 오류는
# errors.reason_code_for()의 공용 표로 분류해 ExecutionOutcome에 싣는다. 실행 도중
# 예외가 밖으로 나가면 호출부의 트랜잭션이 열린 채 끊겨 "어디까지 바뀌었는가"가
# 남지 않는다 — 자동 원복이 판단할 근거가 사라진다는 뜻이다.
#
# DB를 모른다. AWS 호출 직전에 IN_PROGRESS 단계를, 직후에 종료 단계를 record_step으로
# 넘길 뿐이고 저장·커밋 순서는 workflows.py가 소유한다 — services/aws/backup.py가
# 캡처만 하고 저장을 넘긴 것과 같은 경계다. 다만 **record_step이 던지는 예외는 막지
# 않는다** — 기록이 되지 않는 채로 자산을 계속 바꾸면 어디까지 갔는지 아무 데도 남지
# 않는다. 그 경우 실행은 IN_PROGRESS로 남고 회수(dispatcher.py)가 집는다.
#
# effect는 "자산이 실제로 바뀌었는가"이며 자동 원복 판단의 입력이다. 낙관적으로 적지
# 않는다 — 4xx 거절과 스로틀링처럼 **작업 이전에 반려된 것이 확실한** 실패만
# NOT_APPLIED이고, 5xx 서버 오류·연결 실패처럼 적용 여부를 알 수 없는 실패는
# UNKNOWN이다(_effect_for).


# 단계 유형 — schemas.executions가 어휘를 Enum으로 확정할 때까지 문자열이다(#55 헤더).
STEP_STOP_INSTANCE = "STOP_INSTANCE"
STEP_MODIFY_INSTANCE_TYPE = "MODIFY_INSTANCE_TYPE"
STEP_START_INSTANCE = "START_INSTANCE"
# 원복 전 상태 대조(ADR-0008 §3-2). 자산을 바꾸지 않는 단계라, 원복을 **진행하는**
# 경우에는 기록하지 않는다 — 기록하면 "단계 1건 이상 = 자산이 바뀌었을 수 있다"는
# 회수 규약(ADR-0008 §7)이 거짓이 되어, 아무것도 안 바꾼 실행이 재실행 대신 종료
# 판정으로 가서 실패로 확정된다. 남기는 것은 대조 자체가 결론인 두 경우뿐이다.
STEP_COMPARE_INSTANCE_TYPE = "COMPARE_INSTANCE_TYPE"

_OP_STOP = "ec2.stop_instances"
_OP_MODIFY = "ec2.modify_instance_attribute"
_OP_START = "ec2.start_instances"
_OP_DESCRIBE = "ec2.describe_instances"

# 정지 확인 대기 — 5초 간격 40회(최대 200초). 초과는 "실패"가 아니라 "상태 불명"이라
# 단계 effect가 UNKNOWN이 되고, 타입 변경으로 넘어가지 않는다.
STOP_WAIT_DELAY_SECONDS = 5
STOP_WAIT_MAX_ATTEMPTS = 40

# 요약 문자열 저장 한도는 1024자(db.models)다. 그보다 넉넉히 줄여 원인 앞부분을 남긴다.
_SUMMARY_LIMIT = 400


@dataclass(frozen=True)
class ExecutionOutcome:
    """실행 1건의 결과. steps는 시도한 순서 그대로다.

    deferred는 **판정을 못 해 자산을 만지지 않았다**는 뜻이다(원복 경로 전용).
    실패와 나누는 이유는 rollback.StatusCheckOutcome.probe_failed와 같다 — AWS에
    물어보지 못한 것은 조치가 실패했다는 근거가 아니고, 확정하면 되돌릴 것이 없는
    자산에 "원복 실패" 기록이 붙는다. 보류는 단계를 남기지 않으므로 다음 주기가
    처음부터 다시 시도한다(ADR-0008 §7). 재시도 상한은 Issue #249다.
    """

    steps: tuple[ExecutionStepResult, ...] = ()
    reason_code: Optional[PrecheckReasonCode] = None
    error_summary: Optional[str] = None
    deferred: bool = False

    def __post_init__(self) -> None:
        if (self.reason_code is None) != (self.error_summary is None):
            raise ValueError("실패에는 reason_code와 error_summary가 함께 필요합니다")
        if self.deferred:
            if self.reason_code is None:
                raise ValueError("보류에도 분류 코드가 필요합니다")
            if self.steps:
                # 자산을 만졌으면 보류가 아니다 — 되돌릴 것이 남은 실패다
                raise ValueError("보류 결과에는 단계 기록이 없어야 합니다")

    @property
    def succeeded(self) -> bool:
        return self.reason_code is None


# 단계 1건을 받는 호출부 콜백. IN_PROGRESS로 한 번, 종료 상태로 한 번 불린다.
StepRecorder = Callable[[ExecutionStepResult], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _summarize(exc: BaseException) -> str:
    """실패 요약 한 줄. AWS 오류 코드를 앞에 세워 원인을 먼저 읽게 한다."""
    if isinstance(exc, ClientError):
        message = str(exc.response.get("Error", {}).get("Message", "")).strip()
        code = aws_error_code(exc)
        text = f"{code}: {message}" if message else code
    else:
        text = f"{type(exc).__name__}: {exc}".strip()
    return text[:_SUMMARY_LIMIT] or type(exc).__name__


# 5xx로 오지만 "작업 이전에 거절됐다"가 확실한 오류 — 스로틀링이 그렇다(EC2의
# RequestLimitExceeded는 503으로 온다). 자산은 손대지 않은 채 요청만 반려된 것이다.
_THROTTLED_CODES: frozenset[str] = frozenset(
    {"RequestLimitExceeded", "Throttling", "ThrottlingException", "SlowDown"}
)


def _effect_for(exc: BaseException) -> ExecutionEffect:
    """이 실패가 자산을 바꿨는가. 확실할 때만 NOT_APPLIED로 적는다.

    자동 원복은 "되돌릴 것이 있는가"를 effect로만 판단한다. 그래서 오류 응답이
    왔다는 사실만으로 변경 없음이라 단정하지 않는다 — 오류를 전부 NOT_APPLIED로
    적으면 서버 오류로 끊긴 변경이 기록상 사라진다.

    - 4xx 거절(IncorrectInstanceState 등): 요청이 받아들여지지 않았다 → NOT_APPLIED
    - 5xx 서버 오류: AWS가 작업을 시작했는지 알 수 없다 → UNKNOWN
    - 스로틀링: 5xx여도 작업 이전 반려라 변경이 없다 → NOT_APPLIED
    - 상태 코드를 읽지 못한 응답·AWS에 닿지 못한 실패 → UNKNOWN
    - ParamValidationError는 botocore가 네트워크 호출 이전에 낸다 → NOT_APPLIED
      (errors.reason_code_for가 같은 이유로 PARAM_INVALID로 분류한다)
    """
    if isinstance(exc, ParamValidationError):
        return ExecutionEffect.NOT_APPLIED
    if not isinstance(exc, ClientError):
        return ExecutionEffect.UNKNOWN
    if aws_error_code(exc) in _THROTTLED_CODES:
        return ExecutionEffect.NOT_APPLIED
    status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    if isinstance(status, int) and 400 <= status < 500:
        return ExecutionEffect.NOT_APPLIED
    return ExecutionEffect.UNKNOWN


def _request_id(payload: Any) -> Optional[str]:
    """AWS 요청 ID. 정상 응답과 ClientError.response 어느 쪽에서도 같은 자리다."""
    if not isinstance(payload, Mapping):
        return None
    request_id = (payload.get("ResponseMetadata") or {}).get("RequestId")
    return request_id if _non_empty_str(request_id) else None


class _StepLog:
    """단계 기록기 — 호출 직전 IN_PROGRESS, 직후 종료 결과. 순서와 누적을 한자리에 둔다."""

    def __init__(self, affected_arn: str, record: Optional[StepRecorder]) -> None:
        self._arn = affected_arn
        self._record: StepRecorder = record if record is not None else _ignore_step
        self._current: tuple[int, str, str] = (0, "", "")
        self.steps: list[ExecutionStepResult] = []

    def begin(self, sequence: int, step_type: str, aws_operation: str) -> None:
        self._current = (sequence, step_type, aws_operation)
        self._emit(status=ExecutionStepStatus.IN_PROGRESS)

    def succeed(
        self, effect: ExecutionEffect, summary: str, *, response: Any = None
    ) -> None:
        self._emit(
            status=ExecutionStepStatus.SUCCESS,
            effect=effect,
            result_summary=summary,
            aws_request_id=_request_id(response),
        )

    def fail(self, exc: BaseException, *, detail: str) -> None:
        self._emit(
            status=ExecutionStepStatus.FAILED,
            effect=_effect_for(exc),
            error_summary=f"{detail}: {_summarize(exc)}"[:_SUMMARY_LIMIT],
            aws_request_id=_request_id(
                exc.response if isinstance(exc, ClientError) else None
            ),
        )

    def _emit(self, **fields: Any) -> None:
        sequence, step_type, aws_operation = self._current
        step = ExecutionStepResult(
            sequence=sequence,
            affected_arn=self._arn,
            step_type=step_type,
            aws_operation=aws_operation,
            occurred_at=_utcnow(),
            **fields,
        )
        if step.status is not ExecutionStepStatus.IN_PROGRESS:
            self.steps.append(step)
        self._record(step)


def _ignore_step(step: ExecutionStepResult) -> None:
    """기록하지 않는 호출부(단위 테스트·스모크)의 기본 콜백."""


def _abort(log: _StepLog, exc: BaseException, *, detail: str) -> ExecutionOutcome:
    """실패 단계를 남기고 실행을 끝낸다. 뒤 단계는 시도하지 않는다."""
    log.fail(exc, detail=detail)
    logger.warning(
        "execution_step_failed",
        extra={"aws_operation": log.steps[-1].aws_operation, "detail": detail},
    )
    return ExecutionOutcome(
        steps=tuple(log.steps),
        reason_code=reason_code_for(exc),
        error_summary=f"{detail}: {_summarize(exc)}"[:_SUMMARY_LIMIT],
    )


def _rejected(detail: str) -> ExecutionOutcome:
    """AWS에 닿기 전에 끝난 거절 — 단계가 하나도 없다."""
    return ExecutionOutcome(reason_code=R.PRECHECK_PARAM_INVALID, error_summary=detail)


def _previous_state(response: Any) -> str:
    """stop_instances 응답이 알려 주는 조치 직전 상태. 알 수 없으면 빈 문자열."""
    if not isinstance(response, Mapping):
        return ""
    for changed in response.get("StoppingInstances") or []:
        name = (changed.get("PreviousState") or {}).get("Name")
        if _non_empty_str(name):
            return str(name)
    return ""


def execute_rightsizing(
    target_arn: str,
    *,
    target_instance_type: str,
    record_step: Optional[StepRecorder] = None,
) -> ExecutionOutcome:
    """`RUNBOOK_EC2_RIGHTSIZING` 실행 — 정지 → 타입 변경 → 기동. (Issue #211)

    **스펙 JSON 백업이 commit된 뒤에만 부른다**(workflows.store_instance_spec_backup).
    타입을 바꾸고 나면 바꾸기 전 값을 어디서도 얻을 수 없어, 백업이 없으면 되돌릴
    근거가 사라진다(ADR-0004 롤백 공통 정책 ③).

    타입 변경은 **stopped 상태에서만** 받는다. 그래서 정지가 조치의 일부이고, 조치
    직전에 running이던 인스턴스만 마지막에 다시 기동한다 — 원래 멈춰 있던 인스턴스를
    켜는 것은 이 런북이 요청받은 변경이 아니다.

    타입 변경이 실패하면 인스턴스는 **정지된 채로 남는다.** 여기서 되돌리지 않는
    이유는 원복 경로를 하나로 두기 위해서다 — `RUNBOOK_EC2_REVERT_SIZE`
    (`trigger_source=AUTO_ON_FAILURE`)가 백업 레코드를 근거로 되돌린다. 실행부가
    자체 보상까지 하면 어느 쪽이 자산을 만졌는지 기록이 갈린다.

    기동은 요청 접수까지다. 2/2 Status Check 확인·타임아웃 판정은
    services/aws/rollback.py 몫이라 여기서 기다리지 않는다(파일 헤더 경계).
    """
    target = parse_arn(target_arn)
    if target is None or target.resource_type != "instance":
        return _rejected(f"인스턴스 ARN이 아닙니다: {target_arn}")
    if not _non_empty_str(target_instance_type):
        return _rejected("target_instance_type이 비어 있습니다")

    instance_id = target.resource_id
    ec2 = aws_client("ec2", target.region)
    log = _StepLog(target_arn, record_step)

    # ① 정지 — 타입 변경의 전제 조건이다
    log.begin(1, STEP_STOP_INSTANCE, _OP_STOP)
    try:
        response = ec2.stop_instances(InstanceIds=[instance_id])
    except (ClientError, BotoCoreError) as exc:
        return _abort(log, exc, detail="인스턴스 정지 요청 실패")
    previous_state = _previous_state(response)
    # 상태를 읽지 못했으면 기동하는 쪽으로 둔다 — 조치 대상은 running 인스턴스이고,
    # 켜야 할 것을 끈 채로 두는 편이 더 나쁜 결과다
    was_running = previous_state != "stopped"
    try:
        ec2.get_waiter("instance_stopped").wait(
            InstanceIds=[instance_id],
            WaiterConfig={
                "Delay": STOP_WAIT_DELAY_SECONDS,
                "MaxAttempts": STOP_WAIT_MAX_ATTEMPTS,
            },
        )
    except (ClientError, BotoCoreError) as exc:
        # 정지 요청은 접수됐고 최종 상태만 확인하지 못했다 — WaiterError는
        # BotoCoreError라 _effect_for가 UNKNOWN을 준다. 이 상태로 타입 변경을 걸면
        # IncorrectInstanceState로 거절되므로 여기서 끝낸다.
        return _abort(log, exc, detail="정지 확인 실패")
    log.succeed(
        ExecutionEffect.APPLIED,
        f"정지 확인(조치 직전 상태: {previous_state or '알 수 없음'})",
        response=response,
    )

    # ② 타입 변경
    log.begin(2, STEP_MODIFY_INSTANCE_TYPE, _OP_MODIFY)
    try:
        response = ec2.modify_instance_attribute(
            InstanceId=instance_id, InstanceType={"Value": target_instance_type}
        )
    except (ClientError, BotoCoreError) as exc:
        return _abort(log, exc, detail="인스턴스 타입 변경 실패")
    log.succeed(
        ExecutionEffect.APPLIED, f"타입 변경: {target_instance_type}", response=response
    )

    # ③ 기동 — 조치 직전에 running이었을 때만
    log.begin(3, STEP_START_INSTANCE, _OP_START)
    if not was_running:
        log.succeed(ExecutionEffect.NOT_APPLIED, "조치 직전 stopped 상태라 기동하지 않음")
        return ExecutionOutcome(steps=tuple(log.steps))
    try:
        response = ec2.start_instances(InstanceIds=[instance_id])
    except (ClientError, BotoCoreError) as exc:
        # 타입은 이미 바뀐 채 멈춰 있다 — 원복 판단은 호출부·rollback.py 몫이다
        return _abort(log, exc, detail="인스턴스 기동 요청 실패")
    log.succeed(
        ExecutionEffect.APPLIED,
        "기동 요청 접수(2/2 Status Check 확인은 별도 축)",
        response=response,
    )
    return ExecutionOutcome(steps=tuple(log.steps))


# ------------------------------------------------------------------ 원복 (Issue #241)
def current_instance_type_and_state(instance_id: str, region: str):
    """(현재 인스턴스 타입, 현재 state, 사유 코드) 3짝. 타입을 못 읽으면 코드가 채워진다.

    실행과 종료 판정이 같은 축을 같은 방법으로 읽어야 해서 공개한다(ADR-0008 §3-2의
    대조와 workflows.judge_revert_size의 실자산 대조가 그 둘이다). 읽는 방법이 갈리면
    "되돌아왔는가"의 답이 자리마다 달라진다.

    **타입과 state를 describe 한 번으로 함께 읽는다.** 나눠 부르면 두 호출 사이에
    상태가 바뀌어 "타입은 되돌아왔는데 state는 그 이전 것"인 조합을 판정이 보게 되고,
    그 조합은 실재한 적 없는 자산이다.

    state는 부가 축이라 없다고 판정을 막지 않는다(None으로 돌려준다) — 대조의 축은
    타입이고, state는 "기동까지 끝났는가"를 덧붙여 묻는 자리이기 때문이다.
    """
    instance, code = _instance(instance_id, region)
    if code is not None:
        return None, None, code
    found = instance.get("InstanceType")
    if not _non_empty_str(found):
        # 조회는 됐는데 타입이 없다 — 대조할 축이 없으므로 대상 상태 문제다
        return None, None, R.PRECHECK_INVALID_STATE
    state = instance.get("State", {}).get("Name")
    return str(found), (str(state) if _non_empty_str(state) else None), None


def current_instance_type(instance_id: str, region: str):
    """(현재 인스턴스 타입, 사유 코드) 짝 — state가 필요 없는 자리의 축약형."""
    found, _state, code = current_instance_type_and_state(instance_id, region)
    return found, code


def _deferred(code: PrecheckReasonCode, detail: str) -> ExecutionOutcome:
    """대조하지 못해 원복을 시작하지 않았다 — 실패가 아니라 보류다."""
    logger.warning(
        "revert_size_deferred",
        extra={"reason_code": code.value, "aws_operation": _OP_DESCRIBE},
    )
    return ExecutionOutcome(reason_code=code, error_summary=detail, deferred=True)


def execute_revert_size(
    target_arn: str,
    *,
    restore_instance_type: str,
    applied_instance_type: str,
    restore_state: str,
    record_step: Optional[StepRecorder] = None,
) -> ExecutionOutcome:
    """`RUNBOOK_EC2_REVERT_SIZE` 실행 — 상태 대조 → 정지 → 타입 원복 → 기동. (Issue #241)

    execute_rightsizing과 같은 규약이다 — **예외를 던지지 않고** 단계별 결과를
    돌려준다. 다른 것은 앞에 붙는 대조 하나다.

    **되돌릴 값은 전부 인자로 받는다.** 이 함수는 백업 레코드도 DB도 읽지 않는다 —
    원복 값의 유일한 원천이 백업 레코드라는 정책(ADR-0004 정책 ③)은 호출부가 그
    레코드에서만 값을 뽑아 넘길 때 성립하며, 여기서 다시 조회하면 원천이 둘이 된다.
    `restore_state`도 같은 이유로 백업 payload의 `state`다 — 원본 실행이 정지 응답에서
    읽은 PreviousState는 그 실행 안에서만 쓴다(ADR-0008 §4).

    **`applied_instance_type`은 원본 조치가 적용한 타입이다.** 되돌릴 값이 아니라
    대조 축이며, 이것이 없으면 아래 3분기 중 ②와 ③을 가를 수 없다.

    상태 대조 3분기(ADR-0008 §3-2) — 위에서 아래로, 처음 일치하는 곳에서 멈춘다.
      ① 현재 타입 == 백업 값: 변경이 적용되지 않았거나 누군가 이미 되돌렸다 →
         **AWS 변경 호출을 하지 않는다.** 되돌릴 것이 없음을 NOT_APPLIED 단계로 남긴다.
         원본 조치가 타입을 실제로 바꾸지 않은 경우 ①과 ②가 동시에 참인데 ①이 이긴다 —
         할 일이 없는 실행을 거절로 올려 사람을 부르지 않기 위해서다.
      ② 현재 타입 == 원본이 적용한 값: 우리가 바꾼 그대로다 → 원복을 진행한다.
      ③ 둘 다 아님: 제3자가 그사이 타입을 바꿨다 → **중단하고 CRITICAL.** 자동
         재시도는 없다(ADR-0008 §6). 백업을 무조건 진실로 삼으면 원복이 남의 변경을
         조용히 덮어쓴다 — 조회 1회로 막을 수 있으면 막는다.

    대조 자체를 하지 못하면(AWS 조회 실패) 원복을 진행하지 않고 **보류**한다.
    검증기의 실패는 자산이 제3자에게 바뀌었다는 근거가 아니다.
    """
    target = parse_arn(target_arn)
    if target is None or target.resource_type != "instance":
        return _rejected(f"인스턴스 ARN이 아닙니다: {target_arn}")
    if not _non_empty_str(restore_instance_type):
        return _rejected("백업 레코드의 instance_type이 비어 있습니다")
    if not _non_empty_str(applied_instance_type):
        return _rejected("원본 조치가 적용한 instance_type을 알 수 없습니다")

    instance_id = target.resource_id
    current, code = current_instance_type(instance_id, target.region)
    if code is not None:
        if code is R.PRECHECK_TARGET_NOT_FOUND:
            # 인스턴스가 없으면 되돌릴 대상이 없다 — 다시 물어도 답은 같으므로 확정한다
            return ExecutionOutcome(
                reason_code=code,
                error_summary=f"원복 대상 인스턴스를 찾을 수 없습니다: {instance_id}",
            )
        return _deferred(code, f"상태 대조 실패로 원복 보류: {code.value}")

    log = _StepLog(target_arn, record_step)

    if current == restore_instance_type:
        log.begin(1, STEP_COMPARE_INSTANCE_TYPE, _OP_DESCRIBE)
        log.succeed(
            ExecutionEffect.NOT_APPLIED,
            f"이미 백업 스펙 상태입니다({current}) — 되돌릴 것이 없어 변경하지 않음",
        )
        return ExecutionOutcome(steps=tuple(log.steps))

    if current != applied_instance_type:
        log.begin(1, STEP_COMPARE_INSTANCE_TYPE, _OP_DESCRIBE)
        detail = (
            f"제3자 변경 감지 — 현재 {current}, 백업 {restore_instance_type},"
            f" 조치 적용 {applied_instance_type}"
        )
        log.succeed(ExecutionEffect.NOT_APPLIED, f"{detail}. 원복을 중단합니다")
        logger.critical(
            "revert_size_third_party_drift",
            extra={
                "instance_id": instance_id,
                "current_instance_type": current,
                "restore_instance_type": restore_instance_type,
                "applied_instance_type": applied_instance_type,
            },
        )
        return ExecutionOutcome(
            steps=tuple(log.steps),
            reason_code=R.PRECHECK_INVALID_STATE,
            error_summary=detail[:_SUMMARY_LIMIT],
        )

    # ② 우리가 바꾼 그대로다 — 대조는 기록하지 않는다(STEP_COMPARE_INSTANCE_TYPE 주석)
    ec2 = aws_client("ec2", target.region)
    log.begin(1, STEP_STOP_INSTANCE, _OP_STOP)
    try:
        response = ec2.stop_instances(InstanceIds=[instance_id])
    except (ClientError, BotoCoreError) as exc:
        return _abort(log, exc, detail="인스턴스 정지 요청 실패")
    try:
        ec2.get_waiter("instance_stopped").wait(
            InstanceIds=[instance_id],
            WaiterConfig={
                "Delay": STOP_WAIT_DELAY_SECONDS,
                "MaxAttempts": STOP_WAIT_MAX_ATTEMPTS,
            },
        )
    except (ClientError, BotoCoreError) as exc:
        return _abort(log, exc, detail="정지 확인 실패")
    log.succeed(
        ExecutionEffect.APPLIED,
        f"정지 확인(조치 직전 상태: {_previous_state(response) or '알 수 없음'})",
        response=response,
    )

    log.begin(2, STEP_MODIFY_INSTANCE_TYPE, _OP_MODIFY)
    try:
        response = ec2.modify_instance_attribute(
            InstanceId=instance_id, InstanceType={"Value": restore_instance_type}
        )
    except (ClientError, BotoCoreError) as exc:
        return _abort(log, exc, detail="인스턴스 타입 원복 실패")
    log.succeed(
        ExecutionEffect.APPLIED,
        f"타입 원복: {restore_instance_type}",
        response=response,
    )

    # 다시 켤지는 백업 레코드의 state가 정한다(ADR-0008 §4) — 조치 이전에 멈춰 있던
    # 인스턴스를 원복하면서 켜는 것은 되돌리기가 아니라 새 변경이다
    log.begin(3, STEP_START_INSTANCE, _OP_START)
    if restore_state != "running":
        log.succeed(
            ExecutionEffect.NOT_APPLIED,
            f"조치 이전 상태가 {restore_state}라 기동하지 않음",
        )
        return ExecutionOutcome(steps=tuple(log.steps))
    try:
        response = ec2.start_instances(InstanceIds=[instance_id])
    except (ClientError, BotoCoreError) as exc:
        # 타입은 되돌아갔고 멈춰 있다 — 원복의 원복은 없으므로 수동 개입이 남는다
        return _abort(log, exc, detail="원복 후 기동 요청 실패")
    log.succeed(
        ExecutionEffect.APPLIED,
        "기동 요청 접수(2/2 Status Check는 원복 성공 판정의 축이 아니다)",
        response=response,
    )
    return ExecutionOutcome(steps=tuple(log.steps))
