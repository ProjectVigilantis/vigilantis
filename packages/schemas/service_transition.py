# ==============================================================================
# [파일 설명]
# MVP 서비스 연결·전환(정적 smoke-http.service) 계약 — 운영자 입력·불변 계획·관측·작업 일지.
#
#   생산: apps/core-api/service_transitions.py(facade) · services/aws/service_transition.py
#   저장: db.models.ServiceTransition.plan(불변) · ServiceTransitionObservation.evidence ·
#         ServiceTransitionOperation(작업 일지 — ActionExecution·ExecutionStep과 별개)
#
# 보장을 섞지 않는다.
#   계획(ServiceTransitionPlan) — 조치 시점의 의도와 원래 경로의 **스냅숏**이다. 현재 상태가 아니다.
#   작업 일지(OperationState)  — AWS가 요청을 받아들였는가(APPLIED)·확실히 안 받았는가(NOT_APPLIED)·
#                                모르는가(PENDING·UNCERTAIN). 받아들임은 서비스 가능을 뜻하지 않는다.
#   관측(ServiceObservation)   — 그 시각에 읽고 요청해 본 사실. verdict는 checks·http에서만 계산한다.
#   ALB 경로 증거는 "그 순간 경로가 ASG 전용 TG로만 갔다(전후 스냅숏) + 실제 ALB 응답"이고, 멤버별
#   증거는 "각 멤버 사설 IP에 직접 보낸 응답"이다. 둘 다 요청 단위 ALB 대상 귀속(접근 로그)이 아니다.
# ==============================================================================

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .runbook_parameters import InstanceId, SecurityGroupId, TargetGroupArn, VpcId

SERVICE_UNIT: Final = "smoke-http.service"
SERVICE_PORT: Final = 80
SERVICE_PATH: Final = "/"
EXPECTED_STATUS: Final = 200
# 이 전환이 만든 TG·검증 리스너의 소유 표지. #446 소유 태그(vigilantis:execution-id)와 함께 단다
TRANSITION_TAG_KEY: Final = "vigilantis:service-transition-id"
PROBE_HEADER: Final = "X-Vigilantis-Probe"
PROBE_TIMEOUT_SECONDS: Final = 5.0
PROBE_MAX_BODY_BYTES: Final = 65536

ServiceCommand = Literal[
    "schema",
    "plan",
    "status",
    "prepare",
    "verify",
    "cutover",
    "return",
    "cleanup-plan",
    "cleanup",
]
# AWS를 바꾸는 명령 — CLI는 --apply가 있을 때만 부른다. 백엔드는 플래그가 아니라 저장된 관문으로 막는다
MUTATING_COMMANDS: Final[frozenset[str]] = frozenset(
    {"prepare", "cutover", "return", "cleanup"}
)

OperationState = Literal["PENDING", "APPLIED", "NOT_APPLIED", "UNCERTAIN"]
# 불확실 변경(PENDING·UNCERTAIN)의 읽기 전용 대조. 원래 호출 결과(state)는 바꾸지 않는다.
#   EFFECT_OBSERVED     — 의도한 효과가 지금 확인됐다(생성·부착·경로는 존재, 삭제·분리는 부재). 관문을 푼다. 최종.
#   EFFECT_NOT_OBSERVED — 지금은 효과가 보이지 않는다. 요청이 받아들여지지 않았다는 증거가 아니다 —
#                         관문을 풀지 않고 재전송도 허용하지 않는다. 뒤의 읽기가 EFFECT_OBSERVED로 바꿀 수 있다.
ReconciledState = Literal["EFFECT_OBSERVED", "EFFECT_NOT_OBSERVED"]
ObservationKind = Literal["PLAN", "VERIFY", "CUTOVER", "RETURN", "RECONCILE", "CLEANUP"]
ObservationVerdict = Literal["PASSED", "FAILED", "UNKNOWN"]

AccountId = Annotated[str, StringConstraints(pattern=r"^\d{12}$")]
Region = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_LB_ARN = re.compile(
    r"^arn:aws:elasticloadbalancing:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):"
    r"loadbalancer/(?P<resource>app/[A-Za-z0-9-]{1,32}/[0-9a-f]+)$"
)
_LISTENER_ARN = re.compile(
    r"^arn:aws:elasticloadbalancing:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):"
    r"listener/(?P<resource>app/[A-Za-z0-9-]{1,32}/[0-9a-f]+)/[0-9a-f]+$"
)
LoadBalancerArn = Annotated[str, StringConstraints(pattern=_LB_ARN.pattern)]
ListenerArn = Annotated[str, StringConstraints(pattern=_LISTENER_ARN.pattern)]
Port = Annotated[int, Field(ge=1, le=65535)]
_Text = Annotated[str, StringConstraints(min_length=1, max_length=1024)]

