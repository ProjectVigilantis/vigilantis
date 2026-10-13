# ==============================================================================
# [파일 설명]
# MVP 서비스 연결·전환의 AWS 조회·변경·HTTP 탐침입니다. DB를 모른다 — 일지·트랜잭션은
# apps/core-api/service_transitions.py(facade)가 소유한다.
#
# 클라이언트는 호출부가 명시 세션으로 만든 것을 받는다(키: ec2·elbv2·asg·sts, 선택 http).
# #446 소유·구성 판정은 services/aws/autoscaling.py의 도우미를 그대로 쓴다.
#
# 보장을 넷으로 나눈다.
#   ① 조회(observe_*·inspect_*)   — 읽기만 한다. 실패는 예외로 올라가 "판정 불가"가 된다.
#   ② 요청 접수(perform)          — AWS가 2xx로 받았다(APPLIED). 서비스 가능을 뜻하지 않는다.
#                                  4xx·스로틀링은 NOT_APPLIED, 응답 유실·5xx는 UNCERTAIN — 되묻지도
#                                  다시 보내지도 않는다.
#   ③ 서비스 증거(inspect_service) — ASG 전용 경로의 전후 스냅숏 + 실제 ALB 응답, 그리고 멤버별
#                                  사설 IP 직접 응답. **요청 단위 ALB 대상 귀속(접근 로그)이 아니다.**
#   ④ 미검증 — DNS·TLS·외부 공개 경로, 운영 리스너 변경의 원자성(AWS CAS 없음 — 전후 대조만 한다).
#
# 탐침 전제(사전 준비 — 이 도구는 만들지 않는다)
#   - 지정 탐침 인스턴스(VPC 안·running·탐침 SG 부착)에서 이 프로세스가 돈다. IMDSv2로 읽은
#     인스턴스 ID가 계획의 탐침 ID와 같아야 HTTP 증거를 남긴다(SSH·SSM 에이전트를 가정하지 않는다).
#   - ALB SG는 검증 포트를 **탐침 SG에서만** 받는다(CIDR·접두 목록·다른 SG 없음).
#   - ASG 멤버의 주 ENI SG는 80/tcp를 탐침 SG에서 받는다(멤버 직접 응답용).
#   - HTTP 클라이언트는 프록시를 쓰지 않고 리다이렉트를 따르지 않는다(DirectHttpProbe).
# ==============================================================================

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPException
from typing import Any, Literal, Protocol

from botocore.exceptions import BotoCoreError, ClientError, PaginationError
from schemas.asset_roles import ROLE_ISOLATION, ROLE_TAG_KEY
from schemas.autoscaling import EXECUTION_TAG_KEY, AutoscalingExecutionPlan
from schemas.executions import ExecutionEffect
from schemas.precheck import PrecheckReasonCode
from schemas.service_transition import (
    PROBE_HEADER,
    PROBE_MAX_BODY_BYTES,
    PROBE_TIMEOUT_SECONDS,
    RETAINED_BILLING_WARNING,
    SERVICE_PATH,
    SERVICE_PORT,
    TRANSITION_TAG_KEY,
    CheckResult,
    CleanupItem,
    CleanupManifest,
    HttpProbeEvidence,
    RouteSnapshot,
    ServiceTransitionPlan,
    ServiceTransitionPlanOptions,
    TargetState,
    forward_action,
    load_balancer_resource,
)

from .autoscaling import (
    _group_problem,
    _groups_named,
    _images_named,
    _launch_templates_named,
    _owned,
    _pages,
    _tags,
)
from .errors import aws_error_code, reason_code_for
from .executor import _effect_for, _summarize, escape_filter_value

R = PrecheckReasonCode
REQUIRED_CLIENT_KEYS: tuple[str, ...] = ("ec2", "elbv2", "asg", "sts")

# 이 모듈이 부르는 AWS 변경 작업 전부 — **관리자 운영 CLI 전용**(scripts/provision_smoke_aws.py service).
# 앱 IAM 정책·그 거울(_code_operations)의 대상이 아니다 — 앱 키에 이 권한을 주지 않는다
_OP_CREATE_TARGET_GROUP = "elbv2.create_target_group"
_OP_CREATE_LISTENER = "elbv2.create_listener"
_OP_MODIFY_LISTENER = "elbv2.modify_listener"
_OP_DELETE_LISTENER = "elbv2.delete_listener"
_OP_DELETE_TARGET_GROUP = "elbv2.delete_target_group"
_OP_ATTACH_TARGET_GROUPS = "autoscaling.attach_load_balancer_target_groups"
_OP_DETACH_TARGET_GROUPS = "autoscaling.detach_load_balancer_target_groups"
_OP_DELETE_ASG = "autoscaling.delete_auto_scaling_group"
_OP_DELETE_VOLUME = "ec2.delete_volume"
_OP_DELETE_LAUNCH_TEMPLATE = "ec2.delete_launch_template"
_OP_DEREGISTER_IMAGE = "ec2.deregister_image"
_OP_DELETE_SNAPSHOT = "ec2.delete_snapshot"


def _now() -> datetime:
    return datetime.now(UTC)


def check(name: str, passed: bool, detail: str) -> CheckResult:
    return CheckResult(name=name, passed=bool(passed), detail=detail[:1024] or name)


# ------------------------------------------------------------------ 클라이언트
class HttpProbe(Protocol):
    def origin_instance_id(self) -> str: ...

    def get(
        self,
        *,
        host: str,
        port: int,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
        max_body_bytes: int,
    ) -> Mapping[str, Any]: ...


