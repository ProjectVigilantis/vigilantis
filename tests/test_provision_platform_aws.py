# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# scripts/provision_platform_aws.py의 정의(정책·규칙·컨테이너·배포 설정) 회귀 테스트입니다. (ADR-0010)
#
# AWS를 부르지 않는다(boto3 자리는 가짜 객체). 지키는 것은 "SaaS 모양"을 이루는 울타리 다섯 묶음과
# 구성 절차의 가드다.
#   ① api 태스크 역할은 접근 역할 전환 하나만 — AWS 자원을 직접 만지지 않는다
#   ② 접근 역할은 플랫폼 태스크 역할만, ExternalId가 맞을 때만 받는다
#   ③ ALB는 비밀 헤더가 있는 요청만 앱으로 보낸다(CloudFront 우회 차단)
#   ④ WAF는 기본 차단 · 허용 IP만 통과 · 허용 CIDR은 /24보다 넓으면 거부
#   ⑤ api는 겹쳐 뜨지 않는다(min 0 · max 100) · AI 분석·조치 실행 디스패치는 명시해야 켜진다
#      · 비밀은 값이 아니라 참조로만
#   가드: 실 AWS·계정 대조 · 스모크 환경을 비싼 자원보다 먼저 확인 · DB 비밀번호는 RDS 생성 전에 저장
#         · 플랫폼 태그 없는 IAM 역할은 덮어쓰거나 지우지 않음
#         · 자동 롤백된 배포를 성공으로 보고하지 않음 · ECS가 전부 내려간 뒤에만 클러스터 삭제
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다.
# ==============================================================================

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


platform = _load("provision_platform_aws")
smoke = _load("provision_smoke_aws")

ACCOUNT = "111122223333"


# ① ---------------------------------------------------------------------------
def test_task_role_can_only_assume_the_access_role():
    statements = platform.task_role_policy(ACCOUNT)["Statement"]
    assert len(statements) == 1
    (only,) = statements
    assert only["Effect"] == "Allow"
    assert only["Action"] == "sts:AssumeRole"
    assert only["Resource"] == f"arn:aws:iam::{ACCOUNT}:role/{platform.ROLE_ACCESS}"
    # 와일드카드가 끼면 플랫폼이 다른 역할(관리자 등)로도 갈아탈 수 있다
    assert "*" not in json.dumps(platform.task_role_policy(ACCOUNT))


# ② ---------------------------------------------------------------------------
def test_access_role_trusts_only_the_platform_task_role_with_external_id():
    (stmt,) = platform.access_trust_policy(ACCOUNT, "ext-123")["Statement"]
    assert stmt["Principal"] == {"AWS": f"arn:aws:iam::{ACCOUNT}:role/{platform.ROLE_TASK}"}
    assert stmt["Action"] == "sts:AssumeRole"
    assert stmt["Condition"] == {"StringEquals": {"sts:ExternalId": "ext-123"}}


def test_access_role_borrows_the_smoke_whitelist_policy():
    """권한 정책을 두 벌 두지 않는다 — 스모크 테스트의 두 방향 보증이 그 한 벌에 걸려 있다."""
    assert platform.SMOKE_POLICY_NAME == smoke.APP_POLICY
    assert platform.SMOKE_VPC_NAME == smoke.VPC_NAME


def test_exec_role_reads_only_the_given_secrets():
    arns = ["arn:b", "arn:a"]
    (stmt,) = platform.exec_secrets_policy(arns)["Statement"]
    assert stmt["Action"] == "secretsmanager:GetSecretValue"
    assert stmt["Resource"] == ["arn:a", "arn:b"]


# ③ ---------------------------------------------------------------------------
def test_every_listener_rule_requires_the_origin_header():
    rules = platform.listener_rules("tg-api", "tg-web", "secret-value")
    assert [r["Priority"] for r in rules] == [10, 20]
    for rule in rules:
        headers = [c for c in rule["Conditions"] if c["Field"] == "http-header"]
        assert headers == [{"Field": "http-header", "HttpHeaderConfig": {
            "HttpHeaderName": platform.ORIGIN_HEADER, "Values": ["secret-value"]}}]