_Input = ConfigDict(extra="forbid", frozen=True)


def load_balancer_resource(arn: str) -> str:
    """`app/<name>/<id>` — ALB ENI 설명(`ELB app/<name>/<id>`)과 리스너 ARN이 같은 조각을 쓴다."""
    match = _LB_ARN.match(arn)
    if match is None:
        raise ValueError("ALB ARN이 아닙니다")
    return match.group("resource")


def transition_target_group_name(transition_id: str) -> str:
    """전환이 만드는 TG 이름 — 전환으로 정해진다(TG 이름 32자 상한). 응답이 유실돼도 이 이름으로 찾는다."""
    return f"vgl-svc-{uuid.UUID(transition_id).hex[:20]}"


class RuntimeFacts(BaseModel):
    """운영자가 공급하는 실행 시점 값 — **운영자 공급값이지 실시간 조회 증거가 아니다.**

    코드는 단가·한도·상한·RTO를 지어내지 않는다(가격 API를 부르지 않는다). 없으면 '없음'으로 보고하고,
    관문이 필요한 명령(prepare·cutover)에서는 차단 사유가 된다 — 0으로 채우지 않는다.

    vcpu_quota·rto_minutes는 **공급 여부만** 확인한다. 공급됐다는 것이 용량 검증도, 실제 Service Quotas
    조회도, RTO 측정도 아니다 — 운영자가 적어 준 값을 그대로 보여 줄 뿐이다.
    """

    model_config = _Input

    # #446이 캡처한 인스턴스 타입(plan.source.instance_type) 1대의 시간당 단가 — ASG 멤버는 이 타입으로 뜬다
    captured_instance_type_hourly_usd: Decimal | None = Field(default=None, ge=0)
    # 복제 원본 인스턴스의 시간당 단가. 없으면 원본의 현재 타입이 캡처 타입과 같을 때만 위 단가를 쓴다
    source_instance_hourly_usd: Decimal | None = Field(default=None, ge=0)
    # 복제 원본 + ASG 멤버(현재 대수)를 함께 띄우는 동안의 compute 상한(시간당)
    simultaneous_cost_cap_usd_per_hour: Decimal | None = Field(default=None, ge=0)
    vcpu_quota: int | None = Field(default=None, ge=0)
    rto_minutes: int | None = Field(default=None, ge=0)


RUNTIME_FACT_LABEL: Final = "operator_supplied — 실시간 조회 증거 아님"
COST_EXCLUSIONS: Final = "원래 TG 대상(예: web-1·web-2)·탐침·다른 스모크 인스턴스·ALB·EBS·스냅숏 비용은 조회하지 않아 합계에 넣지 않습니다"
# EC2 compute가 과금되는 상태 — stopped·terminated는 compute가 과금되지 않는다
_BILLABLE_STATES = frozenset({"pending", "running", "stopping", "shutting-down"})


