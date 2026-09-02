# ==============================================================================
# [파일 설명]  담당: 안성일 (AI/Guardrail · Architect)
# Runbook별 파라미터의 typed 계약입니다. (Issue #154)
# ID 수준 판정은 runbooks.py가 담당하고, 이 파일은 그 아래 층 — "각 Runbook이
# 어떤 값을 받는가"를 정의한다. 원천은 ADR-0007 §5 파라미터 표이며, 전 키가 required다.
#
# 두 계열로 나뉜다. 같은 Runbook이라도 AI가 채우는 값과 실행이 받는 값이 다르다.
#   ① 후보 파라미터(본편 7종) — AI가 정하는 값만. 나머지는 지어낼 수 없다.
#      RUNBOOK_EC2_ISOLATE·SG_DELETE_ISOLATED·EBS_DELETE_UNATTACHED는 AI가 정할
#      값이 0개라 빈 모델이다. 빈 모델은 낭비가 아니라 방어다 — extra=forbid가
#      "이 Runbook에는 AI가 값을 실을 자리가 없다"를 강제한다.
#   ② precheck 파라미터(10종) — §5 표 전체. executor.precheck()가 받는 것.
#
# 사이를 build_precheck_parameters()가 잇는다. 후보가 싣지 않는 값의 출처는 셋이다.
#   - target_arn이 가리키는 자원 ID → resource_id 인자 (RESOURCE_ID_PARAM 참조)
#   - DB·AWS 조회 → target_group_arn·isolation_group_id·current_instance_type 인자
#   - 후보 evidence_ids의 첫 항목 → evidence_id (선택 규칙은 그 함수가 정의한다)
#
# 롤백 3종은 AI 후보가 될 수 없어 이 변환 경로를 타지 않는다(ADR-0004 정책 ②).
# 모델만 두는 이유는 그 셋도 precheck()를 타기 때문이며, parameters를 구성하는
# 복구 접수 경로는 #126이 만든다.
#
# 값 제약은 services/aws/executor.py가 술어 함수로 갖고 있던 것을 그대로 옮긴 것이다
# — 형식 위반은 이제 ④ AWS Dry-Run이 아니라 ① Schema Check에서 걸린다. bool을 int로
# 받지 않기 위해 StrictInt·StrictBool을 쓴다(bool은 int의 하위 타입이라 일반 int
# 필드가 True를 1로 받아들인다).
# ==============================================================================

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Any, Literal, Mapping, Optional, Sequence, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

from .runbooks import ROLLBACK_RUNBOOK_IDS, RunbookId

# ------------------------------------------------------------------------------
# 값 제약 — executor의 술어 함수와 1:1이다
# ------------------------------------------------------------------------------


def _fullmatch(pattern: str) -> AfterValidator:
    """부분 일치를 허용하지 않는다. 정규식 엔진과 무관하게 fullmatch로 고정한다."""
    compiled = re.compile(pattern)

    def check(value: str) -> str:
        if not compiled.fullmatch(value):
            raise ValueError(f"{pattern} 형식이어야 합니다")
        return value

    return AfterValidator(check)


def _reject_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("공백만으로 이루어질 수 없습니다")
    return value


def _require_network_cidr(value: str) -> str:
    """접두 길이가 있고 호스트 비트가 서지 않은 네트워크 주소만 받는다.

    ipaddress는 맨 IP를 /32로, "203.0.113.5/24"를 203.0.113.0/24로 해석해 주지만,
    차단 대역을 다루는 필드에서 그 자동 해석은 AI가 의도한 범위와 실제 차단 범위를
    조용히 가른다 — 호스트 하나를 노린 값이 256개 주소 차단이 된다(PR #178 리뷰).
    보정하지 않고 거절해, 값이 호스트(/32)인지 대역(canonical CIDR)인지를 AI가
    값으로 밝히게 한다.
    """
    if "/" not in value:
        raise ValueError("접두 길이가 있는 IPv4 CIDR이어야 합니다")
    try:
        ipaddress.IPv4Network(value, strict=True)
    except ValueError as exc:
        raise ValueError("호스트 비트가 없는 네트워크 주소 IPv4 CIDR이어야 합니다") from exc
    return value