def test_api_paths_go_to_api_and_the_rest_to_web():
    api_rule, web_rule = platform.listener_rules("tg-api", "tg-web", "v")
    paths = next(c for c in api_rule["Conditions"] if c["Field"] == "path-pattern")["Values"]
    assert set(paths) == {"/api/*", "/health"}  # /api/v1/ws(WebSocket)도 /api/* 안이다
    assert api_rule["Actions"] == [{"Type": "forward", "TargetGroupArn": "tg-api"}]
    assert web_rule["Actions"] == [{"Type": "forward", "TargetGroupArn": "tg-web"}]


def test_cloudfront_sends_the_origin_header_and_does_not_cache():
    cfg = platform.distribution_config("alb.example", "secret-value", "arn:acl", "ref")
    (origin,) = cfg["Origins"]["Items"]
    assert origin["CustomHeaders"]["Items"] == [{"HeaderName": platform.ORIGIN_HEADER, "HeaderValue": "secret-value"}]
    behavior = cfg["DefaultCacheBehavior"]
    assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"
    assert behavior["CachePolicyId"] == platform.CF_CACHING_DISABLED
    assert set(behavior["AllowedMethods"]["Items"]) >= {"GET", "POST", "PUT", "PATCH", "DELETE"}
    assert cfg["WebACLId"] == "arn:acl"
    assert cfg["Comment"] == platform.CF_COMMENT


# ④ ---------------------------------------------------------------------------
def test_waf_allows_only_the_ip_set():
    (rule,) = platform.waf_rules("arn:ipset")
    assert rule["Statement"] == {"IPSetReferenceStatement": {"ARN": "arn:ipset"}}
    assert rule["Action"] == {"Allow": {}}


def test_allow_cidr_accepts_narrow_ipv4_ranges():
    assert platform.validate_allow_cidrs(["203.0.113.7/32", "198.51.100.0/24"]) == [
        "203.0.113.7/32", "198.51.100.0/24"]


@pytest.mark.parametrize("raw, reason", [
    ("0.0.0.0/0", "넓은 범위"),
    ("0.0.0.0/1", "넓은 범위"),     # WAF가 통과시키는 광역 — 출입문이 무력화된다
    ("10.0.0.0/23", "넓은 범위"),
    ("2001:db8::/128", "IPv4만"),
    ("203.0.113.7/24", "올바른 CIDR"),  # 호스트 비트 — 오타로 본다
    ("203.0.113.7", None),              # 접두 없으면 /32 — 받는다
    ("not-an-ip", "올바른 CIDR"),
])
def test_allow_cidr_rejects_wide_or_malformed_ranges(raw, reason):
    if reason is None:
        assert platform.validate_allow_cidrs([raw]) == ["203.0.113.7/32"]
        return
    with pytest.raises(SystemExit, match=reason):
        platform.validate_allow_cidrs([raw])


# ⑤ ---------------------------------------------------------------------------
def test_api_service_never_runs_two_tasks_during_deploy():
    api = platform.service_deployment(platform.API_SERVICE)
    assert (api["minimumHealthyPercent"], api["maximumPercent"]) == (0, 100)
    web = platform.service_deployment(platform.WEB_SERVICE)
    assert web["maximumPercent"] > 100  # web은 상태가 없어 기본 롤링



API_V2 = "arn:aws:ecs:ap-northeast-2:1:task-definition/vigilantis-platform-api:2"
API_V1 = "arn:aws:ecs:ap-northeast-2:1:task-definition/vigilantis-platform-api:1"
WEB_V2 = "arn:aws:ecs:ap-northeast-2:1:task-definition/vigilantis-platform-web:2"


def _svc(name: str, task_def: str, rollout: str = "COMPLETED", **extra) -> dict:
    primary = {"status": "PRIMARY", "taskDefinition": task_def, "rolloutState": rollout, **extra}
    return {"serviceName": name, "deployments": [primary]}


def test_deploy_succeeds_only_on_the_requested_task_definitions():
    services = [_svc(platform.API_SERVICE, API_V2), _svc(platform.WEB_SERVICE, WEB_V2)]
    assert platform.deployment_problems(
        services, {platform.API_SERVICE: API_V2, platform.WEB_SERVICE: WEB_V2}) == []


def test_rolled_back_deploy_is_a_failure_even_though_the_service_is_stable():
    """서킷 브레이커 롤백 뒤에도 services_stable은 통과한다 — PRIMARY가 이전 버전이면 실패다."""
    services = [_svc(platform.API_SERVICE, API_V1), _svc(platform.WEB_SERVICE, WEB_V2)]
    (problem,) = platform.deployment_problems(
        services, {platform.API_SERVICE: API_V2, platform.WEB_SERVICE: WEB_V2})
    assert platform.API_SERVICE in problem and "롤백" in problem