def simultaneous_cost(
    facts: RuntimeFacts,
    *,
    captured_instance_type: str,
    source: dict[str, Any] | None,
    asg_members: int | None,
    asg_max_size: int,
) -> dict[str, Any]:
    """동시 운용 compute 관문 — (복제 원본 1대 + 현재 ASG 멤버) × 운영자 단가 ≤ 운영자 상한.

    source는 원본 인스턴스의 관측({"instance_id","state","instance_type"}, 조회 불가면 None)이다.
    원래 TG 대상은 세지 않는다 — 복제 원본은 TG 대상이 아닐 수 있고(idle-dev), TG 대상은 이 합계 밖이다.
    max_size 대수의 비용은 따로 보여 줄 뿐 관문이 아니다(정책이 대수를 늘릴 수 있다는 정보).
    단가·관측이 없으면 합계는 None(모름)이고 차단 사유가 된다.
    """
    blockers: list[str] = []
    rate = facts.captured_instance_type_hourly_usd
    cap = facts.simultaneous_cost_cap_usd_per_hour
    if rate is None:
        blockers.append(
            "운영자 공급값 없음: runtime_facts.captured_instance_type_hourly_usd"
        )
    if cap is None:
        blockers.append(
            "운영자 공급값 없음: runtime_facts.simultaneous_cost_cap_usd_per_hour"
        )
    if facts.vcpu_quota is None:
        blockers.append("운영자 공급값 없음: runtime_facts.vcpu_quota")
    if facts.rto_minutes is None:
        blockers.append("운영자 공급값 없음: runtime_facts.rto_minutes")
    source_cost: Decimal | None = None
    state = (source or {}).get("state")
    if state is None:
        blockers.append("복제 원본 인스턴스 상태를 관측하지 못했습니다")
    elif state not in _BILLABLE_STATES:
        source_cost = Decimal(0)
    elif facts.source_instance_hourly_usd is not None:
        source_cost = facts.source_instance_hourly_usd
    elif source.get("instance_type") == captured_instance_type:
        source_cost = rate
    else:
        blockers.append(
            f"복제 원본 타입 {source.get('instance_type')}이 캡처 타입 {captured_instance_type}과 달라"
            " runtime_facts.source_instance_hourly_usd가 필요합니다"
        )
    if asg_members is None:
        blockers.append("ASG 멤버 수를 관측하지 못했습니다")
    asg_cost = None if rate is None or asg_members is None else rate * asg_members
    subtotal = (
        None if source_cost is None or asg_cost is None else source_cost + asg_cost
    )
    if subtotal is not None and cap is not None and subtotal > cap:
        blockers.append(
            f"동시 운용 compute {subtotal}USD/h가 운영자 상한 {cap}USD/h를 넘습니다"
        )

    def money(value):
        return None if value is None else str(value)

    return {
        "label": RUNTIME_FACT_LABEL,
        "source_instance": source,
        "source_compute_usd_per_hour": money(source_cost),
        "observed_asg_members": asg_members,
        "asg_compute_usd_per_hour": money(asg_cost),
        "simultaneous_compute_usd_per_hour": money(subtotal),
        "cap_usd_per_hour": money(cap),
        "asg_max_size": asg_max_size,
        "asg_at_max_size_usd_per_hour": money(
            None if rate is None else rate * asg_max_size
        ),
        "asg_at_max_size_note": "정보용 — 관문은 현재 멤버 수로만 비교합니다(앞으로의 예산 집행이 아닙니다)",
        "excluded": COST_EXCLUSIONS,
        "vcpu_quota": facts.vcpu_quota,
        "rto_minutes": facts.rto_minutes,
        "missing": [
            name for name, value in facts.model_dump().items() if value is None
        ],
        "blockers": blockers,
    }


_Body = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class ServiceTransitionPlanOptions(BaseModel):
    """`plan` 입력. 운영자가 지정한 계정·리전·VPC·기존 internal ALB·운영 리스너·검증 포트·사전 준비된 탐침.

    응답 기대값은 둘로 나눈다 — 복제본(ASG 멤버)의 본문과, 원래 경로 대상별 본문. 원래 경로는
    복제 원본(예: idle-dev)과 다른 인스턴스(web-1·web-2)일 수 있고 각자 다른 페이지를 낸다.
    어느 쪽도 검사 대상의 출력에서 끌어오지 않는다.
    """

    model_config = _Input

    account_id: AccountId
    region: Region
    vpc_id: VpcId
    load_balancer_arn: LoadBalancerArn
    listener_arn: ListenerArn
    validation_listener_port: Port
    probe_instance_id: InstanceId
    probe_security_group_id: SecurityGroupId
    # ASG 멤버의 smoke-http.service가 / 에 내야 할 본문 그대로(예: "vigilantis-smoke-idle-dev\n")
    clone_expected_body: _Body
    # 원래 TG 대상별 본문 그대로. 키는 계획 시점의 원래 TG 대상과 정확히 같아야 한다
    original_target_expected_bodies: dict[InstanceId, _Body] = Field(min_length=1)
    runtime_facts: RuntimeFacts = RuntimeFacts()
    # 같은 #446 실행의 앞선 시도가 있으면 그 leaf 시도를 명시로 대체한다(있을 때만, 없으면 안 된다).
    # 앞선 계획은 고치지 않는다 — 새 시도가 그것을 가리킬 뿐이다
    replaces_transition_id: (
        Annotated[str, StringConstraints(min_length=36, max_length=36)] | None
    ) = None

    @model_validator(mode="after")
    def _consistent(self):
        lb = _LB_ARN.match(self.load_balancer_arn)
        listener = _LISTENER_ARN.match(self.listener_arn)
        if listener.group("resource") != lb.group("resource"):
            raise ValueError("listener_arn이 load_balancer_arn의 리스너가 아닙니다")
        for match in (lb, listener):
            if (match.group("region"), match.group("account")) != (
                self.region,
                self.account_id,
            ):
                raise ValueError("ALB·리스너 ARN의 리전·계정이 지정 값과 다릅니다")
        if self.validation_listener_port == SERVICE_PORT:
            raise ValueError("검증 리스너 포트는 운영 포트(80)와 달라야 합니다")
        return self


