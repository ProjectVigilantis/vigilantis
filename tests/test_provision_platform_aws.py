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


# 사용자 도메인 — 웹 번들의 API 주소와 api의 CORS가 별칭을 따른다 ---------------------
def test_public_origins_put_the_alias_first_and_keep_the_default_domain():
    dist = {"DomainName": "d1.cloudfront.net", "Aliases": {"Quantity": 1, "Items": ["vigilantis.example"]}}
    # 첫 값이 빌드 인자 — 별칭이 아니면 별칭 주소로 연 화면이 기본 도메인의 WS로 붙어 Origin 검사에 걸린다
    assert platform.public_origins(dist) == ["https://vigilantis.example", "https://d1.cloudfront.net"]


def test_public_origins_without_alias_is_the_default_domain_only():
    assert platform.public_origins({"DomainName": "d1.cloudfront.net", "Aliases": {"Quantity": 0}}) == [
        "https://d1.cloudfront.net"]


def test_custom_domain_config_keeps_the_rest_of_the_distribution():
    base = platform.distribution_config("alb.example", "secret-value", "arn:acl", "ref")
    updated = platform.custom_domain_config(base, "vigilantis.example", "arn:cert")
    assert updated["Aliases"] == {"Quantity": 1, "Items": ["vigilantis.example"]}
    assert updated["ViewerCertificate"]["ACMCertificateArn"] == "arn:cert"
    assert updated["ViewerCertificate"]["SSLSupportMethod"] == "sni-only"
    assert {k: v for k, v in updated.items() if k not in ("Aliases", "ViewerCertificate")} == base
    assert "Aliases" not in base  # 원본을 고치지 않는다


@pytest.mark.parametrize("cert, covered", [
    ({"DomainName": "vigilantis.example"}, True),
    ({"DomainName": "other.example", "SubjectAlternativeNameSummaries": ["*.vigilantis.example"]}, False),
    ({"DomainName": "x.example", "SubjectAlternativeNameSummaries": ["*.example"]}, True),
    ({"DomainName": "other.example"}, False),
])
def test_certificate_must_cover_the_domain(cert, covered):
    assert platform.certificate_covers(cert, "vigilantis.example") is covered


def test_target_groups_drain_in_30_seconds_not_the_300_second_default():
    # api는 이전 태스크를 내린 뒤 띄우므로 이 대기가 그대로 중단 시간이 된다
    assert platform.target_group_attributes() == [
        {"Key": "deregistration_delay.timeout_seconds", "Value": "30"}]


def test_down_removes_only_alias_records_that_point_at_this_distribution():
    records = [
        {"Name": "vigilantis.example.", "Type": "A", "AliasTarget": {"DNSName": "d1.cloudfront.net."}},
        {"Name": "vigilantis.example.", "Type": "A", "AliasTarget": {"DNSName": "d2.cloudfront.net."}},
        {"Name": "vigilantis.example.", "Type": "NS", "ResourceRecords": []},
        {"Name": "www.vigilantis.example.", "Type": "A", "AliasTarget": {"DNSName": "d1.cloudfront.net."}},
    ]
    assert platform.alias_records_for(records, "vigilantis.example", "d1.cloudfront.net") == [records[0]]


# ④ ---------------------------------------------------------------------------
def test_waf_allows_only_the_ip_set():
    _, allow = platform.waf_rules("arn:ipset", "arn:nat")
    assert allow["Statement"] == {"IPSetReferenceStatement": {"ARN": "arn:ipset"}}
    assert allow["Action"] == {"Allow": {}}


# ⑦ 속도 제한 (#484) ------------------------------------------------------------
def test_rate_limit_is_evaluated_before_the_allow_rule():
    # 허용이 먼저 맞으면 WAF가 거기서 끝낸다 — 속도 규칙은 우선순위가 더 앞이어야 평가된다
    rate, allow = platform.waf_rules("arn:ipset", "arn:nat")
    assert rate["Name"] == "rate-per-ip"
    assert rate["Priority"] < allow["Priority"]


def test_rate_limit_counts_per_ip_but_never_the_nat_ip():
    (rate, _) = platform.waf_rules("arn:ipset", "arn:nat")
    stmt = rate["Statement"]["RateBasedStatement"]
    assert stmt["AggregateKeyType"] == "IP"
    assert stmt["Limit"] == platform.RATE_LIMIT_PER_WINDOW
    assert stmt["EvaluationWindowSec"] == platform.RATE_WINDOW_SECONDS
    # NAT 공인 IP가 모든 사용자의 서버 측 호출을 싣는다 — 세면 플랫폼이 스스로를 막는다
    assert stmt["ScopeDownStatement"] == {
        "NotStatement": {"Statement": {"IPSetReferenceStatement": {"ARN": "arn:nat"}}}}