@pytest.mark.parametrize("rollout", ["IN_PROGRESS", "FAILED"])
def test_unfinished_rollout_is_a_failure(rollout):
    services = [_svc(platform.API_SERVICE, API_V2, rollout, rolloutStateReason="circuit breaker"),
                _svc(platform.WEB_SERVICE, WEB_V2)]
    (problem,) = platform.deployment_problems(
        services, {platform.API_SERVICE: API_V2, platform.WEB_SERVICE: WEB_V2})
    assert rollout in problem


def test_missing_service_is_a_failure():
    (problem,) = platform.deployment_problems(
        [_svc(platform.API_SERVICE, API_V2)], {platform.API_SERVICE: API_V2, platform.WEB_SERVICE: WEB_V2})
    assert platform.WEB_SERVICE in problem


class DrainingEcs:
    """폴링마다 다음 상태를 돌려주는 가짜 ECS — 서비스·태스크가 서로 다른 시점에 내려가는 경우를 만든다.
    한 번의 폴링은 describe_services로 시작한다(_ecs_drained의 호출 순서)."""

    def __init__(self, timeline: list[tuple[list[dict], list[dict]]]):
        self._timeline = timeline
        self.polls = 0

    def _state(self):
        return self._timeline[min(self.polls - 1, len(self._timeline) - 1)]

    def describe_services(self, cluster, services):
        self.polls += 1
        return {"services": [s for s in self._state()[0] if s["serviceName"] in services]}

    def list_tasks(self, cluster, desiredStatus):
        return {"taskArns": [t["taskArn"] for t in self._state()[1] if t["desired"] == desiredStatus]}

    def describe_tasks(self, cluster, tasks):
        return {"tasks": [t for t in self._state()[1] if t["taskArn"] in tasks]}


def _s(name, status):
    return {"serviceName": name, "status": status}


def _t(arn, desired, last):
    return {"taskArn": f"arn:aws:ecs:r:1:task/c/{arn}", "desired": desired, "lastStatus": last}


API, WEB = platform.API_SERVICE, platform.WEB_SERVICE


def test_drain_waits_for_both_services_and_their_stopping_tasks():
    """api가 먼저 INACTIVE가 돼도, web이 남았거나 STOPPING 태스크가 남았으면 클러스터를 지우지 않는다."""
    ecs = DrainingEcs([
        ([_s(API, "INACTIVE"), _s(WEB, "DRAINING")], [_t("a", "STOPPED", "STOPPING"), _t("w", "RUNNING", "RUNNING")]),
        ([_s(API, "INACTIVE"), _s(WEB, "INACTIVE")], [_t("a", "STOPPED", "STOPPED"), _t("w", "STOPPED", "STOPPING")]),
        ([_s(API, "INACTIVE"), _s(WEB, "INACTIVE")], [_t("a", "STOPPED", "STOPPED"), _t("w", "STOPPED", "STOPPED")]),
    ])
    assert platform._ecs_drained(ecs, [API, WEB], delay=0, attempts=5, sleep=lambda _: None)
    assert ecs.polls == 3  # 앞의 두 상태에서는 끝났다고 보지 않는다


def test_drain_waits_for_the_second_service_even_when_every_task_has_stopped():
    """services_inactive가 놓치던 바로 그 상태 — api만 INACTIVE이고 태스크는 전부 STOPPED여도 web이 남았으면 기다린다."""
    ecs = DrainingEcs([
        ([_s(API, "INACTIVE"), _s(WEB, "DRAINING")], [_t("a", "STOPPED", "STOPPED"), _t("w", "STOPPED", "STOPPED")]),
        ([_s(API, "INACTIVE"), _s(WEB, "INACTIVE")], [_t("a", "STOPPED", "STOPPED"), _t("w", "STOPPED", "STOPPED")]),
    ])
    assert platform._ecs_drained(ecs, [API, WEB], delay=0, attempts=5, sleep=lambda _: None)
    assert ecs.polls == 2


def test_drain_gives_up_without_claiming_success():
    ecs = DrainingEcs([([_s(API, "INACTIVE")], [_t("a", "STOPPED", "STOPPING")])])
    assert not platform._ecs_drained(ecs, [API], delay=0, attempts=3, sleep=lambda _: None)