class DirectHttpProbe:
    """기본 탐침 — 이 프로세스의 호스트에서 GET 1건. http.client는 환경 프록시를 읽지 않고
    리다이렉트를 따르지 않는다. peer_ip는 실제로 연결된 상대 주소다."""

    imds_host = "169.254.169.254"

    def get(self, *, host, port, path, headers, timeout, max_body_bytes):
        connection = HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request("GET", path, headers=dict(headers))
            peer = connection.sock.getpeername()[0] if connection.sock else None
            response = connection.getresponse()
            body = response.read(max_body_bytes + 1)
            return {
                "status": response.status,
                "headers": dict(response.getheaders()),
                "body": body,
                "peer_ip": peer,
            }
        finally:
            connection.close()

    def origin_instance_id(self) -> str:
        """IMDSv2 — 링크 로컬이라 그 인스턴스 안에서만 답한다."""
        connection = HTTPConnection(self.imds_host, 80, timeout=2)
        try:
            connection.request(
                "PUT",
                "/latest/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            )
            response = connection.getresponse()
            token = response.read(256).decode()
            if response.status != 200:
                raise OSError(f"IMDS 토큰 응답 {response.status}")
            connection.request(
                "GET",
                "/latest/meta-data/instance-id",
                headers={"X-aws-ec2-metadata-token": token},
            )
            response = connection.getresponse()
            value = response.read(64).decode().strip()
            if response.status != 200:
                raise OSError(f"IMDS instance-id 응답 {response.status}")
            return value
        finally:
            connection.close()


@dataclass(frozen=True)
class Clients:
    ec2: Any
    elbv2: Any
    asg: Any
    sts: Any
    http: Any


def clients_from(mapping: Mapping[str, Any]) -> Clients:
    """필수 키가 없으면 KeyError. http가 없으면 DirectHttpProbe."""
    missing = [key for key in REQUIRED_CLIENT_KEYS if mapping.get(key) is None]
    if missing:
        raise KeyError(", ".join(missing))
    return Clients(
        ec2=mapping["ec2"],
        elbv2=mapping["elbv2"],
        asg=mapping["asg"],
        sts=mapping["sts"],
        http=mapping.get("http") or DirectHttpProbe(),
    )


def session_checks(c: Clients, *, account_id: str, region: str) -> list[CheckResult]:
    account = c.sts.get_caller_identity().get("Account")
    regions = {
        name: getattr(getattr(getattr(c, name), "meta", None), "region_name", None)
        for name in ("ec2", "elbv2", "asg")
    }
    return [
        check(
            "session_account",
            account == account_id,
            f"호출 계정 {account} / 계획 {account_id}",
        ),
        check(
            "session_region",
            all(value == region for value in regions.values()),
            f"클라이언트 리전 {regions} / 계획 {region}",
        ),
    ]


# ------------------------------------------------------------------ 조회 도우미
def _elb_pages(operation: Callable[..., Any], result_key: str, **params: Any) -> list:
    items: list = []
    seen: set[str] = set()
    while True:
        page = operation(**params)
        items.extend(page.get(result_key) or ())
        marker = page.get("NextMarker")
        if not marker:
            return items
        if marker in seen:
            raise PaginationError(
                message=f"{result_key} 조회에서 NextMarker가 반복됐습니다"
            )
        seen.add(marker)
        params = {**params, "Marker": marker}


def _not_found(exc: ClientError) -> bool:
    return reason_code_for(exc) is R.PRECHECK_TARGET_NOT_FOUND


def _instances(ec2: Any, instance_ids) -> dict[str, dict]:
    ids = sorted({i for i in instance_ids if i})
    if not ids:
        return {}
    reservations = _pages(ec2.describe_instances, "Reservations", InstanceIds=ids)
    return {
        item["InstanceId"]: item
        for reservation in reservations
        for item in reservation.get("Instances") or ()
        if item.get("InstanceId")
    }


def _tagged_instances(ec2: Any, execution_id: str) -> dict[str, dict]:
    reservations = _pages(
        ec2.describe_instances,
        "Reservations",
        Filters=[
            {
                "Name": f"tag:{EXECUTION_TAG_KEY}",
                "Values": [escape_filter_value(execution_id)],
            }
        ],
    )
    return {
        item["InstanceId"]: item
        for reservation in reservations
        for item in reservation.get("Instances") or ()
        if _tags(item.get("Tags")).get(EXECUTION_TAG_KEY) == execution_id
    }


def _instance_group_ids(instance: Mapping[str, Any]) -> list[str]:
    ids = {
        group.get("GroupId")
        for interface in instance.get("NetworkInterfaces") or ()
        for group in interface.get("Groups") or ()
    }
    ids |= {group.get("GroupId") for group in instance.get("SecurityGroups") or ()}
    return sorted(i for i in ids if i)


def _primary_group_ids(instance: Mapping[str, Any]) -> list[str]:
    for interface in instance.get("NetworkInterfaces") or ():
        if (interface.get("Attachment") or {}).get("DeviceIndex") == 0:
            return sorted(
                g.get("GroupId")
                for g in interface.get("Groups") or ()
                if g.get("GroupId")
            )
    return []


def _security_groups(ec2: Any, group_ids) -> dict[str, dict]:
    ids = sorted({i for i in group_ids if i})
    if not ids:
        return {}
    groups = _pages(ec2.describe_security_groups, "SecurityGroups", GroupIds=ids)
    return {group["GroupId"]: group for group in groups}


def quarantine_basis(
    instance: Mapping[str, Any], groups: Mapping[str, Mapping]
) -> list[str]:
    """현재 ENI에 붙은 격리 SG(자리 태그 vigilantis:role=isolation). 이름·규칙 수로 추정하지 않는다."""
    return [
        f"격리 SG {group_id}"
        for group_id in _instance_group_ids(instance)
        if _tags((groups.get(group_id) or {}).get("Tags")).get(ROLE_TAG_KEY)
        == ROLE_ISOLATION
    ]


def _covers(permission: Mapping[str, Any], port: int) -> bool:
    protocol = str(permission.get("IpProtocol"))
    if protocol == "-1":
        return True
    if protocol not in ("tcp", "6"):
        return False
    low, high = permission.get("FromPort"), permission.get("ToPort")
    return isinstance(low, int) and isinstance(high, int) and low <= port <= high


def _source_groups(permission: Mapping[str, Any]) -> set[str]:
    return {pair.get("GroupId") for pair in permission.get("UserIdGroupPairs") or ()}


def _broad(permission: Mapping[str, Any]) -> bool:
    return bool(
        permission.get("IpRanges")
        or permission.get("Ipv6Ranges")
        or permission.get("PrefixListIds")
    )


def validation_port_restriction(
    groups: list[Mapping[str, Any]], port: int, probe_group_id: str
) -> CheckResult:
    """ALB SG가 검증 포트를 탐침 SG에서만 받는가. 헤더가 아니라 네트워크로 가둔다."""
    covering = [
        p for g in groups for p in g.get("IpPermissions") or () if _covers(p, port)
    ]
    if not covering:
        return check(
            "validation_port_restricted",
            False,
            f"ALB SG에 {port}/tcp 수신 규칙이 없습니다 — 탐침 SG {probe_group_id} 출발 규칙을 사전 준비해야 합니다",
        )
    loose = [p for p in covering if _broad(p) or _source_groups(p) != {probe_group_id}]
    if loose:
        return check(
            "validation_port_restricted",
            False,
            f"ALB SG가 {port}/tcp를 탐침 SG {probe_group_id} 밖(CIDR·접두 목록·다른 SG)에도 엽니다",
        )
    return check(
        "validation_port_restricted",
        True,
        f"{port}/tcp는 탐침 SG {probe_group_id}에서만",
    )


def member_probe_access(
    groups: Mapping[str, Mapping], group_ids, probe_group_id: str
) -> bool:
    return any(
        _covers(p, SERVICE_PORT) and probe_group_id in _source_groups(p)
        for group_id in group_ids
        for p in (groups.get(group_id) or {}).get("IpPermissions") or ()
    )


def forward_target(actions: Any) -> tuple[str | None, str | None]:
    """기본 동작이 단일 TG forward면 (TG ARN, None). 그 밖의 형태는 (None, 거절 사유)."""
    actions = [a for a in actions or () if isinstance(a, Mapping)]
    if len(actions) != 1:
        return None, f"기본 동작 {len(actions)}개 — 단일 forward만 지원합니다"
    action = actions[0]
    if action.get("Type") != "forward":
        return None, f"기본 동작 {action.get('Type')} — forward만 지원합니다"
    target = action.get("TargetGroupArn")
    config = action.get("ForwardConfig")
    if config:
        groups = config.get("TargetGroups") or []
        if len(groups) != 1:
            return None, f"가중 forward(TG {len(groups)}개)는 지원하지 않습니다"
        if target and groups[0].get("TargetGroupArn") != target:
            return None, "TargetGroupArn과 ForwardConfig의 TG가 다릅니다"
        target = groups[0].get("TargetGroupArn")
        if (config.get("TargetGroupStickinessConfig") or {}).get("Enabled"):
            return None, "TG 고정(stickiness) forward는 지원하지 않습니다"
    if not target:
        return None, "forward 대상 TG가 없습니다"
    return target, None


def _listener(elbv2: Any, listener_arn: str) -> dict | None:
    try:
        listeners = (
            elbv2.describe_listeners(ListenerArns=[listener_arn]).get("Listeners") or []
        )
    except ClientError as exc:
        if _not_found(exc):
            return None
        raise
    return next(
        (item for item in listeners if item.get("ListenerArn") == listener_arn), None
    )


def _load_balancer_listeners(elbv2: Any, load_balancer_arn: str) -> list[dict]:
    """ALB의 리스너 전부(페이지 끝까지). ALB가 없다는 확정 응답(LoadBalancerNotFound)이면 빈 목록 —
    권한 거부·스로틀링·5xx 같은 다른 조회 실패는 부재가 아니라 그대로 올린다."""
    try:
        return _elb_pages(
            elbv2.describe_listeners, "Listeners", LoadBalancerArn=load_balancer_arn
        )
    except ClientError as exc:
        if _not_found(exc):
            return []
        raise


def _listener_on_port(elbv2: Any, load_balancer_arn: str, port: int) -> dict | None:
    listeners = _load_balancer_listeners(elbv2, load_balancer_arn)
    return next((item for item in listeners if item.get("Port") == port), None)


def _non_default_rules(elbv2: Any, listener_arn: str) -> list[dict]:
    rules = _elb_pages(elbv2.describe_rules, "Rules", ListenerArn=listener_arn)
    return [rule for rule in rules if not rule.get("IsDefault")]


def _target_states(elbv2: Any, target_group_arn: str) -> tuple[TargetState, ...]:
    """TG의 등록 대상. TG가 없다는 확정 응답(TargetGroupNotFound)이면 빈 튜플 — 다른 조회 실패는 올린다."""
    try:
        descriptions = (
            elbv2.describe_target_health(TargetGroupArn=target_group_arn).get(
                "TargetHealthDescriptions"
            )
            or []
        )
    except ClientError as exc:
        if _not_found(exc):
            return ()
        raise
    states = []
    for description in descriptions:
        health = description.get("TargetHealth") or {}
        if health.get("Reason") == "Target.NotRegistered":
            continue
        target = description.get("Target") or {}
        states.append(
            TargetState(
                instance_id=str(target.get("Id")),
                port=target.get("Port"),
                state=str(health.get("State")),
                reason=health.get("Reason"),
            )
        )
    return tuple(sorted(states, key=lambda s: (s.instance_id, s.port or 0)))


def route_snapshot(elbv2: Any, listener_arn: str) -> RouteSnapshot:
    listener = _listener(elbv2, listener_arn)
    if listener is None:
        return RouteSnapshot(
            listener_arn=listener_arn,
            target_group_arn=None,
            unsupported="리스너 없음",
            non_default_rules=0,
            targets=(),
            observed_at=_now(),
        )
    target, unsupported = forward_target(listener.get("DefaultActions"))
    return RouteSnapshot(
        listener_arn=listener_arn,
        target_group_arn=target,
        unsupported=unsupported,
        non_default_rules=len(_non_default_rules(elbv2, listener_arn)),
        targets=_target_states(elbv2, target) if target else (),
        observed_at=_now(),
    )


def _target_group_named(elbv2: Any, name: str) -> dict | None:
    try:
        groups = elbv2.describe_target_groups(Names=[name]).get("TargetGroups") or []
    except ClientError as exc:
        if _not_found(exc):
            return None
        raise
    return next((g for g in groups if g.get("TargetGroupName") == name), None)


def _target_group(elbv2: Any, arn: str) -> dict | None:
    try:
        groups = (
            elbv2.describe_target_groups(TargetGroupArns=[arn]).get("TargetGroups")
            or []
        )
    except ClientError as exc:
        if _not_found(exc):
            return None
        raise
    return next((g for g in groups if g.get("TargetGroupArn") == arn), None)


def _load_balancer(elbv2: Any, arn: str) -> dict | None:
    try:
        balancers = (
            elbv2.describe_load_balancers(LoadBalancerArns=[arn]).get("LoadBalancers")
            or []
        )
    except ClientError as exc:
        if _not_found(exc):
            return None
        raise
    return next((b for b in balancers if b.get("LoadBalancerArn") == arn), None)


def _elb_tags(elbv2: Any, arn: str) -> dict[str, Any]:
    descriptions = elbv2.describe_tags(ResourceArns=[arn]).get("TagDescriptions") or []
    return _tags(
        next((d.get("Tags") for d in descriptions if d.get("ResourceArn") == arn), None)
    )


def transition_owned(tags: Mapping[str, Any], plan: ServiceTransitionPlan) -> bool:
    return (
        tags.get(TRANSITION_TAG_KEY) == plan.transition_id
        and tags.get(EXECUTION_TAG_KEY) == plan.execution_id
    )


def alb_private_ips(ec2: Any, load_balancer_arn: str) -> tuple[str, ...]:
    """ALB 노드 ENI의 사설 IP — ALB 경로 응답의 상대 주소가 이 안에 있어야 한다(프록시 배제)."""
    description = f"ELB {load_balancer_resource(load_balancer_arn)}"
    interfaces = _pages(
        ec2.describe_network_interfaces,
        "NetworkInterfaces",
        Filters=[{"Name": "description", "Values": [escape_filter_value(description)]}],
    )
    ips = {
        address.get("PrivateIpAddress")
        for interface in interfaces
        if interface.get("Description") == description
        for address in interface.get("PrivateIpAddresses") or ()
    }
    return tuple(sorted(ip for ip in ips if ip))


# ------------------------------------------------------------------ #446 ASG
@dataclass
class GroupFacts:
    group: dict | None
    template_id: str | None
    members: list[dict]
    problems: list[str]

    @property
    def member_ids(self) -> list[str]:
        return sorted(str(m.get("InstanceId")) for m in self.members)


def managed_group(c: Clients, plan: AutoscalingExecutionPlan) -> GroupFacts:
    """#446 실행의 ASG — 소유 태그·구성 대조는 autoscaling._group_problem과 같은 기준이다."""
    problems: list[str] = []
    templates = [
        t
        for t in _launch_templates_named(c.ec2, plan.launch_template_name)
        if _owned(_tags(t.get("Tags")), plan)
    ]
    template_id = templates[0].get("LaunchTemplateId") if len(templates) == 1 else None
    if template_id is None:
        problems.append(
            f"이 실행의 시작 템플릿 {plan.launch_template_name}을 특정하지 못했습니다"
        )
    groups = _groups_named(c.asg, plan.asg_name)
    if not groups:
        return GroupFacts(
            None, template_id, [], [*problems, f"ASG {plan.asg_name} 없음"]
        )
    group = groups[0]
    owned, problem = _group_problem(group, plan, template_id)
    if not owned:
        return GroupFacts(None, template_id, [], [*problems, problem])
    if problem:
        problems.append(problem)
    members = [m for m in group.get("Instances") or () if isinstance(m, Mapping)]
    if not members:
        problems.append("ASG 멤버가 없습니다")
    for member in members:
        spec = member.get("LaunchTemplate") or {}
        if (
            member.get("LifecycleState") != "InService"
            or member.get("HealthStatus") != "Healthy"
        ):
            problems.append(
                f"멤버 {member.get('InstanceId')} {member.get('LifecycleState')}/{member.get('HealthStatus')}"
            )
        if spec.get("LaunchTemplateId") != template_id or spec.get("Version") != str(
            plan.launch_template_version
        ):
            problems.append(
                f"멤버 {member.get('InstanceId')}가 이 실행의 시작 템플릿 버전으로 뜨지 않았습니다"
            )
    return GroupFacts(group, template_id, members, problems)


# ------------------------------------------------------------------ ① 계획 조회
@dataclass
class PlanInspection:
    checks: list[CheckResult]
    original_target_group_arn: str | None = None
    original_targets: tuple[TargetState, ...] = ()
    original_quarantine: dict[str, list[str]] = field(default_factory=dict)
    member_ids: list[str] = field(default_factory=list)
    probe_private_ip: str | None = None


def inspect_for_plan(
    c: Clients, options: ServiceTransitionPlanOptions, as_plan: AutoscalingExecutionPlan
) -> PlanInspection:
    """계획 전 읽기 전용 확인 + 운영 리스너 기본 동작 캡처. 실패 항목은 checks에 남는다."""
    checks = session_checks(c, account_id=options.account_id, region=options.region)
    target_region, target_account = as_plan.target_arn.split(":")[3:5]
    checks.append(
        check(
            "execution_scope",
            (target_region, target_account) == (options.region, options.account_id),
            f"#446 원본 {as_plan.target_arn}",
        )
    )
    group = managed_group(c, as_plan)
    checks.append(
        check(
            "asg_managed",
            not group.problems,
            "; ".join(group.problems) or "소유·구성·멤버 정상",
        )
    )
    attached = list((group.group or {}).get("TargetGroupARNs") or ()) + list(
        (group.group or {}).get("LoadBalancerNames") or ()
    )
    checks.append(
        check("asg_unattached", not attached, f"ASG 기존 부착 {attached or '없음'}")
    )

    lb = _load_balancer(c.elbv2, options.load_balancer_arn)
    lb_ok = bool(
        lb
        and lb.get("Scheme") == "internal"
        and lb.get("Type") == "application"
        and lb.get("VpcId") == options.vpc_id
        and (lb.get("State") or {}).get("Code") == "active"
    )
    checks.append(
        check(
            "load_balancer", lb_ok, "기존 internal ALB(active·지정 VPC)만 재사용합니다"
        )
    )

    listener = _listener(c.elbv2, options.listener_arn)
    inspection = PlanInspection(checks=checks, member_ids=group.member_ids)
    listener_ok = bool(
        listener
        and listener.get("LoadBalancerArn") == options.load_balancer_arn
        and listener.get("Protocol") == "HTTP"
        and listener.get("Port") == SERVICE_PORT
    )
    checks.append(
        check(
            "production_listener",
            listener_ok,
            "지정 리스너는 그 ALB의 HTTP 80이어야 합니다",
        )
    )
    if not (lb_ok and listener_ok):
        return inspection
    target, unsupported = forward_target(listener.get("DefaultActions"))
    checks.append(
        check(
            "default_action_supported",
            target is not None,
            unsupported or f"forward {target}",
        )
    )
    rules = _non_default_rules(c.elbv2, options.listener_arn)
    checks.append(
        check(
            "no_listener_rules",
            not rules,
            f"기본 외 규칙 {len(rules)}개 — 지원하지 않습니다",
        )
    )
    if target is None:
        return inspection
    original = _target_group(c.elbv2, target)
    checks.append(
        check(
            "original_target_group",
            bool(
                original
                and original.get("VpcId") == options.vpc_id
                and original.get("Protocol") == "HTTP"
                and original.get("TargetType") == "instance"
            ),
            f"원래 TG {target}",
        )
    )
    inspection.original_target_group_arn = target
    inspection.original_targets = _target_states(c.elbv2, target)
    overlap = sorted(
        {t.instance_id for t in inspection.original_targets} & set(group.member_ids)
    )
    checks.append(
        check(
            "members_outside_original",
            not overlap,
            f"원래 TG에 든 ASG 멤버 {overlap or '없음'}",
        )
    )
    checks.append(
        check(
            "validation_port_unused",
            _listener_on_port(
                c.elbv2, options.load_balancer_arn, options.validation_listener_port
            )
            is None,
            f"검증 포트 {options.validation_listener_port}",
        )
    )

    instance_ids = [
        as_plan.source.instance_id,
        options.probe_instance_id,
        *group.member_ids,
        *(t.instance_id for t in inspection.original_targets),
    ]
    instances = _instances(c.ec2, instance_ids)
    groups = _security_groups(
        c.ec2,
        [*(lb.get("SecurityGroups") or ()), options.probe_security_group_id]
        + [g for item in instances.values() for g in _instance_group_ids(item)],
    )
    source = instances.get(as_plan.source.instance_id) or {}
    checks.append(
        check(
            "source_vpc",
            source.get("VpcId") == options.vpc_id,
            f"원본 {as_plan.source.instance_id}",
        )
    )
    probe = instances.get(options.probe_instance_id) or {}
    inspection.probe_private_ip = probe.get("PrivateIpAddress")
    checks.append(
        check(
            "probe_instance",
            (probe.get("State") or {}).get("Name") == "running"
            and probe.get("VpcId") == options.vpc_id
            and options.probe_security_group_id in _instance_group_ids(probe)
            and bool(inspection.probe_private_ip),
            f"탐침 {options.probe_instance_id}(running·지정 VPC·탐침 SG 부착·사설 IP)",
        )
    )
    checks.append(
        validation_port_restriction(
            [groups[g] for g in lb.get("SecurityGroups") or () if g in groups],
            options.validation_listener_port,
            options.probe_security_group_id,
        )
    )
    unreachable = [
        m
        for m in group.member_ids
        if not member_probe_access(
            groups,
            _primary_group_ids(instances.get(m) or {}),
            options.probe_security_group_id,
        )
    ]
    checks.append(
        check(
            "member_probe_access",
            not unreachable,
            f"탐침 SG→80 미허용 멤버 {unreachable or '없음'}",
        )
    )
    fleet = {
        i: quarantine_basis(instances.get(i) or {}, groups)
        for i in [as_plan.source.instance_id, *group.member_ids]
    }
    isolated = {i: basis for i, basis in fleet.items() if basis}
    checks.append(
        check(
            "aws_isolation_clear",
            not isolated,
            f"격리 SG가 붙은 원본·멤버 {isolated or '없음'}",
        )
    )
    inspection.original_quarantine = {
        t.instance_id: quarantine_basis(instances.get(t.instance_id) or {}, groups)
        for t in inspection.original_targets
    }
    registered = {t.instance_id for t in inspection.original_targets}
    checks.append(
        check(
            "original_expected_bodies",
            set(options.original_target_expected_bodies) == registered,
            f"기대 본문 대상 {sorted(options.original_target_expected_bodies)} / 원래 TG 대상 {sorted(registered)}",
        )
    )
    blind = [
        t.instance_id
        for t in inspection.original_targets
        if not inspection.original_quarantine[t.instance_id]
        and not member_probe_access(
            groups,
            _primary_group_ids(instances.get(t.instance_id) or {}),
            options.probe_security_group_id,
        )
    ]
    checks.append(
        check(
            "original_probe_access",
            not blind,
            f"탐침 SG→80 미허용 원래 대상 {blind or '없음'}(복귀 전 직접 응답용)",
        )
    )
    return inspection


# ------------------------------------------------------------------ 현재 상태
@dataclass
class LiveState:
    observed_at: datetime
    group: GroupFacts
    load_balancer: dict | None
    target_group: dict | None
    target_group_owned: bool
    validation_listener: dict | None
    validation_listener_owned: bool
    production: RouteSnapshot
    attached_target_groups: list[str]
    attached_load_balancers: list[str]

    @property
    def target_group_arn(self) -> str | None:
        return (
            self.target_group.get("TargetGroupArn")
            if self.target_group and self.target_group_owned
            else None
        )

    def phase(
        self, plan: ServiceTransitionPlan
    ) -> Literal["original", "transition", "drifted"]:
        current = self.production.target_group_arn
        if (
            self.production.unsupported is None
            and self.production.non_default_rules == 0
        ):
            if current == plan.original_route.target_group_arn:
                return "original"
            if current is not None and current == self.target_group_arn:
                return "transition"
        return "drifted"


def observe_live(
    c: Clients, plan: ServiceTransitionPlan, as_plan: AutoscalingExecutionPlan
) -> LiveState:
    group = managed_group(c, as_plan)
    target_group = _target_group_named(c.elbv2, plan.target_group_name)
    listener = _listener_on_port(
        c.elbv2, plan.load_balancer_arn, plan.validation_listener_port
    )
    return LiveState(
        observed_at=_now(),
        group=group,
        load_balancer=_load_balancer(c.elbv2, plan.load_balancer_arn),
        target_group=target_group,
        target_group_owned=bool(
            target_group
            and transition_owned(
                _elb_tags(c.elbv2, target_group["TargetGroupArn"]), plan
            )
        ),
        validation_listener=listener,
        validation_listener_owned=bool(
            listener
            and transition_owned(_elb_tags(c.elbv2, listener["ListenerArn"]), plan)
        ),
        production=route_snapshot(c.elbv2, plan.original_route.listener_arn),
        attached_target_groups=sorted((group.group or {}).get("TargetGroupARNs") or ()),
        attached_load_balancers=sorted(
            (group.group or {}).get("LoadBalancerNames") or ()
        ),
    )


def live_view(live: LiveState, plan: ServiceTransitionPlan) -> dict[str, Any]:
    """현재 관측의 JSON 표현 — 계획 스냅숏과 섞지 않는다."""
    return {
        "observed_at": live.observed_at.isoformat(),
        "route_phase": live.phase(plan),
        "production_route": live.production.model_dump(mode="json"),
        "target_group": {
            "arn": live.target_group.get("TargetGroupArn")
            if live.target_group
            else None,
            "owned_by_transition": live.target_group_owned,
        },
        "validation_listener": {
            "arn": live.validation_listener.get("ListenerArn")
            if live.validation_listener
            else None,
            "owned_by_transition": live.validation_listener_owned,
        },
        "asg": {
            "name": (live.group.group or {}).get("AutoScalingGroupName"),
            "members": [
                {
                    k: m.get(k)
                    for k in (
                        "InstanceId",
                        "LifecycleState",
                        "HealthStatus",
                        "ProtectedFromScaleIn",
                    )
                }
                for m in live.group.members
            ],
            "problems": live.group.problems,
            "attached_target_groups": live.attached_target_groups,
            "attached_load_balancers": live.attached_load_balancers,
        },
    }


def original_route_targets(
    c: Clients, plan: ServiceTransitionPlan
) -> tuple[TargetState, ...]:
    return _target_states(c.elbv2, plan.original_route.target_group_arn)


def prepare_checks(
    c: Clients,
    plan: ServiceTransitionPlan,
    as_plan: AutoscalingExecutionPlan,
    live: LiveState,
) -> list[CheckResult]:
    """prepare 직전 확인. 기존 자원은 이 전환의 소유 태그일 때만 이어 쓴다(남의 같은 이름은 거절)."""
    checks = session_checks(c, account_id=plan.account_id, region=plan.region)
    group = live.group
    checks.append(
        check(
            "asg_managed",
            group.group is not None and not group.problems,
            "; ".join(group.problems) or "정상",
        )
    )
    lb = live.load_balancer or {}
    checks.append(
        check(
            "load_balancer",
            lb.get("Scheme") == "internal"
            and lb.get("VpcId") == plan.vpc_id
            and (lb.get("State") or {}).get("Code") == "active",
            "계획한 internal ALB(active)",
        )
    )
    checks.append(
        check(
            "target_group_free_or_owned",
            live.target_group is None
            or (
                live.target_group_owned
                and _target_group_config_ok(live.target_group, plan)
            ),
            f"TG 이름 {plan.target_group_name}",
        )
    )
    listener_target = None
    if live.validation_listener is not None:
        listener_target, _ = forward_target(
            live.validation_listener.get("DefaultActions")
        )
    checks.append(
        check(
            "validation_listener_free_or_owned",
            live.validation_listener is None
            or (
                live.validation_listener_owned
                and listener_target == live.target_group_arn
            ),
            f"검증 포트 {plan.validation_listener_port}",
        )
    )
    foreign = [a for a in live.attached_target_groups if a != live.target_group_arn]
    checks.append(
        check(
            "asg_attachment_free_or_owned",
            not foreign and not live.attached_load_balancers,
            f"ASG의 다른 부착 {foreign + live.attached_load_balancers or '없음'}",
        )
    )
    members = group.member_ids
    instances = _instances(c.ec2, [as_plan.source.instance_id, *members])
    groups = _security_groups(
        c.ec2,
        [*(lb.get("SecurityGroups") or ())]
        + [g for item in instances.values() for g in _instance_group_ids(item)],
    )
    checks.append(
        validation_port_restriction(
            [groups[g] for g in lb.get("SecurityGroups") or () if g in groups],
            plan.validation_listener_port,
            plan.probe.security_group_id,
        )
    )
    fleet = {
        i: quarantine_basis(instances.get(i) or {}, groups)
        for i in [as_plan.source.instance_id, *members]
    }
    isolated = {i: basis for i, basis in fleet.items() if basis}
    checks.append(
        check(
            "aws_isolation_clear",
            not isolated,
            f"격리 SG가 붙은 원본·멤버 {isolated or '없음'} — TG 부착은 격리 멤버를 등록한다",
        )
    )
    return checks


# ------------------------------------------------------------------ ③ 서비스 증거
def http_probe(
    probe: Any,
    *,
    path_kind: str,
    host: str,
    port: int,
    peers: tuple[str, ...],
    expected_bodies: tuple[str, ...],
    instance_id: str | None = None,
) -> HttpProbeEvidence:
    """GET 1건 — 고유 요청 ID를 헤더·쿼리에 싣는다. 리다이렉트·상한 초과·허용 밖 상대·기대 밖 본문은 실패다.

    기대 본문은 계획에서만 온다. ALB 경로처럼 대상을 가리지 못하는 응답은 그 경로 대상들의 기대
    본문 중 하나와 같아야 한다 — 어느 대상이 답했는지는 이 증거가 말하지 않는다.
    """
    request_id = uuid.uuid4().hex
    path = f"{SERVICE_PATH}?vigilantis-probe={request_id}"
    status = body_bytes = digest = location = peer = error = None
    matches = False
    try:
        response = probe.get(
            host=host,
            port=port,
            path=path,
            headers={PROBE_HEADER: request_id},
            timeout=PROBE_TIMEOUT_SECONDS,
            max_body_bytes=PROBE_MAX_BODY_BYTES,
        )
        status = int(response["status"])
        body = bytes(response["body"])
        headers = {
            str(k).lower(): v for k, v in (response.get("headers") or {}).items()
        }
        location = headers.get("location")
        peer = response.get("peer_ip")
        body_bytes = len(body)
        digest = hashlib.sha256(body).hexdigest()
        if body_bytes > PROBE_MAX_BODY_BYTES:
            error = f"응답 본문이 상한 {PROBE_MAX_BODY_BYTES}바이트를 넘었습니다"
        matches = any(body == expected.encode() for expected in expected_bodies)
    except (OSError, HTTPException, KeyError, TypeError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
    return HttpProbeEvidence(
        path_kind=path_kind,
        instance_id=instance_id,
        host=str(host),
        port=port,
        path=path,
        request_id=request_id,
        status=status,
        body_bytes=body_bytes,
        body_sha256=digest,
        expected_sha256=tuple(
            sorted({hashlib.sha256(e.encode()).hexdigest() for e in expected_bodies})
        ),
        body_matches=matches,
        location=location,
        peer_ip=peer,
        peer_allowed=peers,
        error=error,
        observed_at=_now(),
    )


@dataclass
class ServiceInspection:
    checks: list[CheckResult]
    http: list[HttpProbeEvidence] = field(default_factory=list)
    routes: list[RouteSnapshot] = field(default_factory=list)
    member_ids: list[str] = field(default_factory=list)
    live: LiveState | None = None


def _target_group_config_ok(
    group: Mapping[str, Any], plan: ServiceTransitionPlan
) -> bool:
    return (
        group.get("VpcId") == plan.vpc_id
        and group.get("Protocol") == "HTTP"
        and group.get("Port") == SERVICE_PORT
        and group.get("TargetType") == "instance"
        and group.get("HealthCheckPath") == SERVICE_PATH
        and (group.get("Matcher") or {}).get("HttpCode") == "200"
    )


def inspect_service(
    c: Clients, plan: ServiceTransitionPlan, as_plan: AutoscalingExecutionPlan
) -> ServiceInspection:
    """새 경로의 서비스 증거. 구조 확인이 하나라도 실패하면 HTTP 증거를 만들지 않는다."""
    checks = session_checks(c, account_id=plan.account_id, region=plan.region)
    live = observe_live(c, plan, as_plan)
    result = ServiceInspection(
        checks=checks, member_ids=live.group.member_ids, live=live
    )
    group = live.group
    tg_arn = live.target_group_arn
    checks.append(
        check(
            "asg_managed",
            group.group is not None and not group.problems,
            "; ".join(group.problems) or "정상",
        )
    )
    checks.append(
        check(
            "target_group_owned",
            tg_arn is not None and _target_group_config_ok(live.target_group, plan),
            f"전환 TG {plan.target_group_name}(소유 태그·HTTP 80 · 헬스체크 / 200)",
        )
    )
    checks.append(
        check(
            "asg_attachment_exclusive",
            tg_arn is not None
            and live.attached_target_groups == [tg_arn]
            and not live.attached_load_balancers,
            f"ASG 부착 {live.attached_target_groups} {live.attached_load_balancers}",
        )
    )
    listener = live.validation_listener
    before = (
        route_snapshot(c.elbv2, listener["ListenerArn"])
        if listener and live.validation_listener_owned
        else None
    )
    checks.append(
        check(
            "validation_listener_owned",
            before is not None
            and before.target_group_arn == tg_arn
            and before.unsupported is None
            and before.non_default_rules == 0,
            f"검증 리스너 {plan.validation_listener_port} → 전환 TG 단일 forward",
        )
    )
    members = set(group.member_ids)
    if before is not None:
        result.routes.append(before)
        registered = {t.instance_id for t in before.targets}
        unhealthy = [t.instance_id for t in before.targets if t.state != "healthy"]
        checks.append(
            check(
                "asg_only_targets",
                bool(members) and registered == members and not unhealthy,
                f"TG 대상 {sorted(registered)} / ASG 멤버 {sorted(members)} / 비정상 {unhealthy or '없음'}",
            )
        )
    lb = live.load_balancer or {}
    instances = _instances(
        c.ec2, [as_plan.source.instance_id, plan.probe.instance_id, *members]
    )
    groups = _security_groups(
        c.ec2,
        [*(lb.get("SecurityGroups") or ()), plan.probe.security_group_id]
        + [g for item in instances.values() for g in _instance_group_ids(item)],
    )
    checks.append(
        validation_port_restriction(
            [groups[g] for g in lb.get("SecurityGroups") or () if g in groups],
            plan.validation_listener_port,
            plan.probe.security_group_id,
        )
    )
    unreachable = [
        m
        for m in sorted(members)
        if not member_probe_access(
            groups,
            _primary_group_ids(instances.get(m) or {}),
            plan.probe.security_group_id,
        )
    ]
    checks.append(
        check(
            "member_probe_access",
            not unreachable,
            f"탐침 SG→80 미허용 멤버 {unreachable or '없음'}",
        )
    )
    checks += probe_checks(c, plan, instances)
    fleet = {
        i: quarantine_basis(instances.get(i) or {}, groups)
        for i in [as_plan.source.instance_id, *sorted(members)]
    }
    isolated = {i: basis for i, basis in fleet.items() if basis}
    checks.append(
        check(
            "aws_isolation_clear",
            not isolated,
            f"격리 SG가 붙은 원본·멤버 {isolated or '없음'}",
        )
    )
    if not all(item.passed for item in checks):
        return result

    alb = http_probe(
        c.http,
        path_kind="alb_validation_listener",
        host=lb.get("DNSName"),
        port=plan.validation_listener_port,
        peers=alb_private_ips(c.ec2, plan.load_balancer_arn),
        expected_bodies=(plan.clone_expected_body,),
    )
    after = route_snapshot(c.elbv2, listener["ListenerArn"])
    result.routes.append(after)
    result.http.append(alb)
    checks.append(
        check(
            "route_stable_during_request",
            before.same_route(after),
            f"요청 {alb.request_id} 전후 경로·대상 동일 — ASG 전용 경로 증거이며 요청 단위 대상 귀속은 아님",
        )
    )
    for member in sorted(members):
        address = instances[member].get("PrivateIpAddress")
        result.http.append(
            http_probe(
                c.http,
                path_kind="member_direct",
                host=address,
                port=SERVICE_PORT,
                peers=(address,),
                expected_bodies=(plan.clone_expected_body,),
                instance_id=member,
            )
        )
    return result


def probe_checks(
    c: Clients, plan: ServiceTransitionPlan, instances: Mapping[str, Mapping]
) -> list[CheckResult]:
    """HTTP 증거를 남기기 전의 실제 출발지 전제 — 지정 탐침 인스턴스이고 이 프로세스가 그 위에서 돈다."""
    probe = instances.get(plan.probe.instance_id) or {}
    checks = [
        check(
            "probe_instance",
            (probe.get("State") or {}).get("Name") == "running"
            and probe.get("VpcId") == plan.vpc_id
            and plan.probe.security_group_id in _instance_group_ids(probe)
            and probe.get("PrivateIpAddress") == plan.probe.private_ip,
            f"탐침 {plan.probe.instance_id}",
        )
    ]
    try:
        origin = c.http.origin_instance_id()
        detail = f"이 프로세스의 인스턴스 {origin} / 지정 탐침 {plan.probe.instance_id}"
    except (OSError, HTTPException) as exc:
        origin, detail = None, f"탐침 출발지 확인 실패: {type(exc).__name__}"
    checks.append(check("probe_origin", origin == plan.probe.instance_id, detail))
    return checks


# ------------------------------------------------------------------ 복귀 조회
# 리스너가 요청을 보내는 대상 상태 — unavailable은 헬스 체크를 끈 TG의 대상이다(그래도 보낸다)
_SERVING_STATES = ("healthy", "initial", "unavailable")
RouteEligibility = Literal["routable", "not_routable", "unknown"]


def return_routing(
    target: TargetState,
    instance: Mapping[str, Any] | None,
    alb_zones: frozenset[str] | None,
) -> tuple[RouteEligibility, str]:
    """원래 경로로 돌린 뒤 이 등록 대상에 요청이 갈 수 있는가 — (판정, 근거).

    cutover 뒤 원래 TG는 리스너가 없어 대상이 unused다. AWS 문서상 unused의 Target.NotInUse는 'TG를 쓰는
    리스너 없음'과 'ALB에 켜지지 않은 AZ'를 함께 뜻하므로, 인스턴스가 running이고 그 AZ가 실제 ALB에 켜져
    있음을 확인한 대상만 routable로 본다. InvalidState·IpUnusable·꺼진 AZ는 routable이 아니다(검증된
    대상으로 세지 않는다). 확인하지 못하면 unknown — 호출부는 안전하다고 보지 않는다.
    """
    if target.state in _SERVING_STATES:
        return "routable", target.state
    if target.state == "unused" and target.reason == "Target.NotInUse":
        if instance is None:
            return "unknown", "unused(Target.NotInUse) — 인스턴스를 읽지 못했습니다"
        state = (instance.get("State") or {}).get("Name")
        if state != "running":
            return "not_routable", f"unused(Target.NotInUse) — 인스턴스 {state}"
        zone = (instance.get("Placement") or {}).get("AvailabilityZone")
        if alb_zones is None or zone is None:
            return (
                "unknown",
                "unused(Target.NotInUse) — ALB AZ 활성 여부를 확인하지 못했습니다",
            )
        if zone not in alb_zones:
            return (
                "not_routable",
                f"unused(Target.NotInUse) — AZ {zone}가 ALB에 켜져 있지 않습니다",
            )
        return (
            "routable",
            f"unused(Target.NotInUse) — running·AZ {zone} 활성, 복귀하면 라우팅됩니다",
        )
    return (
        "not_routable",
        f"{target.state}({target.reason})" if target.reason else target.state,
    )


@dataclass
class ReturnInspection:
    checks: list[CheckResult]
    production: RouteSnapshot
    original_targets: tuple[TargetState, ...]
    quarantine: dict[str, list[str]]
    private_ips: dict[str, str | None]
    probe_access: dict[str, bool]
    dns_name: str | None
    # (instance_id, port) → return_routing 판정
    routing: dict[tuple[str, int | None], tuple[RouteEligibility, str]]


def inspect_return(c: Clients, plan: ServiceTransitionPlan) -> ReturnInspection:
    """원래 경로를 대상별로 읽는다 — 등록·헬스·라우팅 가능성·현재 격리 SG·탐침 접근, 그리고 탐침 출발지 전제.

    대상을 등록·해제·격리 해제하지 않는다. 출발지 전제가 깨지면 호출부는 HTTP를 보내지 않는다.
    """
    checks = session_checks(c, account_id=plan.account_id, region=plan.region)
    production = route_snapshot(c.elbv2, plan.original_route.listener_arn)
    targets = _target_states(c.elbv2, plan.original_route.target_group_arn)
    instances = _instances(
        c.ec2, [plan.probe.instance_id, *(t.instance_id for t in targets)]
    )
    groups = _security_groups(
        c.ec2, [g for item in instances.values() for g in _instance_group_ids(item)]
    )
    checks += probe_checks(c, plan, instances)
    lb = _load_balancer(c.elbv2, plan.load_balancer_arn) or {}
    zones = (
        frozenset(
            z["ZoneName"]
            for z in lb.get("AvailabilityZones") or ()
            if z.get("ZoneName")
        )
        or None
    )
    return ReturnInspection(
        checks=checks,
        production=production,
        original_targets=targets,
        routing={
            (t.instance_id, t.port): return_routing(
                t, instances.get(t.instance_id), zones
            )
            for t in targets
        },
        quarantine={
            t.instance_id: quarantine_basis(instances.get(t.instance_id) or {}, groups)
            for t in targets
        },
        private_ips={
            t.instance_id: (instances.get(t.instance_id) or {}).get("PrivateIpAddress")
            for t in targets
        },
        probe_access={
            t.instance_id: member_probe_access(
                groups,
                _primary_group_ids(instances.get(t.instance_id) or {}),
                plan.probe.security_group_id,
            )
            for t in targets
        },
        dns_name=lb.get("DNSName"),
    )


# ------------------------------------------------------------------ 시도 발자국·원본·보안 보류(읽기 전용)
def service_footprint(
    c: Clients, plan: ServiceTransitionPlan, as_plan: AutoscalingExecutionPlan
) -> list[str]:
    """시도가 AWS에 남긴 서비스 자원 — 지금 읽은 존재로만 판단한다(삭제 응답을 믿지 않는다)."""
    found = []
    group = _target_group_named(c.elbv2, plan.target_group_name)
    if group is not None:
        found.append(f"TG {group['TargetGroupArn']}")
    listener = _listener_on_port(
        c.elbv2, plan.load_balancer_arn, plan.validation_listener_port
    )
    if listener is not None and transition_owned(
        _elb_tags(c.elbv2, listener["ListenerArn"]), plan
    ):
        found.append(f"검증 리스너 {listener['ListenerArn']}")
    if group is not None:
        asgs = _groups_named(c.asg, as_plan.asg_name)
        if asgs and group["TargetGroupArn"] in (asgs[0].get("TargetGroupARNs") or ()):
            found.append(f"ASG 부착 {group['TargetGroupArn']}")
        if (
            route_snapshot(c.elbv2, plan.original_route.listener_arn).target_group_arn
            == group["TargetGroupArn"]
        ):
            found.append("운영 경로가 이 시도의 TG로 갑니다")
    return found


def source_instance(
    c: Clients, as_plan: AutoscalingExecutionPlan
) -> dict[str, Any] | None:
    """복제 원본 인스턴스의 현재 관측 — 비용 관문의 입력. 조회되지 않으면 None(모름)."""
    try:
        instance = _instances(c.ec2, [as_plan.source.instance_id]).get(
            as_plan.source.instance_id
        )
    except ClientError as exc:
        if _not_found(exc):
            return None
        raise
    if instance is None:
        return None
    return {
        "instance_id": as_plan.source.instance_id,
        "state": (instance.get("State") or {}).get("Name"),
        "instance_type": instance.get("InstanceType"),
    }


def security_holds(
    c: Clients, as_plan: AutoscalingExecutionPlan
) -> tuple[list[str], dict[str, list[str]]]:
    """정리가 없앨 수 있는 인스턴스(이 실행 ASG 멤버 + 살아 있는 실행 태그 인스턴스)와 그 보안 보류 근거.

    근거는 현재 ENI의 격리 SG, ASG의 ProtectedFromScaleIn(참이거나 값이 없음 — 모름은 보류다),
    상태를 읽지 못함이다. DB의 열린 격리 실행은 facade가 더한다. 보류는 풀지 않는다 — 보고하고 막는다.
    """
    groups = [
        g
        for g in _groups_named(c.asg, as_plan.asg_name)
        if _owned(_tags(g.get("Tags")), as_plan)
    ]
    members = {
        str(m.get("InstanceId")): m for g in groups for m in g.get("Instances") or ()
    }
    alive = {
        i
        for i, item in _tagged_instances(c.ec2, as_plan.execution_id).items()
        if (item.get("State") or {}).get("Name") in _LIVE_INSTANCE
    }
    ids = sorted(set(members) | alive)
    instances = _instances(c.ec2, ids)
    sgs = _security_groups(
        c.ec2, [g for item in instances.values() for g in _instance_group_ids(item)]
    )
    holds: dict[str, list[str]] = {}
    for instance_id in ids:
        basis = []
        if instance_id not in instances:
            basis.append("상태를 읽지 못했습니다")
        else:
            basis += quarantine_basis(instances[instance_id], sgs)
        member = members.get(instance_id)
        if member is not None:
            protected = member.get("ProtectedFromScaleIn")
            if protected is True:
                basis.append("ProtectedFromScaleIn")
            elif protected is not False:
                basis.append("scale-in 보호 상태를 알 수 없습니다")
        if basis:
            holds[instance_id] = basis
    return ids, holds


def _launch_template_image(
    ec2: Any, spec: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """ASG가 쓰는 시작 템플릿 버전의 AMI. ($Default·$Latest 포함) 확정 못 하면 (None, 사유)."""
    version = str(spec.get("Version") or "$Default")
    params = (
        {"LaunchTemplateId": spec["LaunchTemplateId"]}
        if spec.get("LaunchTemplateId")
        else {"LaunchTemplateName": spec.get("LaunchTemplateName")}
    )
    try:
        versions = _pages(
            ec2.describe_launch_template_versions,
            "LaunchTemplateVersions",
            Versions=[version],
            **params,
        )
    except ClientError as exc:
        if _not_found(exc) or aws_error_code(exc).endswith("VersionNotFound"):
            return None, f"시작 템플릿 {params} v{version}을 읽지 못했습니다"
        raise
    image = (
        (versions[0].get("LaunchTemplateData") or {}).get("ImageId")
        if versions
        else None
    )
    if not isinstance(image, str) or not image.startswith("ami-"):
        return (
            None,
            f"시작 템플릿 {params} v{version}의 AMI를 확정할 수 없습니다({image})",
        )
    return image, None


def external_launch_refs(
    c: Clients,
    as_plan: AutoscalingExecutionPlan,
    image_id: str | None,
    template_ids: list[str],
    template_names: list[str],
) -> list[str]:
    """이 실행 밖의 ASG가 이 실행의 시작 템플릿·AMI를 쓰는가 — 확정 못 하는 참조는 쓰는 것으로 본다.

    시작 템플릿 참조는 ID와 이름 둘 다로 이 실행의 템플릿과 대조한다 — AMI가 이미 없어도(템플릿이 다른
    AMI를 가리켜도) 템플릿 자체를 쓰는 ASG가 있으면 막는다. 그다음 다른 템플릿(버전 $Default·$Latest·번호),
    MixedInstancesPolicy의 템플릿·override, 레거시 시작 구성이 이 AMI를 쓰는지 본다. 끝으로 이 실행 소유 밖의
    모든 시작 템플릿의 모든 버전(번호 버전·ASG에 붙지 않은 독립 템플릿 포함)이 쓰는지 본다. 확정 못 하는
    AMI 참조(resolve:ssm 등)는 쓰는 것으로 본다. 조회 실패는 예외로 올라간다(부재가 아니다).
    """
    blockers = []
    for group in _pages(c.asg.describe_auto_scaling_groups, "AutoScalingGroups"):
        name = group.get("AutoScalingGroupName")
        if name == as_plan.asg_name:
            continue
        mixed = (group.get("MixedInstancesPolicy") or {}).get("LaunchTemplate") or {}
        specs = [
            group.get("LaunchTemplate"),
            mixed.get("LaunchTemplateSpecification"),
        ] + [o.get("LaunchTemplateSpecification") for o in mixed.get("Overrides") or ()]
        for spec in (s for s in specs if s):
            if (
                spec.get("LaunchTemplateId") in template_ids
                or spec.get("LaunchTemplateName") in template_names
            ):
                blockers.append(f"ASG {name}가 이 실행의 시작 템플릿을 씁니다")
                continue
            if image_id is None:
                continue
            image, problem = _launch_template_image(c.ec2, spec)
            if problem:
                blockers.append(
                    f"ASG {name}의 시작 참조를 확정할 수 없습니다: {problem}"
                )
            elif image == image_id:
                blockers.append(
                    f"ASG {name}가 다른 시작 템플릿으로 AMI {image_id}를 씁니다"
                )
        configuration = group.get("LaunchConfigurationName")
        if configuration and image_id is not None:
            found = (
                c.asg.describe_launch_configurations(
                    LaunchConfigurationNames=[configuration]
                ).get("LaunchConfigurations")
                or []
            )
            if not found:
                blockers.append(
                    f"ASG {name}의 시작 구성 {configuration}을 읽지 못했습니다"
                )
            elif found[0].get("ImageId") == image_id:
                blockers.append(f"ASG {name}의 시작 구성이 AMI {image_id}를 씁니다")
    if image_id is not None:
        blockers += _standalone_template_refs(c.ec2, image_id, template_ids)
    return blockers


def _standalone_template_refs(
    ec2: Any, image_id: str, owned_template_ids: list[str]
) -> list[str]:
    """이 실행 소유가 아닌 모든 시작 템플릿의 모든 버전이 AMI를 쓰는가.

    확정 못 하는 ImageId(resolve:ssm 등 ami-로 시작하지 않는 값)는 쓰는 것으로 본다.
    ImageId가 없는 버전은 AMI를 가리키지 않는다. 조회 실패는 예외로 올라간다.
    """
    blockers = []
    for template in _pages(ec2.describe_launch_templates, "LaunchTemplates"):
        template_id = template.get("LaunchTemplateId")
        if template_id in owned_template_ids:
            continue
        for version in _pages(
            ec2.describe_launch_template_versions,
            "LaunchTemplateVersions",
            LaunchTemplateId=template_id,
        ):
            image = (version.get("LaunchTemplateData") or {}).get("ImageId")
            number = version.get("VersionNumber")
            if image == image_id:
                blockers.append(
                    f"시작 템플릿 {template_id} v{number}이 AMI {image_id}를 씁니다"
                )
            elif isinstance(image, str) and not image.startswith("ami-"):
                blockers.append(
                    f"시작 템플릿 {template_id} v{number}의 AMI 참조({image})를 확정할 수 없습니다"
                )
    return blockers


def _owned_launch_templates(
    ec2: Any, as_plan: AutoscalingExecutionPlan
) -> tuple[list[str], list[str]]:
    owned = [
        t
        for t in _launch_templates_named(ec2, as_plan.launch_template_name)
        if _owned(_tags(t.get("Tags")), as_plan)
    ]
    return (
        [t["LaunchTemplateId"] for t in owned],
        [t.get("LaunchTemplateName") or as_plan.launch_template_name for t in owned],
    )


def _external_image_instances(
    ec2: Any, image_id: str, execution_instance_ids: Mapping[str, Any]
) -> list[str]:
    """이 AMI로 떠 있는 살아 있는 인스턴스 중 이 실행 태그가 없는 것."""
    launched = _pages(
        ec2.describe_instances,
        "Reservations",
        Filters=[
            {"Name": "image-id", "Values": [image_id]},
            {"Name": "instance-state-name", "Values": list(_LIVE_INSTANCE)},
        ],
    )
    return sorted(
        i["InstanceId"]
        for r in launched
        for i in r.get("Instances") or ()
        if i.get("InstanceId") not in execution_instance_ids
    )


def _snapshot_image_users(ec2: Any, snapshot_ids: list[str]) -> list[str]:
    """지금 등록돼 있는 내 계정 AMI 중 이 스냅숏을 매핑한 것(소유 AMI 포함).

    등록 해제가 확정된 기록(State=deregistered)만 뺀다 — 비활성·상태 없음은 등록된 것으로 센다.
    """
    users = _pages(
        ec2.describe_images,
        "Images",
        Owners=["self"],
        IncludeDisabled=True,
        Filters=[{"Name": "block-device-mapping.snapshot-id", "Values": snapshot_ids}],
    )
    return sorted(i["ImageId"] for i in users if i.get("State") != "deregistered")


def shared_resource_refs(
    c: Clients, item: CleanupItem, as_plan: AutoscalingExecutionPlan
) -> list[str]:
    """파괴 호출 바로 앞의 공유 참조. 조회 실패는 부재가 아닌 예외이며 재고와 같은 검사를 쓴다."""
    rid = item.resource_id
    if item.action == "delete_launch_template":
        return external_launch_refs(
            c, as_plan, None, [rid], [as_plan.launch_template_name]
        )
    if item.action == "deregister_image":
        template_ids, template_names = _owned_launch_templates(c.ec2, as_plan)
        refs = external_launch_refs(c, as_plan, rid, template_ids, template_names)
        tagged = _tagged_instances(c.ec2, as_plan.execution_id)
        return refs + [
            f"이 실행 밖의 인스턴스 {i}가 AMI {rid}로 떠 있습니다"
            for i in _external_image_instances(c.ec2, rid, tagged)
        ]
    if item.action == "delete_snapshot":
        return [
            f"AMI {i}가 스냅숏 {rid}를 씁니다"
            for i in _snapshot_image_users(c.ec2, [rid])
        ]
    return []


# ------------------------------------------------------------------ ② 변경
@dataclass(frozen=True)
class Mutation:
    aws_operation: str
    kind: str
    resource_ref: str
    request: dict
    expect_present: bool
    call: Callable[[], Any]


@dataclass(frozen=True)
class MutationOutcome:
    state: Literal["APPLIED", "NOT_APPLIED", "UNCERTAIN"]
    response: dict | None = None
    error_code: str | None = None
    error_summary: str | None = None


def perform(mutation: Mutation) -> MutationOutcome:
    """AWS 변경 1건. 실패를 되묻거나 다시 보내지 않는다 — 불확실은 UNCERTAIN으로 돌려준다."""
    try:
        response = mutation.call() or {}
    except (ClientError, BotoCoreError) as exc:
        state = (
            "NOT_APPLIED"
            if _effect_for(exc) is ExecutionEffect.NOT_APPLIED
            else "UNCERTAIN"
        )
        code = (
            aws_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
        )
        return MutationOutcome(
            state=state, error_code=code[:128] or None, error_summary=_summarize(exc)
        )
    ids: dict[str, str] = {}
    for item in response.get("TargetGroups") or ():
        ids["target_group_arn"] = item.get("TargetGroupArn")
    for item in response.get("Listeners") or ():
        ids["listener_arn"] = item.get("ListenerArn")
    request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
    if request_id:
        ids["request_id"] = request_id
    return MutationOutcome(state="APPLIED", response=ids)


def _transition_tags(
    plan: ServiceTransitionPlan, as_plan: AutoscalingExecutionPlan
) -> list[dict[str, str]]:
    return [{"Key": t.key, "Value": t.value} for t in as_plan.resource_tags()] + [
        {"Key": TRANSITION_TAG_KEY, "Value": plan.transition_id}
    ]


def create_target_group(
    c: Clients, plan: ServiceTransitionPlan, as_plan: AutoscalingExecutionPlan
) -> Mutation:
    params = {
        "Name": plan.target_group_name,
        "Protocol": "HTTP",
        "Port": SERVICE_PORT,
        "VpcId": plan.vpc_id,
        "TargetType": "instance",
        "HealthCheckProtocol": "HTTP",
        "HealthCheckPort": "traffic-port",
        "HealthCheckPath": SERVICE_PATH,
        "Matcher": {"HttpCode": "200"},
        "Tags": _transition_tags(plan, as_plan),
    }
    return Mutation(
        _OP_CREATE_TARGET_GROUP,
        "target_group",
        plan.target_group_name,
        {"name": plan.target_group_name, "vpc_id": plan.vpc_id, "port": SERVICE_PORT},
        True,
        lambda: c.elbv2.create_target_group(**params),
    )


def create_validation_listener(
    c: Clients,
    plan: ServiceTransitionPlan,
    as_plan: AutoscalingExecutionPlan,
    target_group_arn: str,
) -> Mutation:
    params = {
        "LoadBalancerArn": plan.load_balancer_arn,
        "Protocol": "HTTP",
        "Port": plan.validation_listener_port,
        "DefaultActions": forward_action(target_group_arn),
        "Tags": _transition_tags(plan, as_plan),
    }
    return Mutation(
        _OP_CREATE_LISTENER,
        "validation_listener",
        f"{plan.load_balancer_arn}:{plan.validation_listener_port}",
        {"port": plan.validation_listener_port, "target_group_arn": target_group_arn},
        True,
        lambda: c.elbv2.create_listener(**params),
    )


def attach_target_group(
    c: Clients, as_plan: AutoscalingExecutionPlan, target_group_arn: str
) -> Mutation:
    return Mutation(
        _OP_ATTACH_TARGET_GROUPS,
        "asg_attachment",
        target_group_arn,
        {"asg_name": as_plan.asg_name, "target_group_arn": target_group_arn},
        True,
        lambda: c.asg.attach_load_balancer_target_groups(
            AutoScalingGroupName=as_plan.asg_name, TargetGroupARNs=[target_group_arn]
        ),
    )


def set_production_route(
    c: Clients,
    plan: ServiceTransitionPlan,
    *,
    target_group_arn: str,
    previous_target_group_arn: str,
) -> Mutation:
    listener_arn = plan.original_route.listener_arn
    return Mutation(
        _OP_MODIFY_LISTENER,
        "listener_default",
        listener_arn,
        {
            "target_group_arn": target_group_arn,
            "previous_target_group_arn": previous_target_group_arn,
        },
        True,
        lambda: c.elbv2.modify_listener(
            ListenerArn=listener_arn, DefaultActions=forward_action(target_group_arn)
        ),
    )


def cleanup_mutation(
    c: Clients, item: CleanupItem, as_plan: AutoscalingExecutionPlan
) -> Mutation | None:
    """매니페스트 항목의 변경. await_*는 호출 없이 관문만 본다(None)."""
    rid = item.resource_id
    if item.action == "delete_listener":
        return Mutation(
            _OP_DELETE_LISTENER,
            "listener",
            rid,
            {"listener_arn": rid},
            False,
            lambda: c.elbv2.delete_listener(ListenerArn=rid),
        )
    if item.action == "detach_target_group":
        asg_name, tg_arn = rid.split("|", 1)
        return Mutation(
            _OP_DETACH_TARGET_GROUPS,
            "asg_attachment",
            tg_arn,
            {"asg_name": asg_name, "target_group_arn": tg_arn},
            False,
            lambda: c.asg.detach_load_balancer_target_groups(
                AutoScalingGroupName=asg_name, TargetGroupARNs=[tg_arn]
            ),
        )
    if item.action == "delete_target_group":
        return Mutation(
            _OP_DELETE_TARGET_GROUP,
            "target_group_arn",
            rid,
            {"target_group_arn": rid},
            False,
            lambda: c.elbv2.delete_target_group(TargetGroupArn=rid),
        )
    if item.action == "delete_auto_scaling_group":
        return Mutation(
            _OP_DELETE_ASG,
            "asg",
            rid,
            {"asg_name": rid, "force_delete": True},
            False,
            lambda: c.asg.delete_auto_scaling_group(
                AutoScalingGroupName=rid, ForceDelete=True
            ),
        )
    if item.action == "delete_volume":
        return Mutation(
            _OP_DELETE_VOLUME,
            "volume",
            rid,
            {"volume_id": rid},
            False,
            lambda: c.ec2.delete_volume(VolumeId=rid),
        )
    if item.action == "delete_launch_template":
        return Mutation(
            _OP_DELETE_LAUNCH_TEMPLATE,
            "launch_template",
            rid,
            {"launch_template_id": rid},
            False,
            lambda: c.ec2.delete_launch_template(LaunchTemplateId=rid),
        )
    if item.action == "deregister_image":
        return Mutation(
            _OP_DEREGISTER_IMAGE,
            "image",
            rid,
            {"image_id": rid},
            False,
            lambda: c.ec2.deregister_image(ImageId=rid),
        )
    if item.action == "delete_snapshot":
        return Mutation(
            _OP_DELETE_SNAPSHOT,
            "snapshot",
            rid,
            {"snapshot_id": rid},
            False,
            lambda: c.ec2.delete_snapshot(SnapshotId=rid),
        )
    return None


# ------------------------------------------------------------------ 불확실 대조(읽기 전용)
def observe_resource(
    c: Clients,
    plan: ServiceTransitionPlan,
    kind: str,
    resource_ref: str,
    request: Mapping[str, Any],
) -> bool | None:
    """일지 1건의 대상이 지금 '변경이 반영된 쪽'(True)·'반영 전 쪽'(False)인가. 가를 수 없으면 None."""
    if kind == "target_group":
        group = _target_group_named(c.elbv2, resource_ref)
        if group is None:
            return False
        return (
            True
            if transition_owned(_elb_tags(c.elbv2, group["TargetGroupArn"]), plan)
            else None
        )
    if kind == "validation_listener":
        listener = _listener_on_port(
            c.elbv2, plan.load_balancer_arn, int(request["port"])
        )
        if listener is None:
            return False
        return (
            True
            if transition_owned(_elb_tags(c.elbv2, listener["ListenerArn"]), plan)
            else None
        )
    if kind == "asg_attachment":
        groups = _groups_named(c.asg, request["asg_name"])
        if not groups:
            return None
        return request["target_group_arn"] in (groups[0].get("TargetGroupARNs") or ())
    if kind == "listener_default":
        current = route_snapshot(c.elbv2, resource_ref).target_group_arn
        if current == request["target_group_arn"]:
            return True
        if current == request["previous_target_group_arn"]:
            return False
        return None
    if kind == "listener":
        return _listener(c.elbv2, resource_ref) is not None
    if kind == "target_group_arn":
        return _target_group(c.elbv2, resource_ref) is not None
    if kind == "asg":
        groups = _groups_named(c.asg, resource_ref)
        return bool(groups) and "Delete" not in str(groups[0].get("Status") or "")
    if kind == "volume":
        return _exists(
            lambda: c.ec2.describe_volumes(VolumeIds=[resource_ref]), "Volumes"
        )
    if kind == "launch_template":
        return _exists(
            lambda: c.ec2.describe_launch_templates(LaunchTemplateIds=[resource_ref]),
            "LaunchTemplates",
        )
    if kind == "image":
        # 등록 해제는 State=deregistered로 잠시 보이다 빈 결과, 끝내 InvalidAMIID.NotFound가 된다 —
        # 셋 다 '해제됨'이다. 비활성(disabled) AMI는 기본 응답에서 빠지므로 함께 읽는다(아직 등록돼 있다).
        try:
            images = (
                c.ec2.describe_images(
                    ImageIds=[resource_ref], IncludeDisabled=True
                ).get("Images")
                or []
            )
        except ClientError as exc:
            if _not_found(exc):
                return False
            raise
        states = [i.get("State") for i in images]
        if None in states:
            return None  # 상태 없는 기록은 해제의 근거가 아니다 — 모름(관문을 풀지 않는다)
        return any(state != "deregistered" for state in states)
    if kind == "snapshot":
        return _exists(
            lambda: c.ec2.describe_snapshots(SnapshotIds=[resource_ref]), "Snapshots"
        )
    return None


def _exists(call: Callable[[], Any], key: str) -> bool:
    try:
        return bool(call().get(key))
    except ClientError as exc:
        if _not_found(exc):
            return False
        raise


# ------------------------------------------------------------------ 정리 재고(읽기 전용)
@dataclass
class Inventory:
    manifest: CleanupManifest
    blockers: list[str]
    retained: list[dict]
    unknown: list[dict]
    counts: dict[str, int]
    observed_at: datetime
    # 정리가 없앨 수 있는 인스턴스와 그 AWS 보안 보류 근거 — DB 격리 사실은 facade가 더한다
    at_risk_instances: list[str] = field(default_factory=list)
    security_holds: dict[str, list[str]] = field(default_factory=dict)
    destroys_instances: bool = True
    # 이 실행 시도들이 만든(소유 확인된) TG — 열린 격리의 복원 참조를 facade가 DB에서 대조한다
    target_groups: list[str] = field(default_factory=list)
    # TG → 그 TG를 복원 대상으로 가리키는 열린 격리 실행
    recovery_holds: dict[str, list[str]] = field(default_factory=dict)

    def hold_for_recovery(self, references: Mapping[str, list[str]]) -> None:
        """열린 격리의 불변 SG/TG 백업이 복원 대상으로 가리키는 TG는 지우지도 ASG에서 떼지도 않는다.

        UNISOLATE는 그 TG의 존재(같은 VPC)를 확인하고 재등록한다. 백업을 바꾸거나 격리를 풀지 않는다 —
        TG·부착을 매니페스트에서 빼 보존으로 옮기고, 남아 있는 동안 정리를 완료로 세지 않는다.
        """
        held = {arn: list(ids) for arn, ids in references.items() if ids}
        if not held:
            return

        def target_of(item: CleanupItem) -> str:
            return item.resource_id.split("|", 1)[-1]

        kept = [
            item
            for item in self.manifest.items
            if item.kind in ("target_group", "target_group_attachment")
            and target_of(item) in held
        ]
        self.manifest = self.manifest.model_copy(
            update={
                "items": tuple(item for item in self.manifest.items if item not in kept)
            }
        )
        self.retained += [
            {
                "kind": item.kind,
                "id": item.resource_id,
                "reason": f"열린 격리 {held[target_of(item)]}의 복원(UNISOLATE)이 이 TG를 씁니다 — 지우거나 떼지 않습니다",
            }
            for item in kept
        ]
        self.recovery_holds = held
        if self.destroys_instances:
            self.blockers.append(
                f"열린 격리의 복원이 이 실행의 TG {sorted(held)}를 씁니다 — 인스턴스를 없애는 정리는 하지 않습니다"
            )


_LIVE_INSTANCE = ("pending", "running", "stopping", "stopped", "shutting-down")


def cleanup_inventory(
    c: Clients,
    as_plan: AutoscalingExecutionPlan,
    plans: list[ServiceTransitionPlan],
    *,
    retention_decision: str | None,
) -> Inventory:
    """실행 소유 자원의 정확한 목록과 처분 순서. 자원을 바꾸지 않는다. 조회 실패는 예외로 올라간다.

    plans는 이 실행의 시도 전부(오래된 것부터)다 — 대체된 시도의 서비스 자원도 빠지지 않는다. 시도가
    없으면 #446 자원만 본다(부분 실패·고아 포함). 보존 결정 셋:
      retain_launch_image             — 시작 템플릿·AMI·스냅숏 보존
      retain_execution_infrastructure — 서비스 자원(검증 리스너·TG·부착)만 정리, 인스턴스는 없애지 않는다
    결정이 없으면 차단 사유다. 인스턴스를 없애는 결정이면 보안 보류 인스턴스가 차단 사유다.
    """
    region, account = as_plan.target_arn.split(":")[3:5]
    blockers = [
        f"세션 확인 실패: {r.detail}"
        for r in session_checks(c, account_id=account, region=region)
        if not r.passed
    ]
    if retention_decision is None:
        blockers.append(
            "보존 결정(retention_decision)이 없습니다 — 운영자 공급값이며 추정하지 않습니다"
        )
    retain_infra = retention_decision == "retain_execution_infrastructure"
    retain_image = retain_infra or retention_decision == "retain_launch_image"
    owner: dict[str, str] = {}
    items: list[tuple[str, str, str]] = []
    retained: list[dict] = [
        {
            "kind": "instance",
            "id": as_plan.source.instance_id,
            "reason": "#446 원본 인스턴스 — 정리하지 않습니다",
        }
    ]
    unknown: list[dict] = []
    counts: dict[str, int] = {}

    groups = _groups_named(c.asg, as_plan.asg_name)
    group = groups[0] if groups else None
    members: set[str] = set()
    if group is not None and not _owned(_tags(group.get("Tags")), as_plan):
        unknown.append(
            {
                "kind": "auto_scaling_group",
                "id": as_plan.asg_name,
                "reason": "소유 태그 불일치",
            }
        )
        group = None
    tg_arns: list[str] = []
    for plan in plans:
        tg_arn: str | None = None
        retained += [
            {
                "kind": "load_balancer",
                "id": plan.load_balancer_arn,
                "reason": "공유 ALB — 지우지 않습니다",
            },
            {
                "kind": "listener",
                "id": plan.original_route.listener_arn,
                "reason": "운영 리스너 — 지우지 않습니다",
            },
            {
                "kind": "target_group",
                "id": plan.original_route.target_group_arn,
                "reason": "원래 TG — 지우지 않습니다",
            },
        ]
        target_group = _target_group_named(c.elbv2, plan.target_group_name)
        if target_group is not None:
            if transition_owned(
                _elb_tags(c.elbv2, target_group["TargetGroupArn"]), plan
            ):
                tg_arn = target_group["TargetGroupArn"]
            else:
                unknown.append(
                    {
                        "kind": "target_group",
                        "id": target_group["TargetGroupArn"],
                        "reason": "소유 태그 불일치",
                    }
                )
        listener = _listener_on_port(
            c.elbv2, plan.load_balancer_arn, plan.validation_listener_port
        )
        listener_arn = None
        if listener is not None:
            listener_tags = _elb_tags(c.elbv2, listener["ListenerArn"])
            if transition_owned(listener_tags, plan):
                listener_arn = listener["ListenerArn"]
                items.append(("validation_listener", listener_arn, "delete_listener"))
                owner[listener_arn] = plan.transition_id
            elif not any(transition_owned(listener_tags, other) for other in plans):
                retained.append(
                    {
                        "kind": "listener",
                        "id": listener["ListenerArn"],
                        "reason": "검증 포트의 남의 리스너",
                    }
                )
        production = route_snapshot(c.elbv2, plan.original_route.listener_arn)
        if tg_arn is not None:
            if production.target_group_arn == tg_arn:
                blockers.append(
                    "운영 리스너가 전환 TG로 서비스 중입니다 — return 뒤에만 정리합니다"
                )
            foreign_lbs = [
                a
                for a in target_group.get("LoadBalancerArns") or ()
                if a != plan.load_balancer_arn
            ]
            if foreign_lbs:
                blockers.append(f"전환 TG를 다른 ALB가 참조합니다: {foreign_lbs}")
            for other in _load_balancer_listeners(c.elbv2, plan.load_balancer_arn):
                if other.get("ListenerArn") in (
                    listener_arn,
                    plan.original_route.listener_arn,
                ):
                    continue
                refs = [other.get("DefaultActions")] + [
                    r.get("Actions")
                    for r in _non_default_rules(c.elbv2, other["ListenerArn"])
                ]
                if tg_arn in repr(refs):
                    blockers.append(
                        f"리스너 {other.get('ListenerArn')}가 전환 TG를 참조합니다"
                    )
            if production.non_default_rules and tg_arn in repr(
                [
                    r.get("Actions")
                    for r in _non_default_rules(
                        c.elbv2, plan.original_route.listener_arn
                    )
                ]
            ):
                blockers.append("운영 리스너 규칙이 전환 TG를 참조합니다")
            tg_arns.append(tg_arn)
            owner[tg_arn] = owner[f"{as_plan.asg_name}|{tg_arn}"] = plan.transition_id
        counts["original_route_members"] = len(
            _target_states(c.elbv2, plan.original_route.target_group_arn)
        )
    if group is not None:
        members = {str(m.get("InstanceId")) for m in group.get("Instances") or ()}
        attached = list(group.get("TargetGroupARNs") or ())
        foreign = [a for a in attached if a not in tg_arns] + list(
            group.get("LoadBalancerNames") or ()
        )
        if foreign:
            blockers.append(
                f"ASG가 이 실행의 시도 밖의 부하 분산 대상에 부착돼 있습니다: {foreign}"
            )
        for tg_arn in tg_arns:
            if tg_arn in attached:
                items.append(
                    (
                        "target_group_attachment",
                        f"{as_plan.asg_name}|{tg_arn}",
                        "detach_target_group",
                    )
                )
    for tg_arn in tg_arns:
        items.append(("target_group", tg_arn, "delete_target_group"))
    if group is not None:
        deleting = "Delete" in str(group.get("Status") or "")
        items.append(
            (
                "auto_scaling_group",
                as_plan.asg_name,
                "await_instance_termination"
                if deleting
                else "delete_auto_scaling_group",
            )
        )
    counts["asg_members"] = len(members)

    tagged = _tagged_instances(c.ec2, as_plan.execution_id)
    alive = {
        i
        for i, item in tagged.items()
        if (item.get("State") or {}).get("Name") in _LIVE_INSTANCE
    }
    for instance_id in sorted(alive):
        if instance_id not in members and group is not None:
            unknown.append(
                {
                    "kind": "instance",
                    "id": instance_id,
                    "reason": "이 실행 태그가 있으나 ASG 멤버가 아님",
                }
            )
        items.append(("instance", instance_id, "await_instance_termination"))
    counts["live_execution_instances"] = len(alive)
    at_risk, holds = security_holds(c, as_plan)
    if holds and not retain_infra:
        blockers.append(
            f"보안 보류 인스턴스가 있어 없애지 않습니다(보호 해제·격리 해제를 하지 않습니다): {sorted(holds)}"
        )

    volumes = _pages(
        c.ec2.describe_volumes,
        "Volumes",
        Filters=[
            {
                "Name": f"tag:{EXECUTION_TAG_KEY}",
                "Values": [escape_filter_value(as_plan.execution_id)],
            }
        ],
    )
    for volume in volumes:
        if not _owned(_tags(volume.get("Tags")), as_plan):
            unknown.append(
                {
                    "kind": "volume",
                    "id": volume.get("VolumeId"),
                    "reason": "소유 태그 불완전",
                }
            )
            continue
        action = (
            "delete_volume"
            if volume.get("State") == "available"
            else "await_instance_termination"
        )
        items.append(("volume", volume["VolumeId"], action))

    templates = _launch_templates_named(c.ec2, as_plan.launch_template_name)
    template_ids: list[str] = []
    template_names: list[str] = []
    for template in templates:
        if _owned(_tags(template.get("Tags")), as_plan):
            template_ids.append(template["LaunchTemplateId"])
            template_names.append(
                template.get("LaunchTemplateName") or as_plan.launch_template_name
            )
            items.append(
                (
                    "launch_template",
                    template["LaunchTemplateId"],
                    "delete_launch_template",
                )
            )
        else:
            unknown.append(
                {
                    "kind": "launch_template",
                    "id": template.get("LaunchTemplateId"),
                    "reason": "소유 태그 불일치",
                }
            )

    mapped: set[str] = set()
    image_id: str | None = None
    for image in _images_named(c.ec2, as_plan.image_name):
        if image.get("OwnerId") == account and _owned(
            _tags(image.get("Tags")), as_plan
        ):
            # 해제가 확정된 소유 AMI의 매핑도 스냅숏 출처로 읽는다(태그 없는 매핑 스냅숏을 놓치지 않는다)
            mapped |= {
                str(m["Ebs"]["SnapshotId"])
                for m in image.get("BlockDeviceMappings") or ()
                if isinstance(m.get("Ebs"), Mapping) and m["Ebs"].get("SnapshotId")
            }
            if image.get("State") == "deregistered":
                continue  # 등록 해제가 확정됐다 — 처분할 것이 없고 다시 보내지 않는다
            image_id = image["ImageId"]
            items.append(("image", image_id, "deregister_image"))
        else:
            unknown.append(
                {
                    "kind": "image",
                    "id": image.get("ImageId"),
                    "reason": "소유자·소유 태그 불일치",
                }
            )
    if image_id is not None:
        others = _external_image_instances(c.ec2, image_id, tagged)
        if others and not retain_image:
            blockers.append(
                f"이 실행 밖의 인스턴스가 AMI {image_id}로 떠 있습니다: {others}"
            )
    if not retain_image and (image_id is not None or template_ids):
        blockers += external_launch_refs(
            c, as_plan, image_id, template_ids, template_names
        )

    tagged_snapshots = {
        s["SnapshotId"]: s
        for s in _pages(
            c.ec2.describe_snapshots,
            "Snapshots",
            OwnerIds=["self"],
            Filters=[
                {
                    "Name": f"tag:{EXECUTION_TAG_KEY}",
                    "Values": [escape_filter_value(as_plan.execution_id)],
                }
            ],
        )
    }
    # 태그로 못 찾은 매핑 스냅숏은 하나씩 읽는다 — ID 하나라도 없으면 호출 전체가 NotFound다. 지워졌다는
    # 확정 응답(해제된 AMI 기록에 매핑만 남음)은 처분할 것이 없고, 응답에 없으면 아래에서 모름으로 남긴다.
    deleted: set[str] = set()
    for snapshot_id in sorted(mapped - set(tagged_snapshots)):
        try:
            found = (
                c.ec2.describe_snapshots(
                    OwnerIds=["self"], SnapshotIds=[snapshot_id]
                ).get("Snapshots")
                or []
            )
        except ClientError as exc:
            if not _not_found(exc):
                raise
            deleted.add(snapshot_id)
            continue
        for s in found:
            tagged_snapshots[s["SnapshotId"]] = s
    snapshot_ids = sorted(tagged_snapshots)
    for snapshot_id in snapshot_ids:
        if _owned(_tags(tagged_snapshots[snapshot_id].get("Tags")), as_plan):
            items.append(("snapshot", snapshot_id, "delete_snapshot"))
        else:
            unknown.append(
                {
                    "kind": "snapshot",
                    "id": snapshot_id,
                    "reason": "소유 태그 불일치(AMI 매핑)",
                }
            )
    for snapshot_id in sorted(mapped - set(tagged_snapshots) - deleted):
        unknown.append(
            {
                "kind": "snapshot",
                "id": snapshot_id,
                "reason": "AMI 매핑 스냅숏을 조회하지 못함",
            }
        )
    if snapshot_ids:
        foreign = [
            i for i in _snapshot_image_users(c.ec2, snapshot_ids) if i != image_id
        ]
        if foreign and not retain_image:
            blockers.append(f"다른 AMI가 이 실행의 스냅숏을 씁니다: {foreign}")

    if unknown:
        blockers.append(
            f"소유를 확인하지 못한 자원 {len(unknown)}건 — 정리하지 않고 사람이 확인합니다"
        )
    kept_kinds = ("launch_template", "image", "snapshot")
    if retain_infra:
        kept_kinds += ("auto_scaling_group", "instance", "volume")
    if retain_image:
        kept = [i for i in items if i[0] in kept_kinds]
        items = [i for i in items if i not in kept]
        retained += [
            {
                "kind": k,
                "id": rid,
                "reason": f"운영자 보존 결정({retention_decision}) — {RETAINED_BILLING_WARNING}",
            }
            for k, rid, _ in kept
        ]
    order = {
        k: n
        for n, k in enumerate(
            (
                "validation_listener",
                "target_group_attachment",
                "target_group",
                "auto_scaling_group",
                "instance",
                "volume",
                "launch_template",
                "image",
                "snapshot",
            ),
            start=1,
        )
    }
    manifest = CleanupManifest(
        execution_id=as_plan.execution_id,
        transition_id=plans[-1].transition_id if plans else None,
        items=tuple(
            CleanupItem(
                order=order[kind],
                kind=kind,
                resource_id=rid,
                action=action,
                transition_id=owner.get(rid),
            )
            for kind, rid, action in sorted(items, key=lambda x: (order[x[0]], x[1]))
        ),
    )
    retained = list({(r["kind"], r["id"]): r for r in retained}.values())
    return Inventory(
        manifest,
        blockers,
        retained,
        unknown,
        counts,
        _now(),
        at_risk_instances=at_risk,
        security_holds=holds,
        destroys_instances=not retain_infra,
        target_groups=tg_arns,
    )


def cleanup_gate(
    c: Clients, item: CleanupItem, as_plan: AutoscalingExecutionPlan
) -> str | None:
    """항목을 지금 처분해도 되는가 — 바로 앞에서 다시 읽는다. 기다릴 것이 있으면 사유."""
    if item.action == "delete_target_group":
        group = _target_group(c.elbv2, item.resource_id)
        if group is not None and group.get("LoadBalancerArns"):
            return f"TG {item.resource_id}를 아직 리스너·규칙이 참조합니다: {group['LoadBalancerArns']}"
        if _target_states(c.elbv2, item.resource_id):
            return f"TG {item.resource_id} 대상이 아직 빠지는 중(draining)입니다"
        groups = _groups_named(c.asg, as_plan.asg_name)
        if groups and item.resource_id in (groups[0].get("TargetGroupARNs") or ()):
            return "ASG에서 TG 분리가 아직 끝나지 않았습니다"
    if item.action == "delete_auto_scaling_group":
        groups = _groups_named(c.asg, as_plan.asg_name)
        if groups and (
            groups[0].get("TargetGroupARNs") or groups[0].get("LoadBalancerNames")
        ):
            return "ASG가 아직 부하 분산 대상에 부착돼 있습니다"
    if item.action == "await_instance_termination" or item.action in (
        "delete_volume",
        "delete_launch_template",
        "deregister_image",
        "delete_snapshot",
    ):
        groups = _groups_named(c.asg, as_plan.asg_name)
        alive = [
            i
            for i, v in _tagged_instances(c.ec2, as_plan.execution_id).items()
            if (v.get("State") or {}).get("Name") in _LIVE_INSTANCE
        ]
        if alive:
            return f"이 실행의 인스턴스 종료 확인 전입니다: {sorted(alive)}"
        if (
            item.action != "await_instance_termination"
            and item.action != "delete_volume"
            and groups
        ):
            return f"ASG {as_plan.asg_name} 삭제 확인 전입니다 — 시작 템플릿·AMI·스냅숏을 유지합니다"
    if item.action == "delete_volume":
        volumes = (
            c.ec2.describe_volumes(VolumeIds=[item.resource_id]).get("Volumes") or []
        )
        if not volumes or volumes[0].get("State") != "available":
            return f"볼륨 {item.resource_id}가 available이 아닙니다"
    return None