def test_rate_limit_starts_in_count_mode():
    # 정상 사용량을 재지 않았다 — 관측 전에는 막지 않는다(#484 ⓔ)
    (rate, _) = platform.waf_rules("arn:ipset", "arn:nat")
    assert rate["Action"] == {"Count": {}}
    assert platform.RATE_WINDOW_SECONDS in (60, 120, 300, 600)  # WAF가 받는 창 길이


class FakeWaf:
    """IP 집합 · 웹 ACL 을 들고 있는 WAFv2 대역 — 생성 · 갱신 · 삭제가 상태를 바꾼다."""

    def __init__(self, ipsets=None, acl_rules=None):
        self.ipsets = {name: list(addrs) for name, addrs in (ipsets or {}).items()}
        self.acl_rules = acl_rules  # None = 웹 ACL 없음
        self.calls: list[str] = []

    def list_ip_sets(self, **_):
        return {"IPSets": [{"Name": n, "Id": f"id-{n}", "LockToken": "t"} for n in self.ipsets]}

    def get_ip_set(self, Name, **_):
        return {"IPSet": {"Addresses": self.ipsets[Name], "ARN": f"arn:{Name}"}, "LockToken": "t"}

    def create_ip_set(self, Name, Addresses, **_):
        self.calls.append(f"create_ip_set:{Name}")
        self.ipsets[Name] = list(Addresses)
        return {"Summary": {"ARN": f"arn:{Name}"}}

    def update_ip_set(self, Name, Addresses, **_):
        self.calls.append(f"update_ip_set:{Name}")
        self.ipsets[Name] = list(Addresses)

    def delete_ip_set(self, Name, **_):
        self.calls.append(f"delete_ip_set:{Name}")
        del self.ipsets[Name]

    def list_web_acls(self, **_):
        if self.acl_rules is None:
            return {"WebACLs": []}
        return {"WebACLs": [{"Name": platform.WAF_ACL, "Id": "acl-1", "ARN": "arn:acl"}]}

    def get_web_acl(self, **_):
        # 실제 응답처럼 요청에 없던 기본 키를 덧붙인다 — 통째 비교면 매번 「다름」이 된다
        rules = [{**r, "RuleLabels": []} for r in self.acl_rules]
        return {"WebACL": {"Rules": rules}, "LockToken": "t"}

    def create_web_acl(self, Rules, **_):
        self.calls.append("create_web_acl")
        self.acl_rules = Rules
        return {"Summary": {"ARN": "arn:acl"}}

    def update_web_acl(self, Rules, **_):
        self.calls.append("update_web_acl")
        self.acl_rules = Rules

    def delete_web_acl(self, **_):
        self.calls.append("delete_web_acl")
        self.acl_rules = None


ALLOW = ["203.0.113.7/32", "198.51.100.9/32"]
NAT = "198.51.100.9/32"


def test_new_acl_gets_the_rate_rule_and_a_separate_nat_set():
    waf = FakeWaf()
    platform._ensure_waf(waf, ALLOW, NAT)
    assert waf.ipsets[platform.WAF_NAT_IPSET] == [NAT]
    assert [r["Name"] for r in waf.acl_rules] == ["rate-per-ip", "allow-listed-ips"]


def test_existing_acl_without_the_rate_rule_is_updated():
    # 지금 배포된 플랫폼의 모양 — 허용 규칙 하나뿐인 웹 ACL(#484 「지금 상태」 ②)
    old = [{"Name": "allow-listed-ips", "Priority": 0, "Action": {"Allow": {}},
            "Statement": {"IPSetReferenceStatement": {"ARN": f"arn:{platform.WAF_IPSET}"}}}]
    waf = FakeWaf(ipsets={platform.WAF_IPSET: ALLOW}, acl_rules=old)
    platform._ensure_waf(waf, ALLOW, NAT)
    assert "update_web_acl" in waf.calls
    assert [r["Name"] for r in waf.acl_rules] == ["rate-per-ip", "allow-listed-ips"]