class NoOptions(BaseModel):
    model_config = _Input


# 운영자의 보존 결정(셋 중 하나). 시간에 따른 자동 만료는 없다.
#   delete_execution_resources      — 서비스 자원과 #446 자원 전부
#   retain_launch_image             — 시작 템플릿·AMI·스냅숏은 보존, 나머지 정리
#   retain_execution_infrastructure — 시도의 검증 리스너·TG·부착만 정리. ASG·인스턴스·볼륨·시작 템플릿·
#                                     AMI·스냅숏은 전부 보존한다(과금이 계속된다) — 같은 인프라로 새 시도를 세울 때
RetentionDecision = Literal[
    "delete_execution_resources",
    "retain_launch_image",
    "retain_execution_infrastructure",
]
RETAINED_BILLING_WARNING: Final = (
    "보존한 자원은 계속 과금됩니다(운영자 보존 결정) — 단가·합계는 조회하지 않습니다"
)


class CleanupPlanOptions(BaseModel):
    """`cleanup-plan` 입력 — 보존 결정이 없으면 재고는 보이되 정리는 차단 사유가 된다."""

    model_config = _Input

    retention_decision: RetentionDecision | None = None


class CleanupOptions(BaseModel):
    """`cleanup` 입력 — cleanup-plan이 낸 매니페스트를 그대로 승인한다. 둘 다 현재 재고와 정확히 같아야 한다."""

    model_config = _Input

    retention_decision: RetentionDecision
    manifest_sha256: Sha256Hex
    resource_ids: tuple[
        Annotated[str, StringConstraints(min_length=1, max_length=512)], ...
    ] = Field(min_length=1)


class RouteMember(BaseModel):
    """원래 TG 대상 1건 — 계획 시점 관측. 격리 여부는 그때의 AWS SG·DB 사실이다."""

    model_config = _Input

    instance_id: InstanceId
    port: Port
    health_state: _Text
    quarantined: bool
    quarantine_basis: tuple[_Text, ...] = ()
    # 운영자가 공급한 이 대상의 기대 본문 — 복귀 전 직접 응답과 복귀 후 ALB 응답의 기준
    expected_body: _Body


class OriginalRoute(BaseModel):
    """운영 리스너의 **실제** 기본 동작 스냅숏(단일 TG forward만 지원). 복원은 이 값으로만 한다."""

    model_config = _Input

    listener_arn: ListenerArn
    listener_port: Literal[80]
    listener_protocol: Literal["HTTP"]
    target_group_arn: TargetGroupArn
    members: tuple[RouteMember, ...]
    captured_at: datetime

    def default_actions(self) -> list[dict[str, str]]:
        return forward_action(self.target_group_arn)


def forward_action(target_group_arn: str) -> list[dict[str, str]]:
    return [{"Type": "forward", "TargetGroupArn": target_group_arn}]


class ProbeClient(BaseModel):
    """사전 준비된 VPC 안 탐침 인스턴스. 이 도구는 탐침 호스트·네트워크 접근을 만들지 않는다."""

    model_config = _Input

    instance_id: InstanceId
    security_group_id: SecurityGroupId
    private_ip: Annotated[str, StringConstraints(min_length=7, max_length=45)]


class ServiceTransitionPlan(BaseModel):
    """전환 1건의 불변 의도 + 원래 경로 스냅숏. #446 실행 계획은 복제하지 않고 execution_id로 다시 읽는다."""

    model_config = _Input

    transition_id: Annotated[str, StringConstraints(min_length=36, max_length=36)]
    execution_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    planned_at: datetime
    account_id: AccountId
    region: Region
    vpc_id: VpcId
    service_unit: Literal["smoke-http.service"]
    service_port: Literal[80]
    service_path: Literal["/"]
    expected_status: Literal[200]
    clone_expected_body: _Body
    load_balancer_arn: LoadBalancerArn
    original_route: OriginalRoute
    validation_listener_port: Port
    target_group_name: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    probe: ProbeClient
    runtime_facts: RuntimeFacts

    @model_validator(mode="after")
    def _consistent(self):
        if self.target_group_name != transition_target_group_name(self.transition_id):
            raise ValueError("target_group_name이 전환 ID로 정해지는 이름과 다릅니다")
        lb = _LB_ARN.match(self.load_balancer_arn)
        listener = _LISTENER_ARN.match(self.original_route.listener_arn)
        if listener.group("resource") != lb.group("resource"):
            raise ValueError("운영 리스너가 계획한 ALB의 것이 아닙니다")
        if self.validation_listener_port == self.original_route.listener_port:
            raise ValueError("검증 리스너 포트가 운영 리스너 포트와 같습니다")
        return self