# 자유 형식 문자열의 상한은 ai/guardrails.py의 ① 잠정 상한을 옮겨 온 것이다
# (LLM이 지은 문자열이 JSONB 컬럼과 관제 화면까지 그대로 흘러가는 것을 막는다).
_FreeText = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_reject_blank)]
_EvidenceId = Annotated[
    str, Field(min_length=1, max_length=36), AfterValidator(_reject_blank)
]  # DB의 UUID 길이

# ADR-0007 §5 파라미터 표의 pattern을 그대로 옮긴다
InstanceId = Annotated[str, _fullmatch(r"i-[a-f0-9]{8,17}")]
SecurityGroupId = Annotated[str, _fullmatch(r"sg-[a-f0-9]{8,17}")]
NetworkAclId = Annotated[str, _fullmatch(r"acl-[a-f0-9]{8,17}")]
VolumeId = Annotated[str, _fullmatch(r"vol-[a-f0-9]{8,17}")]
TargetGroupArn = Annotated[
    str, _fullmatch(r"arn:aws:elasticloadbalancing:.*:targetgroup/.*")
]
RuleNumber = Annotated[StrictInt, Field(ge=1, le=32766)]
AutoScalingSize = Annotated[StrictInt, Field(ge=1, le=4)]
Ipv4Cidr = Annotated[str, AfterValidator(_require_network_cidr)]
# "-1"은 AWS의 전체 프로토콜 표기다
NaclProtocol = Literal["tcp", "udp", "icmp", "-1"]


class _Parameters(BaseModel):
    """파라미터 모델 공통 — 알 수 없는 키를 받지 않는다.

    이 규칙이 ADR-0007 §5 ①("롤백 3종은 원복 값을 파라미터로 받지 않는다")을
    그대로 강제한다. 원본 SG 규칙·인스턴스 타입이 실려 오면 알 수 없는 키가 되어
    여기서 거절된다.
    """

    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------
# ① 후보 파라미터 — AI가 정하는 값만 (본편 7종)
# ------------------------------------------------------------------------------


class Ec2IsolateCandidateParameters(_Parameters):
    """격리 대상·격리용 SG·Target Group은 전부 시스템이 정한다 — AI가 정할 값이 없다."""


class NaclAddDenyCandidateParameters(_Parameters):
    rule_number: RuleNumber
    cidr_block: Ipv4Cidr
    protocol: NaclProtocol


class NaclRestoreCandidateParameters(_Parameters):
    rule_number: RuleNumber
    egress: StrictBool


class SgDeleteIsolatedCandidateParameters(_Parameters):
    """삭제 대상 SG는 target_arn이 가리킨다 — AI가 정할 값이 없다."""


class Ec2RightsizingCandidateParameters(_Parameters):
    target_instance_type: _FreeText


class Ec2EnableAutoscalingCandidateParameters(_Parameters):
    min_size: AutoScalingSize
    max_size: AutoScalingSize

    @model_validator(mode="after")
    def _size_order(self):
        if self.min_size > self.max_size:
            raise ValueError("min_size는 max_size보다 클 수 없습니다")
        return self


class EbsDeleteUnattachedCandidateParameters(_Parameters):
    """삭제 대상 볼륨은 target_arn이 가리킨다 — AI가 정할 값이 없다."""


CandidateParameters = Union[
    Ec2IsolateCandidateParameters,
    NaclAddDenyCandidateParameters,
    NaclRestoreCandidateParameters,
    SgDeleteIsolatedCandidateParameters,
    Ec2RightsizingCandidateParameters,
    Ec2EnableAutoscalingCandidateParameters,
    EbsDeleteUnattachedCandidateParameters,
]