def test_second_run_changes_nothing():
    waf = FakeWaf()
    platform._ensure_waf(waf, ALLOW, NAT)
    waf.calls.clear()
    platform._ensure_waf(waf, ALLOW, NAT)
    assert waf.calls == []  # 응답에 기본 키가 덧붙어도 같은 규칙으로 본다


def test_switching_to_block_is_applied_on_rerun(monkeypatch):
    waf = FakeWaf()
    platform._ensure_waf(waf, ALLOW, NAT)
    monkeypatch.setattr(platform, "RATE_ACTION", "Block")
    waf.calls.clear()
    platform._ensure_waf(waf, ALLOW, NAT)
    assert waf.calls == ["update_web_acl"]
    assert waf.acl_rules[0]["Action"] == {"Block": {}}


def test_down_removes_the_acl_then_both_ip_sets():
    waf = FakeWaf(ipsets={platform.WAF_IPSET: ALLOW, platform.WAF_NAT_IPSET: [NAT]}, acl_rules=[])
    platform._delete_waf(waf)
    assert waf.ipsets == {} and waf.acl_rules is None
    # 웹 ACL 이 두 집합을 참조한다 — 집합을 먼저 지우면 WAF 가 거절한다
    assert waf.calls == ["delete_web_acl", f"delete_ip_set:{platform.WAF_IPSET}",
                         f"delete_ip_set:{platform.WAF_NAT_IPSET}"]


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


_DOMAIN = "vigilantis.example"


class _Pages:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return iter(self.pages)


class FakeAcm:
    def __init__(self, certs=()):
        self.certs = list(certs)

    def get_paginator(self, name):
        assert name == "list_certificates"
        return _Pages([{"CertificateSummaryList": self.certs}])


class FakeR53:
    """조회만 답한다 — 레코드 변경이 불리면 기록해 테스트가 잡게 한다."""

    def __init__(self, zones=()):
        self.zones = list(zones)
        self.calls: list[str] = []

    def get_paginator(self, name):
        assert name == "list_hosted_zones"
        return _Pages([{"HostedZones": self.zones}])

    def __getattr__(self, name):
        self.calls.append(name)
        raise AttributeError(name)


_CERT = {"DomainName": _DOMAIN, "CertificateArn": "arn:aws:acm:us-east-1:123456789012:certificate/x"}
_ZONE = {"Id": "/hostedzone/Z1", "Name": f"{_DOMAIN}.", "Config": {"PrivateZone": False}}


@pytest.mark.parametrize("certs, zones, missing", [
    ([], [_ZONE], r"인증서\(ISSUED\) 없음"),
    ([_CERT], [], "호스팅 영역 없음"),
    ([_CERT], [{**_ZONE, "Config": {"PrivateZone": True}}], "호스팅 영역 없음"),  # 비공개 영역은 별칭을 못 단다
])
def test_up_stops_before_creating_anything_when_the_domain_is_not_ready(certs, zones, missing):
    """인증서·공개 호스팅 영역이 없으면 VPC·RDS·WAF 어느 것도 바꾸기 전에 멈춘다."""
    ec2, r53 = RecordingEc2(smoke_vpc="vpc-smoke"), FakeR53(zones)
    c = {"iam": FakeIam(policies=[_SMOKE_POLICY]), "ec2": ec2, "acm": FakeAcm(certs), "r53": r53}
    with pytest.raises(SystemExit, match=missing):
        platform.up(c, ACCOUNT, "ap-northeast-2", ["203.0.113.7/32"], _DOMAIN)
    assert ec2.calls == ["describe_vpcs"]  # 스모크 VPC 조회 말고는 아무것도 부르지 않았다
    assert r53.calls == []  # c에 sm·rds·waf·cf가 없으니 그쪽 호출은 KeyError로 드러난다


def test_up_preview_also_checks_the_domain(monkeypatch):
    """--yes 없는 미리 보기에서 먼저 알려야 --yes 실행이 중간에 멈추지 않는다."""
    _fake_main(monkeypatch, ["up", "--account", ACCOUNT, "--allow-cidr", "203.0.113.7/32", "--domain", _DOMAIN],
               {"iam": FakeIam(policies=[_SMOKE_POLICY]), "ec2": RecordingEc2(smoke_vpc="vpc-smoke"),
                "acm": FakeAcm(), "route53": FakeR53([_ZONE])})
    with pytest.raises(SystemExit, match=r"인증서\(ISSUED\) 없음"):
        platform.main()


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