def test_remaining_ignores_only_finished_things():
    assert platform.ecs_remaining([_s(API, "INACTIVE")], [_t("a", "STOPPED", "STOPPED")]) == []
    assert platform.ecs_remaining([_s(API, "ACTIVE")], []) == [f"서비스 {API}(ACTIVE)"]
    assert platform.ecs_remaining([], [_t("a", "STOPPED", "DEPROVISIONING")]) == ["태스크 a(DEPROVISIONING)"]


def _api(dispatch: bool = False) -> dict:
    return platform.api_container(
        "repo/api:t", region="ap-northeast-2", cors_origin="https://d.cloudfront.net",
        access_role_arn="arn:aws:iam::1:role/vigilantis-access", collect_vpc_ids="vpc-1",
        dispatch_enabled=dispatch,
        secrets_arns={"DATABASE_URL": "arn:s1", "OPENAI_API_KEY": "arn:s2", "VIGILANTIS_ACCESS_EXTERNAL_ID": "arn:s3"})


def test_dispatch_is_off_unless_asked():
    """DISPATCH_ENABLED는 AI 디스패처와 조치 실행 디스패처가 공유한다(config.py) — 기본 배포는 둘 다 끈다."""
    env = {e["name"]: e["value"] for e in _api()["environment"]}
    assert env["DISPATCH_ENABLED"] == "false"
    env_on = {e["name"]: e["value"] for e in _api(dispatch=True)["environment"]}
    assert env_on["DISPATCH_ENABLED"] == "true"


def test_api_container_carries_secrets_only_by_reference():
    container = _api()
    env = {e["name"]: e["value"] for e in container["environment"]}
    # 정적 키·비밀 값이 평문 환경변수로 실리지 않는다
    for forbidden in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "OPENAI_API_KEY",
                      "VIGILANTIS_ACCESS_EXTERNAL_ID", "AWS_ENDPOINT_URL"):
        assert forbidden not in env
    assert {s["name"] for s in container["secrets"]} == {
        "DATABASE_URL", "OPENAI_API_KEY", "VIGILANTIS_ACCESS_EXTERNAL_ID"}
    assert env["COLLECT_VPC_IDS"] == "vpc-1"
    assert env["LOG_FILE_ENABLED"] == "false"
    assert env["CORS_ALLOW_ORIGINS"] == "https://d.cloudfront.net"


# 가드 ------------------------------------------------------------------------
def test_refuses_to_run_against_localstack(monkeypatch):
    monkeypatch.setattr(platform, "endpoint_url", lambda: "http://localhost:4566")
    with pytest.raises(SystemExit, match="실 AWS 전용"):
        platform._require_real_aws(ACCOUNT)


def test_refuses_a_different_account(monkeypatch):
    class _Sts:
        def get_caller_identity(self):
            return {"Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/x"}

    monkeypatch.setattr(platform, "endpoint_url", lambda: None)
    monkeypatch.setattr(platform, "aws_client", lambda *a, **k: _Sts())
    with pytest.raises(SystemExit, match="호출 주체의 계정"):
        platform._require_real_aws(ACCOUNT)


# 구성 절차 가드 — boto3 자리는 아래 가짜 객체 ------------------------------------
def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


_PLATFORM_TAGS = [{"Key": platform.TAG_KEY, "Value": platform.TAG_VALUE}]
_SMOKE_POLICY = f"arn:aws:iam::{ACCOUNT}:policy/{platform.SMOKE_POLICY_NAME}"


class FakeIam:
    def __init__(self, roles=None, policies=()):
        self.roles = {name: {"RoleName": name, "Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}", "Tags": tags}
                      for name, tags in (roles or {}).items()}
        self.policies = set(policies)
        self.calls: list[tuple[str, str]] = []

    def get_role(self, RoleName):
        if RoleName not in self.roles:
            raise _client_error("NoSuchEntity", "GetRole")
        return {"Role": self.roles[RoleName]}

    def get_policy(self, PolicyArn):
        if PolicyArn not in self.policies:
            raise _client_error("NoSuchEntity", "GetPolicy")
        return {"Policy": {"Arn": PolicyArn}}

    def update_assume_role_policy(self, RoleName, PolicyDocument):
        self.calls.append(("update_assume_role_policy", RoleName))

    def detach_role_policy(self, RoleName, PolicyArn):
        self.calls.append(("detach_role_policy", RoleName))

    def delete_role_policy(self, RoleName, PolicyName):
        self.calls.append(("delete_role_policy", RoleName))

    def delete_role(self, RoleName):
        self.calls.append(("delete_role", RoleName))
        del self.roles[RoleName]