# ------------------------------------------------------------------ 관측
class CheckResult(BaseModel):
    model_config = _Input

    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    passed: bool
    detail: _Text


class TargetState(BaseModel):
    model_config = _Input

    instance_id: str
    port: int | None
    state: str
    reason: str | None = None


class RouteSnapshot(BaseModel):
    """리스너 기본 동작과 그 TG 대상 — 관측 시각의 사실이다(현재 상태로 재사용하지 않는다)."""

    model_config = _Input

    listener_arn: str
    target_group_arn: str | None
    unsupported: str | None
    non_default_rules: int
    targets: tuple[TargetState, ...]
    observed_at: datetime

    def same_route(self, other: RouteSnapshot) -> bool:
        def key(snapshot: RouteSnapshot):
            return (
                snapshot.target_group_arn,
                snapshot.unsupported,
                snapshot.non_default_rules,
                sorted((t.instance_id, t.port, t.state) for t in snapshot.targets),
            )

        return key(self) == key(other)


class HttpProbeEvidence(BaseModel):
    """GET 1건의 관측. passed는 응답 사실에서만 계산한다 — 호출자가 성공 여부를 넘기지 않는다."""

    model_config = _Input

    path_kind: Literal[
        "alb_validation_listener", "alb_production_listener", "member_direct"
    ]
    instance_id: str | None
    host: str
    port: int
    path: str
    request_id: str
    status: int | None
    body_bytes: int | None
    body_sha256: str | None
    # 계획의 기대 본문(들)과 바이트 그대로 같은가. 기대값은 계획에서만 온다
    expected_sha256: tuple[str, ...]
    body_matches: bool
    location: str | None
    peer_ip: str | None
    peer_allowed: tuple[str, ...]
    error: str | None
    observed_at: datetime

    @property
    def passed(self) -> bool:
        return (
            self.error is None
            and self.status == EXPECTED_STATUS
            and self.location is None
            and self.body_matches
            and self.peer_ip is not None
            and self.peer_ip in self.peer_allowed
        )


class ServiceObservation(BaseModel):
    """관측 1건. verdict는 checks·http의 사실에서 계산된 값만 받는다(계산식: compute_verdict)."""

    model_config = _Input

    kind: ObservationKind
    verdict: ObservationVerdict
    observed_at: datetime
    summary: _Text
    checks: tuple[CheckResult, ...] = ()
    http: tuple[HttpProbeEvidence, ...] = ()
    routes: tuple[RouteSnapshot, ...] = ()
    facts: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _verdict_from_evidence(self):
        if (
            self.verdict == "PASSED"
            and compute_verdict(self.checks, self.http) != "PASSED"
        ):
            raise ValueError("PASSED는 모든 확인과 HTTP 증거가 통과했을 때만입니다")
        return self


def compute_verdict(checks, http) -> ObservationVerdict:
    if not checks:
        return "UNKNOWN"
    return (
        "PASSED"
        if all(c.passed for c in checks) and all(h.passed for h in http)
        else "FAILED"
    )


# ------------------------------------------------------------------ 정리
CleanupAction = Literal[
    "delete_listener",
    "detach_target_group",
    "delete_target_group",
    "delete_auto_scaling_group",
    "await_instance_termination",
    "delete_volume",
    "delete_launch_template",
    "deregister_image",
    "delete_snapshot",
]


class CleanupItem(BaseModel):
    model_config = _Input

    order: int = Field(ge=1)
    kind: Literal[
        "validation_listener",
        "target_group_attachment",
        "target_group",
        "auto_scaling_group",
        "instance",
        "volume",
        "launch_template",
        "image",
        "snapshot",
    ]
    resource_id: _Text
    action: CleanupAction
    # 시도가 만든 서비스 자원(검증 리스너·TG·부착)은 그 시도, #446 인프라는 None
    transition_id: str | None = None


class CleanupManifest(BaseModel):
    """실행 소유 자원의 정확한 목록과 처분 순서. 승인은 이 목록의 digest로 한다."""

    model_config = _Input

    execution_id: str
    transition_id: str | None
    items: tuple[CleanupItem, ...]

    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def resource_ids(self) -> tuple[str, ...]:
        return tuple(item.resource_id for item in self.items)