# ------------------------------------------------------------------------------
# ② precheck 파라미터 — ADR-0007 §5 표 전체 (10종)
# ------------------------------------------------------------------------------


class Ec2IsolateParameters(_Parameters):
    instance_id: InstanceId
    target_group_arn: TargetGroupArn
    isolation_group_id: SecurityGroupId
    evidence_id: _EvidenceId


class NaclAddDenyParameters(_Parameters):
    network_acl_id: NetworkAclId
    rule_number: RuleNumber
    cidr_block: Ipv4Cidr
    protocol: NaclProtocol
    evidence_id: _EvidenceId


class NaclRestoreParameters(_Parameters):
    network_acl_id: NetworkAclId
    rule_number: RuleNumber
    egress: StrictBool
    evidence_id: _EvidenceId


class SgDeleteIsolatedParameters(_Parameters):
    group_id: SecurityGroupId
    evidence_id: _EvidenceId


class Ec2RightsizingParameters(_Parameters):
    instance_id: InstanceId
    current_instance_type: _FreeText
    target_instance_type: _FreeText
    evidence_id: _EvidenceId


class Ec2EnableAutoscalingParameters(_Parameters):
    instance_id: InstanceId
    min_size: AutoScalingSize
    max_size: AutoScalingSize
    evidence_id: _EvidenceId

    @model_validator(mode="after")
    def _size_order(self):
        if self.min_size > self.max_size:
            raise ValueError("min_size는 max_size보다 클 수 없습니다")
        return self


class EbsDeleteUnattachedParameters(_Parameters):
    volume_id: VolumeId
    evidence_id: _EvidenceId


class Ec2UnisolateParameters(_Parameters):
    instance_id: InstanceId
    backup_record_id: _FreeText
    evidence_id: _EvidenceId


class SgRecreateParameters(_Parameters):
    """복원 대상은 백업 레코드가 가리킨다 — 자원 ID를 받지 않는 유일한 런북이다."""

    backup_record_id: _FreeText
    evidence_id: _EvidenceId


class Ec2RevertSizeParameters(_Parameters):
    instance_id: InstanceId
    backup_record_id: _FreeText
    evidence_id: _EvidenceId


RunbookParameters = Union[
    Ec2IsolateParameters,
    NaclAddDenyParameters,
    NaclRestoreParameters,
    SgDeleteIsolatedParameters,
    Ec2RightsizingParameters,
    Ec2EnableAutoscalingParameters,
    EbsDeleteUnattachedParameters,
    Ec2UnisolateParameters,
    SgRecreateParameters,
    Ec2RevertSizeParameters,
]


# ------------------------------------------------------------------------------
# 매핑 — runbook_id로만 모델을 고른다(smart-union 오매칭 방지)
# ------------------------------------------------------------------------------

CANDIDATE_PARAMETER_MODELS: Mapping[RunbookId, type[BaseModel]] = {
    RunbookId.RUNBOOK_EC2_ISOLATE: Ec2IsolateCandidateParameters,
    RunbookId.RUNBOOK_NACL_ADD_DENY: NaclAddDenyCandidateParameters,
    RunbookId.RUNBOOK_NACL_RESTORE: NaclRestoreCandidateParameters,
    RunbookId.RUNBOOK_SG_DELETE_ISOLATED: SgDeleteIsolatedCandidateParameters,
    RunbookId.RUNBOOK_EC2_RIGHTSIZING: Ec2RightsizingCandidateParameters,
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING: Ec2EnableAutoscalingCandidateParameters,
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED: EbsDeleteUnattachedCandidateParameters,
}