class RecordingEc2:
    """describe_vpcs만 답한다 — 그 밖의 호출은 기록만 하고 AttributeError로 멈춘다."""

    def __init__(self, smoke_vpc=None):
        self.smoke_vpc = smoke_vpc
        self.calls: list[str] = []

    def describe_vpcs(self, Filters):
        self.calls.append("describe_vpcs")
        return {"Vpcs": [{"VpcId": self.smoke_vpc}] if self.smoke_vpc else []}

    def __getattr__(self, name):
        self.calls.append(name)
        raise AttributeError(name)


def test_existing_role_without_platform_tag_is_not_overwritten():
    iam = FakeIam(roles={platform.ROLE_ACCESS: [{"Key": "owner", "Value": "someone-else"}]})
    with pytest.raises(SystemExit, match="플랫폼 태그"):
        platform._ensure_role(iam, platform.ROLE_ACCESS, {"Statement": []}, "d")
    assert iam.calls == []  # 신뢰 정책을 덮어쓰지 않았다


def test_existing_platform_role_gets_its_trust_policy_refreshed():
    iam = FakeIam(roles={platform.ROLE_ACCESS: _PLATFORM_TAGS})
    arn = platform._ensure_role(iam, platform.ROLE_ACCESS, {"Statement": []}, "d")
    assert arn.endswith(f":role/{platform.ROLE_ACCESS}")
    assert iam.calls == [("update_assume_role_policy", platform.ROLE_ACCESS)]


def test_down_deletes_only_platform_tagged_roles():
    iam = FakeIam(roles={platform.ROLE_ACCESS: [], platform.ROLE_TASK: _PLATFORM_TAGS})
    platform._delete_roles(iam, ACCOUNT)
    assert set(iam.roles) == {platform.ROLE_ACCESS}  # 태그 없는 같은 이름 역할은 남는다
    assert iam.calls and all(role == platform.ROLE_TASK for _, role in iam.calls)


def test_up_stops_before_creating_anything_when_the_smoke_policy_is_missing():
    ec2 = RecordingEc2(smoke_vpc="vpc-smoke")
    with pytest.raises(SystemExit, match=platform.SMOKE_POLICY_NAME):
        platform.up({"iam": FakeIam(), "ec2": ec2}, ACCOUNT, "ap-northeast-2", ["203.0.113.7/32"])
    assert ec2.calls == []  # VPC·NAT·RDS 어느 것도 만들지 않았다


def test_up_stops_before_creating_anything_when_the_smoke_vpc_is_missing():
    ec2 = RecordingEc2(smoke_vpc=None)
    with pytest.raises(SystemExit, match="스모크 VPC"):
        platform.up({"iam": FakeIam(policies=[_SMOKE_POLICY]), "ec2": ec2}, ACCOUNT, "ap-northeast-2",
                    ["203.0.113.7/32"])
    assert ec2.calls == ["describe_vpcs"]


def _fake_main(monkeypatch, argv: list[str], fakes: dict) -> None:
    monkeypatch.setattr(platform, "_require_real_aws", lambda account: {"Account": account})
    monkeypatch.setattr(platform, "default_region", lambda: "ap-northeast-2")
    monkeypatch.setattr(platform, "aws_client", lambda name, region=None: fakes.get(name, object()))
    monkeypatch.setattr(sys, "argv", ["provision_platform_aws.py", *argv])


def test_up_preview_also_checks_the_smoke_environment(monkeypatch):
    """--yes 없는 미리 보기에서 먼저 알려야 --yes 실행이 중간에 멈추지 않는다."""
    _fake_main(monkeypatch, ["up", "--account", ACCOUNT, "--allow-cidr", "203.0.113.7/32"],
               {"iam": FakeIam(), "ec2": RecordingEc2(smoke_vpc="vpc-smoke")})
    with pytest.raises(SystemExit, match=platform.SMOKE_POLICY_NAME):
        platform.main()


def test_up_rejects_a_wide_cidr_before_touching_aws(monkeypatch):
    iam = FakeIam(policies=[_SMOKE_POLICY])
    _fake_main(monkeypatch, ["up", "--account", ACCOUNT, "--allow-cidr", "0.0.0.0/1", "--yes"], {"iam": iam})
    with pytest.raises(SystemExit, match="넓은 범위"):
        platform.main()


