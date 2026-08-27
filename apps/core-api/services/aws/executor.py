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
# [남은 작업]
# 1. 확정 10종 실행 함수(execute) — 조치 전 스펙 JSON 백업 후 상태 변경
# 2. 롤백 3종 실행도 executor 경유 — 트리거 판단·감시는 rollback.py 담당
#
# 파라미터 계약의 원천은 packages/schemas/runbook_parameters.py의 typed 모델이다(#154).
# 형식 위반은 AI 후보라면 ① Schema Check에서 먼저 걸리고, 여기 _validate_params는 같은
# 모델로 한 번 더 본다 — ④를 타는 경로가 그것만이 아니기 때문이다(롤백 3종·시스템
# 트리거는 ①을 거치지 않는다). 후보(RunbookCandidateDraft)를 여기 parameters로 바꾸는
# 변환은 runbook_parameters.py의 build_precheck_parameters()다.
# ==============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, TypeAdapter, ValidationError

from schemas.backups import BackupType
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
from .errors import reason_code_for, run_dry_run

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
        (런북 명세서 parameters_schema 기준).

        payload_match가 있으면 payload의 해당 키가 전부 같은 레코드만 후보다.
        한 자원에 조치가 누적되면 대상의 최신 하나만으로는 복원 대상을 고를 수
        없다 — NACL 하나에 deny 규칙이 둘 이상 쌓이면 오래된 규칙은 복원할 수
        없게 된다(최신 백업이 항상 다른 규칙을 가리키므로).
        """


# 백업 종류 — 어휘의 원천은 schemas.backups.BackupType이다(런북 명세서
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
        # 런북 명세서의 "available 상태 2차 검증"은 실행 직전 단계의 몫이다
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
    # ADD_DENY는 인바운드 차단 규칙이다 — 런북 명세서 parameters_schema에 egress가 없다
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