PRECHECK_PARAMETER_MODELS: Mapping[RunbookId, type[BaseModel]] = {
    RunbookId.RUNBOOK_EC2_ISOLATE: Ec2IsolateParameters,
    RunbookId.RUNBOOK_NACL_ADD_DENY: NaclAddDenyParameters,
    RunbookId.RUNBOOK_NACL_RESTORE: NaclRestoreParameters,
    RunbookId.RUNBOOK_SG_DELETE_ISOLATED: SgDeleteIsolatedParameters,
    RunbookId.RUNBOOK_EC2_RIGHTSIZING: Ec2RightsizingParameters,
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING: Ec2EnableAutoscalingParameters,
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED: EbsDeleteUnattachedParameters,
    RunbookId.RUNBOOK_EC2_UNISOLATE: Ec2UnisolateParameters,
    RunbookId.RUNBOOK_SG_RECREATE: SgRecreateParameters,
    RunbookId.RUNBOOK_EC2_REVERT_SIZE: Ec2RevertSizeParameters,
}

# target_arn이 가리키는 자원 ID가 들어갈 키. executor._Spec.primary_param과 같은 값이며,
# 파라미터가 target_arn과 같은 자원을 가리키는지 대조하는 것은 여전히 executor다
# (ADR-0007 §5 ②·③ — Scope Escalation 2차 방어와 리전 일치).
RESOURCE_ID_PARAM: Mapping[RunbookId, str] = {
    RunbookId.RUNBOOK_EC2_ISOLATE: "instance_id",
    RunbookId.RUNBOOK_NACL_ADD_DENY: "network_acl_id",
    RunbookId.RUNBOOK_NACL_RESTORE: "network_acl_id",
    RunbookId.RUNBOOK_SG_DELETE_ISOLATED: "group_id",
    RunbookId.RUNBOOK_EC2_RIGHTSIZING: "instance_id",
    RunbookId.RUNBOOK_EC2_ENABLE_AUTOSCALING: "instance_id",
    RunbookId.RUNBOOK_EBS_DELETE_UNATTACHED: "volume_id",
    RunbookId.RUNBOOK_EC2_UNISOLATE: "instance_id",
    RunbookId.RUNBOOK_EC2_REVERT_SIZE: "instance_id",
    # RUNBOOK_SG_RECREATE는 없다 — 복원 대상을 백업 레코드가 가리킨다
}


def bind_candidate_parameters(data: Any) -> Any:
    """dict 입력의 parameters를 runbook_id가 지정한 모델로만 검증한다.

    smart-union은 필드가 없는 모델 셋(빈 후보 모델 3종)을 구분하지 못하므로
    Union에 맡기지 않는다. evidence.py의 bind_evidence_content와 같은 규약이다.

    parameters를 아예 싣지 않은 입력은 빈 dict로 본다 — 빈 모델 3종은 그대로
    통과하고, 값이 필요한 Runbook은 필수 키 누락으로 거절된다.
    """
    if not isinstance(data, dict):
        return data
    try:
        runbook_id = RunbookId(data.get("runbook_id"))
    except (ValueError, TypeError):
        return data  # runbook_id 오류는 필드 검증이 보고한다
    model = CANDIDATE_PARAMETER_MODELS.get(runbook_id)
    if model is None:
        return data  # 롤백 3종 — 후보가 될 수 없다는 판정은 모델 검증기가 한다
    raw = data.get("parameters", {})
    if not isinstance(raw, dict):
        return data
    return {**data, "parameters": model.model_validate(raw)}


def bind_precheck_parameters(data: Any) -> Any:
    """dict 입력의 parameters를 runbook_id가 지정한 **실행 파라미터** 모델로 검증한다.

    bind_candidate_parameters와 같은 규약이고 표만 다르다 — 이쪽은 확정 10종 전부를
    가진 PRECHECK_PARAMETER_MODELS다. 후보가 될 수 없는 롤백 3종(ADR-0004 정책 ②)도
    실행 파라미터 계약은 가지므로, 원복 경로의 가드레일 ①이 이 표로 대조한다.
    """
    if not isinstance(data, dict):
        return data
    try:
        runbook_id = RunbookId(data.get("runbook_id"))
    except (ValueError, TypeError):
        return data  # runbook_id 오류는 필드 검증이 보고한다
    model = PRECHECK_PARAMETER_MODELS.get(runbook_id)
    if model is None:
        return data  # 확정 10종 밖 — 목록 대조는 가드레일 ②가 한다
    raw = data.get("parameters", {})
    if not isinstance(raw, dict):
        return data
    return {**data, "parameters": model.model_validate(raw)}