class FakeSm:
    def __init__(self, events, values=None):
        self.events = events
        self.values = dict(values or {})

    def describe_secret(self, SecretId):
        if SecretId not in self.values:
            raise _client_error("ResourceNotFoundException", "DescribeSecret")
        return {"ARN": f"arn:secret:{SecretId}"}

    def create_secret(self, Name, SecretString, Tags):
        self.events.append(("create_secret", Name))
        self.values[Name] = SecretString
        return {"ARN": f"arn:secret:{Name}"}

    def get_secret_value(self, SecretId):
        return {"SecretString": self.values[SecretId]}

    def put_secret_value(self, SecretId, SecretString):
        self.events.append(("put_secret_value", SecretId))
        self.values[SecretId] = SecretString


class FakeRds:
    HOST = "vigilantis-platform-db.abc.ap-northeast-2.rds.amazonaws.com"

    def __init__(self, events, exists=False, wait_fails=False):
        self.events = events
        self.db = {"DBInstanceIdentifier": platform.DB_INSTANCE, "DBInstanceStatus": "creating"} if exists else None
        self.wait_fails = wait_fails
        self.created: dict = {}

    def describe_db_instances(self, DBInstanceIdentifier):
        if not self.db:
            raise _client_error("DBInstanceNotFound", "DescribeDBInstances")
        return {"DBInstances": [dict(self.db)]}

    def create_db_subnet_group(self, **kwargs):
        self.events.append(("create_db_subnet_group", None))

    def create_db_instance(self, **kwargs):
        self.events.append(("create_db_instance", None))
        self.created = kwargs
        self.db = {"DBInstanceIdentifier": platform.DB_INSTANCE, "DBInstanceStatus": "creating"}

    def describe_db_engine_versions(self, **kwargs):
        raise AssertionError("엔진 버전 목록을 조회하지 않는다 — 메이저만 넘긴다")

    def get_waiter(self, name):
        rds = self

        class _Waiter:
            def wait(self, **kwargs):
                rds.events.append(("wait", name))
                if rds.wait_fails:
                    raise KeyboardInterrupt  # 대기 중 Ctrl+C
                rds.db = {**rds.db, "DBInstanceStatus": "available", "Endpoint": {"Address": FakeRds.HOST}}
        return _Waiter()


_SUBNETS = {f"{platform.PREFIX}-private-{az}": f"subnet-{az}" for az in platform.PRIVATE_SUBNETS}


def test_db_password_is_stored_before_the_instance_is_requested():
    events: list = []
    rds, sm = FakeRds(events), FakeSm(events)
    platform._ensure_db(rds, sm, _SUBNETS, "sg-db")
    order = [e[0] for e in events]
    assert order.index("create_secret") < order.index("create_db_instance")
    assert rds.created["EngineVersion"] == platform.DB_MAJOR  # 메이저만 — RDS가 기본 마이너를 고른다
    url = urlsplit(sm.values[platform.SECRET_DATABASE_URL])
    assert url.hostname == FakeRds.HOST
    assert url.password == rds.created["MasterUserPassword"]


def test_db_password_survives_an_interrupted_wait():
    events: list = []
    sm = FakeSm(events)
    with pytest.raises(KeyboardInterrupt):
        platform._ensure_db(FakeRds(events, wait_fails=True), sm, _SUBNETS, "sg-db")
    pending = urlsplit(sm.values[platform.SECRET_DATABASE_URL])
    assert pending.hostname == platform.DB_HOST_PENDING and pending.password

    # 재실행 — 인스턴스는 생성 중이다. 새로 만들지 않고 같은 비밀번호로 호스트만 채운다
    events.clear()
    platform._ensure_db(FakeRds(events, exists=True), sm, _SUBNETS, "sg-db")
    assert "create_db_instance" not in [e[0] for e in events]
    filled = urlsplit(sm.values[platform.SECRET_DATABASE_URL])
    assert (filled.hostname, filled.password) == (FakeRds.HOST, pending.password)


def test_existing_db_without_its_secret_stops_instead_of_guessing():
    events: list = []
    with pytest.raises(SystemExit, match="비밀번호를 알 수 없"):
        platform._ensure_db(FakeRds(events, exists=True), FakeSm(events), _SUBNETS, "sg-db")
    assert events == []