def build_precheck_parameters(
    runbook_id: RunbookId,
    parameters: CandidateParameters,
    *,
    resource_id: str,
    evidence_ids: Sequence[str],
    target_group_arn: Optional[str] = None,
    isolation_group_id: Optional[str] = None,
    current_instance_type: Optional[str] = None,
) -> RunbookParameters:
    """후보 → precheck(parameters) 변환. 본편 7종만 다룬다. (Issue #154)

    resource_id는 target_arn이 가리키는 자원 ID다. 이 함수가 ARN을 파싱하지 않는
    이유는 파서가 둘이 되면 precheck가 본 자원과 조치·백업이 향하는 자원이 갈릴 수
    있기 때문이다 — 해석은 services/aws/executor.parse_arn 하나로 둔다.

    **evidence_id는 후보 evidence_ids의 첫 항목이다.** precheck 판정 핸들러는 형식
    검사 외에 이 값을 쓰지 않으므로 선택 규칙은 결정적이기만 하면 되고, 첫 항목은
    Graph 출력 순서 그대로라 재현 가능하다.

    조회로 채우는 값은 명시 키워드로만 받는다. 해당 Runbook이 받지 않는 값을 넘기면
    거절한다 — 호출부가 조립을 잘못한 것이고, 조용히 버리면 그대로 실행으로 간다.
    누락은 반환 모델의 필수 키 검증이 잡는다(ValidationError).

    반환 모델은 executor.precheck()에 그대로 넘길 수 있다 — §1의 Mapping 계약으로의
    정규화는 precheck()가 경계에서 한다.
    """
    if runbook_id.value in ROLLBACK_RUNBOOK_IDS:
        raise ValueError(
            "롤백 3종은 AI 후보가 될 수 없어 이 변환 경로를 타지 않습니다(#126 복구 접수 경로)"
        )
    expected = CANDIDATE_PARAMETER_MODELS[runbook_id]
    if not isinstance(parameters, expected):
        raise ValueError(f"{runbook_id.value}의 후보 파라미터는 {expected.__name__}이어야 합니다")
    if not evidence_ids:
        raise ValueError("evidence_ids가 비어 있어 evidence_id를 정할 수 없습니다")

    model = PRECHECK_PARAMETER_MODELS[runbook_id]
    data: dict[str, Any] = dict(parameters.model_dump())
    data[RESOURCE_ID_PARAM[runbook_id]] = resource_id
    data["evidence_id"] = evidence_ids[0]

    for name, value in (
        ("target_group_arn", target_group_arn),
        ("isolation_group_id", isolation_group_id),
        ("current_instance_type", current_instance_type),
    ):
        if value is None:
            continue
        if name not in model.model_fields:
            raise ValueError(f"{runbook_id.value}는 {name}을 받지 않습니다")
        data[name] = value

    return model.model_validate(data)


def build_display_parameters(parameters: CandidateParameters) -> dict[str, str]:
    """후보 파라미터의 화면 표시본. 서버가 만든다 — LLM이 짓지 않는다.

    관제자는 이 값을 보고 승인하고 실행은 typed 파라미터로 나가므로, 둘을 각각
    LLM이 채우면 승인 근거와 실행 내용이 갈릴 수 있다. 대상 자원은 FE가
    target_arn으로 따로 보여주므로 여기에는 넣지 않는다.
    """
    return {key: _display_value(value) for key, value in parameters.model_dump().items()}


def _display_value(value: Any) -> str:
    # bool은 int의 하위 타입이라 dict 조회로 갈라내면 1이 True에 걸린다
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
