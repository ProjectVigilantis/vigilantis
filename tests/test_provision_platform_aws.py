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
#   ⑥ 계정을 가르면(T-8) 접근 역할만 고객 계정에 · 남의 고객 역할은 덮어쓰지 않음 · 옛 플랫폼의 down이
#      넘겨받은 역할을 지우지 않음
#   ⑦ 운영 장치(T-8): 오류 알람은 배포 중 잠깐의 중단으로 울리지 않음 · api 5xx는 api 대상 그룹만 셈
#      · DB 자동 백업 7일
#   가드: 실 AWS·계정 대조 · 스모크 환경을 비싼 자원보다 먼저 확인 · DB 비밀번호는 RDS 생성 전에 저장
#         · 플랫폼 태그 없는 IAM 역할·오류 알람·알림 주제는 덮어쓰거나 지우지 않음
#         · 자동 롤백된 배포를 성공으로 보고하지 않음 · ECS가 전부 내려간 뒤에만 클러스터 삭제
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다.
# ==============================================================================

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
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
    def __init__(self, roles=None, policies=(), account=ACCOUNT):
        self.account = account
        self.roles = {name: {"RoleName": name, "Arn": f"arn:aws:iam::{account}:role/{name}", "Tags": list(tags)}
                      for name, tags in (roles or {}).items()}
        self.policies = set(policies)
        self.calls: list[tuple[str, str]] = []
        self.trust: dict[str, dict] = {}
        self.inline: dict[tuple[str, str], dict] = {}
        self.attached: list[tuple[str, str]] = []

    def get_role(self, RoleName):
        if RoleName not in self.roles:
            raise _client_error("NoSuchEntity", "GetRole")
        return {"Role": self.roles[RoleName]}

    def get_policy(self, PolicyArn):
        if PolicyArn not in self.policies:
            raise _client_error("NoSuchEntity", "GetPolicy")
        return {"Policy": {"Arn": PolicyArn}}

    def create_role(self, RoleName, AssumeRolePolicyDocument, Tags, **_):
        self.calls.append(("create_role", RoleName))
        self.roles[RoleName] = {"RoleName": RoleName, "Arn": f"arn:aws:iam::{self.account}:role/{RoleName}",
                                "Tags": list(Tags)}
        self.trust[RoleName] = json.loads(AssumeRolePolicyDocument)
        return {"Role": self.roles[RoleName]}

    def update_assume_role_policy(self, RoleName, PolicyDocument):
        self.calls.append(("update_assume_role_policy", RoleName))
        self.trust[RoleName] = json.loads(PolicyDocument)

    def tag_role(self, RoleName, Tags):
        self.calls.append(("tag_role", RoleName))
        keys = {t["Key"] for t in Tags}
        self.roles[RoleName]["Tags"] = [t for t in self.roles[RoleName]["Tags"] if t["Key"] not in keys] + Tags

    def untag_role(self, RoleName, TagKeys):
        self.calls.append(("untag_role", RoleName))
        self.roles[RoleName]["Tags"] = [t for t in self.roles[RoleName]["Tags"] if t["Key"] not in TagKeys]

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.inline[(RoleName, PolicyName)] = json.loads(PolicyDocument)

    def attach_role_policy(self, RoleName, PolicyArn):
        self.attached.append((RoleName, PolicyArn))

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

    def modify_db_instance(self, **kwargs):
        self.events.append(("modify_db_instance", kwargs.get("BackupRetentionPeriod")))
        self.modified = kwargs

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


# ⑥ 알림 배포(#493 PR3) -----------------------------------------------------------
REGION = "ap-northeast-2"
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:vigilantis-alerts"
SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:vigilantis-platform/slack-a-AbCdEf"


NOTIFY_TENANT = "11111111-1111-4111-8111-111111111111"


def _cfg(tmp_path, **over):
    body = {"enabled": True, "public_base_url": "https://dash.example.com",
            "routes": [{"tenant_id": NOTIFY_TENANT, "aws_account_id": "999900001111",
                        "slack_webhook_env": "NOTIFICATION_SLACK_A", "sns_topic_arn": TOPIC}],
            "slack_secret_arns": {"NOTIFICATION_SLACK_A": SECRET},
            "settings": {"NOTIFICATION_READ_TIMEOUT_SECONDS": 7, "NOTIFICATION_MAX_ATTEMPTS": 3}}
    body.update(over)
    path = tmp_path / "n.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def _body(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class NotifIam(FakeIam):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.puts: list[tuple[str, str, dict]] = []

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.puts.append((RoleName, PolicyName, json.loads(PolicyDocument)))


class NotifSm:
    def __init__(self, existing=(SECRET,)):
        self.existing = set(existing)

    def describe_secret(self, SecretId):
        if SecretId not in self.existing:
            raise _client_error("ResourceNotFoundException", "DescribeSecret")
        return {"ARN": SecretId}


TD = "arn:aws:ecs:r:1:task-definition/vigilantis-platform-api:1"


class FakeEcs:
    """클러스터·서비스·태스크·태스크 정의를 흉내 낸다. 쓰기 호출은 기록만 한다."""

    def __init__(self, *, cluster=True, service=None, tasks=(), env=None, writes=None):
        self.cluster, self.service, self.tasks = cluster, service, list(tasks)
        self.env = env or {}
        self.writes = writes if writes is not None else []

    def describe_clusters(self, clusters):
        return {"clusters": [{"clusterArn": "c", "status": "ACTIVE"}] if self.cluster else []}

    def describe_services(self, cluster, services):
        return {"services": [self.service] if self.service else []}

    def get_paginator(self, name):
        tasks = self.tasks
        class P:
            def paginate(self, cluster, family, desiredStatus):
                return [{"taskArns": [t["taskArn"] for t in tasks if t["desiredStatus"] == desiredStatus]}]
        return P()

    def describe_tasks(self, cluster, tasks):
        return {"tasks": [t for t in self.tasks if t["taskArn"] in tasks]}

    def describe_task_definition(self, taskDefinition):
        env = self.env.get(taskDefinition, [])
        return {"taskDefinition": {"containerDefinitions": [{"name": "api", "environment": env}]}}

    def register_task_definition(self, **kw):
        self.writes.append("register_task_definition")
        raise RuntimeError("stop")

    def run_task(self, **kw):
        self.writes.append("run_task")
        raise RuntimeError("stop")


def QuietEcs():
    return FakeEcs(cluster=False)


def _nsvc(rollout="COMPLETED", extra=()):
    return {"serviceName": platform.API_SERVICE, "status": "ACTIVE", "deployments": [
        {"status": "PRIMARY", "taskDefinition": TD, "rolloutState": rollout}, *extra]}


def _task(arn="t1", last="RUNNING", desired="RUNNING", overrides=None):
    return {"taskArn": arn, "lastStatus": last, "desiredStatus": desired, "taskDefinitionArn": TD,
            "overrides": overrides or {}}


def _on(v="true"):
    return {TD: [{"name": "NOTIFICATION_ENABLED", "value": v}]}


def test_quiescent_allows_no_service_and_legacy_or_disabled_tasks():
    platform.require_notifications_quiescent(FakeEcs(cluster=False))
    platform.require_notifications_quiescent(FakeEcs())  # 서비스·태스크 없음
    platform.require_notifications_quiescent(FakeEcs(service=_nsvc(), tasks=[_task()]))  # 플래그 없음 = 꺼진 레거시
    platform.require_notifications_quiescent(FakeEcs(service=_nsvc(), tasks=[_task()], env=_on("FALSE")))
    old = dict(_task(last="STOPPED", desired="STOPPED"), taskDefinitionArn=TD + "0")
    platform.require_notifications_quiescent(FakeEcs(service=_nsvc(), tasks=[old],
                                                     env={TD + "0": _on()[TD]}))  # 이미 STOPPED인 옛 태스크는 무시


@pytest.mark.parametrize("ecs", [
    FakeEcs(service=_nsvc("IN_PROGRESS"), tasks=[_task()]),
    FakeEcs(service=_nsvc("FAILED"), tasks=[_task()]),
    FakeEcs(service=_nsvc(extra=[{"status": "ACTIVE", "taskDefinition": "old", "rolloutState": "COMPLETED"}])),
    FakeEcs(service=_nsvc(), tasks=[_task()], env=_on()),
    FakeEcs(service=_nsvc(), tasks=[], env=_on()),   # COMPLETED 켜진 PRIMARY, 태스크 일시 없음
    FakeEcs(service=_nsvc(), tasks=[], env=_on("maybe")),
    FakeEcs(service=_nsvc(), tasks=[_task()], env=_on("maybe")),   # 알 수 없는 값은 닫힘
    FakeEcs(service=_nsvc(), tasks=[_task("old", "RUNNING", "STOPPED")], env=_on()),  # 내려가는 중인 켜진 태스크
    FakeEcs(service=_nsvc(), tasks=[_task(overrides={"containerOverrides": [
        {"name": "api", "environment": [{"name": "NOTIFICATION_ENABLED", "value": "1"}]}]})]),
    FakeEcs(cluster=True, service=None, tasks=[_task()], env=_on()),   # 서비스 없이 켜진 태스크
])
def test_apply_and_revoke_refuse_active_or_transitioning_api_before_any_write(tmp_path, ecs):
    _assert_refused(tmp_path, ecs, "정책은 바꾸지 않았다")


class FailingDescribeEcs(FakeEcs):
    def __init__(self, mode, **kw):
        super().__init__(**kw)
        self.mode = mode

    def describe_tasks(self, cluster, tasks):
        got = super().describe_tasks(cluster, tasks)
        if self.mode == "failures":
            got["failures"] = [{"arn": tasks[0], "reason": "MISSING"}]
        else:  # 하나를 조용히 빠뜨린다
            got["tasks"] = got["tasks"][1:]
        return got


@pytest.mark.parametrize("ecs", [
    FakeEcs(service=_nsvc(), tasks=[_task(overrides={"containerOverrides": [
        {"name": "api", "environment": [{"name": "notification_enabled", "value": "true"}]}]})]),
    FakeEcs(service=_nsvc(), tasks=[_task(overrides={"containerOverrides": [
        {"name": "api", "environment": [{"name": "Notification_Enabled", "value": "maybe"}]}]})]),
    FakeEcs(service=_nsvc(), tasks=[], env={TD: [{"name": "notification_enabled", "value": "true"}]}),
    FailingDescribeEcs("failures", service=_nsvc(), tasks=[_task()]),
    FailingDescribeEcs("omit", service=_nsvc(), tasks=[_task("a"), _task("b")]),
])
def test_guard_blocks_case_variant_flags_and_unverified_task_reads(tmp_path, ecs):
    _assert_refused(tmp_path, ecs, "정책은 바꾸지 않았다")


def _assert_refused(tmp_path, ecs, match):
    iam = NotifIam(roles=_both_roles())
    plan = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    with pytest.raises(SystemExit, match="정책은 바꾸지 않았다"):
        platform.apply_notification_policies(iam, ecs, plan)
    with pytest.raises(SystemExit, match="정책은 바꾸지 않았다"):
        platform.revoke_notification_policies(iam, ecs)
    assert iam.puts == [] and iam.calls == []


class PolicyIam(NotifIam):
    def __init__(self, docs, **kw):
        super().__init__(**kw)
        self.docs = docs

    def get_role_policy(self, RoleName, PolicyName):
        if (RoleName, PolicyName) not in self.docs:
            raise _client_error("NoSuchEntity", "GetRolePolicy")
        return {"PolicyDocument": self.docs[(RoleName, PolicyName)]}


def _exact_docs(plan):
    return {(platform.ROLE_TASK, platform.NOTIFY_POLICY_TASK): platform.notification_task_policy(plan["topic_arns"]),
            (platform.ROLE_EXEC, platform.NOTIFY_POLICY_EXEC):
                platform.notification_exec_policy(list(plan["secret_arns"].values()))}


def test_verify_accepts_equivalent_shapes_and_rejects_mismatches(tmp_path):
    plan = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    docs = _exact_docs(plan)
    platform.verify_notification_policies(PolicyIam(docs, roles=_both_roles()), plan)
    # 문자열/리스트·문장 형태가 달라도 같은 내용이면 통과
    (stmt,) = docs[(platform.ROLE_TASK, platform.NOTIFY_POLICY_TASK)]["Statement"]
    stmt["Resource"] = stmt["Resource"][0]
    platform.verify_notification_policies(PolicyIam(docs, roles=_both_roles()), plan)
    bad = [
        lambda d: d.pop((platform.ROLE_TASK, platform.NOTIFY_POLICY_TASK)),
        lambda d: d[(platform.ROLE_TASK, platform.NOTIFY_POLICY_TASK)]["Statement"][0].update(
            Resource=["*"]),
        lambda d: d[(platform.ROLE_TASK, platform.NOTIFY_POLICY_TASK)]["Statement"][0].update(
            Action=["sns:Publish", "sns:Subscribe"]),
        lambda d: d[(platform.ROLE_EXEC, platform.NOTIFY_POLICY_EXEC)]["Statement"][0].update(Effect="Deny"),
        lambda d: d[(platform.ROLE_EXEC, platform.NOTIFY_POLICY_EXEC)]["Statement"][0].update(
            Condition={"Bool": {"x": "y"}}),
    ]
    for mutate in bad:
        d = _exact_docs(plan)
        mutate(d)
        with pytest.raises(SystemExit, match="notification-iam"):
            platform.verify_notification_policies(PolicyIam(d, roles=_both_roles()), plan)


def _patch_deploy(monkeypatch):
    monkeypatch.setattr(platform, "_find_vpc", lambda ec2: "vpc")
    monkeypatch.setattr(platform, "_find_smoke_vpc", lambda ec2: "vpc-s")
    monkeypatch.setattr(platform, "_find_distribution", lambda cf: {"DomainName": "d.net"})
    monkeypatch.setattr(platform, "_secret_value", lambda sm, n: "x")
    monkeypatch.setattr(platform, "_find_subnets", lambda ec2, v: {})
    monkeypatch.setattr(platform, "_find_sgs", lambda ec2, v: {})
    monkeypatch.setattr(platform, "_find_repo", lambda ecr, n: "repo")
    monkeypatch.setattr(platform, "_find_role", lambda iam, n: f"arn:{n}")
    monkeypatch.setattr(platform, "_find_secret", lambda sm, n: f"arn:{n}")


def test_deploy_with_mismatched_policy_stops_before_registration_and_never_writes_iam(tmp_path, monkeypatch):
    _patch_deploy(monkeypatch)
    plan = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    iam = PolicyIam({}, roles=_both_roles())
    ecs = FakeEcs()
    c = {"ec2": None, "cf": None, "sm": None, "ecr": None, "iam": iam, "ecs": ecs}
    with pytest.raises(SystemExit, match="notification-iam"):
        platform.deploy(c, ACCOUNT, REGION, "t", False, plan)
    assert ecs.writes == [] and iam.puts == [] and iam.calls == []


def test_deploy_registration_failure_leaves_iam_unchanged(tmp_path, monkeypatch):
    _patch_deploy(monkeypatch)
    plan = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    iam = PolicyIam(_exact_docs(plan), roles=_both_roles())
    ecs = FakeEcs()
    c = {"ec2": None, "cf": None, "sm": None, "ecr": None, "iam": iam, "ecs": ecs}
    with pytest.raises(RuntimeError):
        platform.deploy(c, ACCOUNT, REGION, "t", False, plan)
    assert ecs.writes == ["register_task_definition"] and iam.puts == [] and iam.calls == []


def test_apply_on_disabled_stable_api_writes_exactly_our_policies(tmp_path):
    iam = NotifIam(roles=_both_roles())
    plan = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    platform.apply_notification_policies(iam, FakeEcs(service=_nsvc(), tasks=[_task()], env=_on("false")), plan)
    assert {(r, p) for r, p, _ in iam.puts} == set(platform.NOTIFY_POLICIES)


def _both_roles():
    return {platform.ROLE_TASK: _PLATFORM_TAGS, platform.ROLE_EXEC: _PLATFORM_TAGS}


def test_notifications_default_off_without_config():
    env = {e["name"]: e["value"] for e in _api()["environment"]}
    assert env["NOTIFICATION_ENABLED"] == "false"
    assert not any(k.startswith("NOTIFICATION_") and k != "NOTIFICATION_ENABLED" for k in env)


def test_config_needs_explicit_enabled_true(tmp_path):
    assert platform.load_notification_config(_cfg(tmp_path, enabled=False), ACCOUNT, REGION) is None
    body = _body(_cfg(tmp_path))
    del body["enabled"]
    p = tmp_path / "x.json"
    p.write_text(json.dumps(body))
    with pytest.raises(SystemExit):
        platform.load_notification_config(str(p), ACCOUNT, REGION)


def test_enabled_container_carries_refs_routes_and_settings(tmp_path):
    from notifications.routing import RouteRegistry
    plan = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    c = platform.api_container("i", region=REGION, cors_origin="https://x", access_role_arn="r", collect_vpc_ids="v",
                               dispatch_enabled=False, secrets_arns={"DATABASE_URL": "a"}, notification=plan)
    env = {e["name"]: e["value"] for e in c["environment"]}
    assert env["NOTIFICATION_ENABLED"] == "true"
    assert env["NOTIFICATION_PUBLIC_BASE_URL"] == "https://dash.example.com"
    assert env["NOTIFICATION_READ_TIMEOUT_SECONDS"] == "7" and env["NOTIFICATION_MAX_ATTEMPTS"] == "3"
    assert "NOTIFICATION_SLACK_A" not in env and "hooks.slack.com" not in json.dumps(c)
    assert {s["name"]: s["valueFrom"] for s in c["secrets"]}["NOTIFICATION_SLACK_A"] == SECRET
    # 생성되는 routes JSON은 런타임 파서가 그대로 읽는다 — serve.py가 만드는 파일과 같은 내용
    path = tmp_path / "routes.json"
    path.write_text(env["NOTIFICATION_ROUTES_JSON"], encoding="utf-8")
    reg = RouteRegistry.from_file(str(path), {"NOTIFICATION_SLACK_A": "https://hooks.slack.com/services/T1/B1/X1"})
    assert {r.channel for r in reg.routes} == {"SLACK", "SNS"}


def test_policies_are_exact_and_dedicated(tmp_path):
    plan = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    task = platform.notification_task_policy(plan["topic_arns"])
    execp = platform.notification_exec_policy(list(plan["secret_arns"].values()))
    assert task["Statement"][0]["Resource"] == [TOPIC] and task["Statement"][0]["Action"] == "sns:Publish"
    assert execp["Statement"][0]["Resource"] == [SECRET]
    assert "*" not in json.dumps(task) + json.dumps(execp)
    assert platform.NOTIFY_POLICY_TASK != "assume-access-role"
    assert platform.NOTIFY_POLICY_EXEC != "read-platform-secrets"


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(slack_secret_arns={}),
    lambda b: b.update(slack_secret_arns={"NOTIFICATION_SLACK_A": SECRET, "EXTRA": SECRET.replace("a-", "b-")}),
    lambda b: b.update(slack_secret_arns={"NOTIFICATION_SLACK_A": SECRET.replace("slack-a-AbCdEf", "*")}),
    lambda b: b.update(slack_secret_arns={"NOTIFICATION_SLACK_A": SECRET.replace(ACCOUNT, "123456789012")}),
    lambda b: b["routes"][0].update(sns_topic_arn=f"arn:aws:sns:{REGION}:{ACCOUNT}:*"),
    lambda b: b["routes"][0].update(sns_topic_arn=f"arn:aws:sns:{REGION}:999900001111:cust"),
    lambda b: b["routes"][0].update(slack_webhook="https://hooks.slack.com/services/T/B/X"),
    lambda b: b["routes"][0].update(tenant_id="a"),
    lambda b: b["routes"][0].update(tenant_id=f" {NOTIFY_TENANT}"),
    lambda b: b["routes"][0].update(tenant_id=None),
    lambda b: b.update(public_base_url="http://dash.example.com"),
    lambda b: b.update(settings={"NOTIFICATION_READ_TIMEOUT_SECONDS": 999}),
    lambda b: b.update(settings={"NOTIFICATION_ENABLED": 1}),
    lambda b: b.update(settings={"NOTIFICATION_BATCH_SIZE": True}),
])
def test_invalid_config_is_rejected(tmp_path, mutate):
    body = _body(_cfg(tmp_path))
    mutate(body)
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(body))
    with pytest.raises(SystemExit) as exc:
        platform.load_notification_config(str(p), ACCOUNT, REGION)
    assert "hooks.slack.com" not in str(exc.value)


@pytest.mark.parametrize("name", ["DATABASE_URL", "OPENAI_API_KEY", "CORS_ALLOW_ORIGINS", "AWS_REGION",
                                  "NOTIFICATION_ROUTES_JSON", "DISPATCH_ENABLED"])
def test_secret_env_name_collisions_are_rejected(tmp_path, name):
    p = _cfg(tmp_path, slack_secret_arns={name: SECRET})
    body = _body(p)
    body["routes"][0]["slack_webhook_env"] = name
    Path(p).write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(SystemExit, match="충돌"):
        platform.load_notification_config(p, ACCOUNT, REGION)


@pytest.mark.parametrize("name", ["database_url", "Database_Url", "openai_api_key", "aws_region",
                                  "Notification_Routes_Json", "dispatch_enabled"])
def test_secret_env_name_collisions_are_case_insensitive(tmp_path, name):
    p = _cfg(tmp_path, slack_secret_arns={name: SECRET})
    body = _body(p)
    body["routes"][0]["slack_webhook_env"] = name
    Path(p).write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(SystemExit, match="충돌"):
        platform.load_notification_config(p, ACCOUNT, REGION)


def test_api_container_rejects_case_variant_secret_names(tmp_path):
    plan = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    plan["secret_arns"] = {"database_url": SECRET}
    with pytest.raises(ValueError):
        platform.api_container("i", region=REGION, cors_origin="https://x", access_role_arn="r",
                               collect_vpc_ids="v", dispatch_enabled=False,
                               secrets_arns={"DATABASE_URL": "a"}, notification=plan)


@pytest.mark.parametrize("settings", [
    {"NOTIFICATION_BACKOFF_SECONDS": 10, "NOTIFICATION_MAX_BACKOFF_SECONDS": 5},
    {"NOTIFICATION_LEASE_SECONDS": 30, "NOTIFICATION_READ_TIMEOUT_SECONDS": 10},
    {"NOTIFICATION_BACKOFF_SECONDS": 400},   # 기본 max_backoff(300)보다 큼 — 기본값과의 조합
    {"notification_read_timeout_seconds": 5},
])
def test_effective_settings_cross_rules_match_runtime(tmp_path, settings):
    with pytest.raises(SystemExit):
        platform.load_notification_config(_cfg(tmp_path, settings=settings), ACCOUNT, REGION)


def test_effective_settings_ignore_host_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_LEASE_SECONDS", "31")
    monkeypatch.setenv("NOTIFICATION_READ_TIMEOUT_SECONDS", "30")
    assert platform.load_notification_config(_cfg(tmp_path, settings={}), ACCOUNT, REGION)["settings"] == {}


def test_routes_json_size_boundary(tmp_path):
    from notifications.routing import MAX_CONFIG_BYTES
    base = {"tenant_id": NOTIFY_TENANT, "aws_account_id": "999900001111"}
    base_len = len(json.dumps({"routes": [base]}, ensure_ascii=False, separators=(",", ":")).encode())
    ok = {"routes": [base]}
    # 서로 다른 UUID 테넌트·계정 라우트를 많이 두어 런타임 상한(파일 상한 80KiB 미만)을 넘긴다
    big = [{"tenant_id": str(uuid.UUID(int=i + 1)), "aws_account_id": f"{i:012d}"} for i in range(800)]
    big_len = len(json.dumps({"routes": big}, ensure_ascii=False, separators=(",", ":")).encode())
    assert base_len < MAX_CONFIG_BYTES < big_len
    assert platform.load_notification_config(
        _cfg(tmp_path, routes=ok["routes"], slack_secret_arns={}), ACCOUNT, REGION)
    with pytest.raises(SystemExit, match="상한"):
        platform.load_notification_config(_cfg(tmp_path, routes=big, slack_secret_arns={}), ACCOUNT, REGION)


def test_missing_secret_or_untagged_role_rejects_before_any_write(tmp_path):
    iam = NotifIam(roles=_both_roles())
    with pytest.raises(SystemExit, match="비밀이 없다"):
        platform.notification_plan({"sm": NotifSm(()), "iam": iam}, ACCOUNT, REGION, _cfg(tmp_path))
    iam = NotifIam(roles={platform.ROLE_TASK: _PLATFORM_TAGS, platform.ROLE_EXEC: []})
    with pytest.raises(SystemExit, match="플랫폼 태그"):
        platform.notification_plan({"sm": NotifSm(), "iam": iam}, ACCOUNT, REGION, _cfg(tmp_path))
    assert iam.puts == [] and iam.calls == []


def test_plan_is_read_only_and_apply_writes_only_dedicated_policies(tmp_path):
    iam = NotifIam(roles=_both_roles())
    plan = platform.notification_plan({"sm": NotifSm(), "iam": iam}, ACCOUNT, REGION, _cfg(tmp_path))
    assert iam.puts == [] and iam.calls == []  # 미리 보기는 변경 없음
    platform.apply_notification_policies(iam, QuietEcs(), plan)
    assert {(r, p) for r, p, _ in iam.puts} == set(platform.NOTIFY_POLICIES)


def test_slack_only_config_removes_stale_task_topic_policy(tmp_path):
    body = _body(_cfg(tmp_path))
    del body["routes"][0]["sns_topic_arn"]
    p = tmp_path / "s.json"
    p.write_text(json.dumps(body))
    iam = NotifIam(roles=_both_roles())
    platform.apply_notification_policies(iam, QuietEcs(), platform.load_notification_config(str(p), ACCOUNT, REGION))
    assert [(r, n) for r, n, _ in iam.puts] == [(platform.ROLE_EXEC, platform.NOTIFY_POLICY_EXEC)]
    assert iam.calls == [("delete_role_policy", platform.ROLE_TASK)]


def test_revoke_deletes_only_our_two_policies_on_tagged_roles():
    iam = NotifIam(roles={platform.ROLE_TASK: _PLATFORM_TAGS, platform.ROLE_EXEC: []})
    platform.revoke_notification_policies(iam, QuietEcs())
    assert iam.calls == [("delete_role_policy", platform.ROLE_TASK)]
    assert iam.puts == []


# ⑨ 운영 장치 — DB 자동 백업 -------------------------------------------------------
def test_new_db_keeps_a_week_of_automated_backups():
    events: list = []
    rds = FakeRds(events)
    platform._ensure_db(rds, FakeSm(events), _SUBNETS, "sg-db")
    assert rds.created["BackupRetentionPeriod"] == platform.DB_BACKUP_DAYS >= 7


def test_existing_db_with_one_day_of_backups_is_raised_to_a_week():
    """MVP 때 1일로 만든 인스턴스도 up 재실행으로 맞춘다."""
    events: list = []
    rds = FakeRds(events, exists=True)
    rds.db.update(DBInstanceStatus="available", BackupRetentionPeriod=1, Endpoint={"Address": FakeRds.HOST})
    sm = FakeSm(events, {platform.SECRET_DATABASE_URL: platform.database_url("pw", FakeRds.HOST)})
    platform._ensure_db(rds, sm, _SUBNETS, "sg-db")
    assert events == [("modify_db_instance", platform.DB_BACKUP_DAYS)]
    # 보존 기간 변경은 옵션 없이도 바로 적용된다 — ApplyImmediately는 대기 중인 다른 변경까지 끌고 온다
    assert "ApplyImmediately" not in rds.modified


# ⑨ 운영 장치 — 오류 알람 ----------------------------------------------------------
_LB_ARN = "arn:aws:elasticloadbalancing:ap-northeast-2:111122223333:loadbalancer/app/vigilantis-platform-alb/abc"
_TG_API_ARN = "arn:aws:elasticloadbalancing:ap-northeast-2:111122223333:targetgroup/vigilantis-platform-api/111"
_TG_WEB_ARN = "arn:aws:elasticloadbalancing:ap-northeast-2:111122223333:targetgroup/vigilantis-platform-web/222"
_TOPIC = "arn:aws:sns:ap-northeast-2:111122223333:vigilantis-platform-alarms"


def _alarms() -> dict[str, dict]:
    return {a["AlarmName"]: a for a in platform.alarm_definitions(_LB_ARN, _TG_API_ARN, _TG_WEB_ARN, _TOPIC)}


def test_every_alarm_notifies_on_trouble_and_on_recovery():
    alarms = _alarms()
    assert list(alarms) == list(platform.ALARM_NAMES)
    for alarm in alarms.values():
        assert alarm["AlarmActions"] == [_TOPIC] and alarm["OKActions"] == [_TOPIC]


def test_down_alarms_outlast_the_gap_every_api_deploy_makes():
    """api는 겹쳐 뜨지 않게 내린 뒤 띄운다(min 0) — 정상 대상이 DOWN_MINUTES분 내내 0이어야 울린다."""
    alarms = _alarms()
    api, web = alarms[f"{platform.PREFIX}-api-down"], alarms[f"{platform.PREFIX}-web-down"]
    assert api["Dimensions"] == [{"Name": "TargetGroup", "Value": "targetgroup/vigilantis-platform-api/111"},
                                 {"Name": "LoadBalancer", "Value": "app/vigilantis-platform-alb/abc"}]
    assert web["Dimensions"][0] == {"Name": "TargetGroup", "Value": "targetgroup/vigilantis-platform-web/222"}
    for alarm in (api, web):
        assert (alarm["Statistic"], alarm["Period"], alarm["Threshold"]) == ("Maximum", 60, 1)
        assert alarm["EvaluationPeriods"] == alarm["DatapointsToAlarm"] == platform.DOWN_MINUTES >= 10
        assert alarm["TreatMissingData"] == "breaching"  # 지표가 끊긴 것도 내려간 것으로 본다


def test_5xx_alarm_stays_quiet_without_traffic():
    alarm = _alarms()[f"{platform.PREFIX}-api-5xx"]
    assert alarm["TreatMissingData"] == "notBreaching"


def test_5xx_alarm_counts_only_the_api_target_group():
    """ALB는 api·web이 함께 쓴다 — LoadBalancer만 주면 web의 5xx도 api 장애로 센다."""
    alarm = _alarms()[f"{platform.PREFIX}-api-5xx"]
    assert alarm["Dimensions"] == [{"Name": "TargetGroup", "Value": "targetgroup/vigilantis-platform-api/111"},
                                   {"Name": "LoadBalancer", "Value": "app/vigilantis-platform-alb/abc"}]


class FakeCw:
    def __init__(self, alarms: dict[str, list[dict]]):
        self.alarms = alarms  # 이름 → 태그
        self.calls: list[tuple[str, object]] = []

    def describe_alarms(self, AlarmNames):
        return {"MetricAlarms": [{"AlarmName": n, "AlarmArn": f"arn:aws:cloudwatch:::alarm:{n}", "StateValue": "OK"}
                                 for n in AlarmNames if n in self.alarms]}

    def list_tags_for_resource(self, ResourceARN):
        return {"Tags": self.alarms[ResourceARN.rsplit(":", 1)[1]]}

    def put_metric_alarm(self, **kwargs):
        self.calls.append(("put_metric_alarm", kwargs["AlarmName"]))

    def delete_alarms(self, AlarmNames):
        self.calls.append(("delete_alarms", tuple(AlarmNames)))


class FakeSns:
    def __init__(self, topic_tags: list[dict] | None = None):
        self.topic_tags = topic_tags  # None이면 주제가 없다
        self.calls: list[str] = []

    def get_paginator(self, name):
        topics = [{"TopicArn": _TOPIC}] if self.topic_tags is not None else []
        return type("P", (), {"paginate": lambda _self: [{"Topics": topics}]})()

    def list_tags_for_resource(self, ResourceArn):
        return {"Tags": self.topic_tags}

    def create_topic(self, **kwargs):
        self.calls.append("create_topic")
        return {"TopicArn": _TOPIC}

    def list_subscriptions_by_topic(self, TopicArn):
        return {"Subscriptions": [{"Protocol": "email", "Endpoint": "ops@example.com"}]}

    def subscribe(self, **kwargs):
        self.calls.append("subscribe")

    def delete_topic(self, TopicArn):
        self.calls.append("delete_topic")


_ALB = {"arn": _LB_ARN, "tg_api": _TG_API_ARN, "tg_web": _TG_WEB_ARN}
_API_DOWN = f"{platform.PREFIX}-api-down"


def test_alarms_of_ours_are_refreshed_on_rerun():
    cw = FakeCw({n: _PLATFORM_TAGS for n in platform.ALARM_NAMES})
    sns = FakeSns(_PLATFORM_TAGS)
    platform._ensure_alarms(cw, sns, _ALB, "ops@example.com")
    assert [name for _, name in cw.calls] == list(platform.ALARM_NAMES)
    assert sns.calls == []  # 기존 주제를 그대로 쓴다


@pytest.mark.parametrize("alarm_tags, topic_tags, foreign", [
    ([], _PLATFORM_TAGS, _API_DOWN),  # 같은 이름의 남의 알람
    (_PLATFORM_TAGS, [], platform.ALARM_TOPIC),  # 같은 이름의 남의 알림 주제
])
def test_same_named_alarm_or_topic_of_someone_else_stops_before_any_write(alarm_tags, topic_tags, foreign):
    cw, sns = FakeCw({_API_DOWN: alarm_tags}), FakeSns(topic_tags)
    with pytest.raises(SystemExit, match=foreign):
        platform._ensure_alarms(cw, sns, _ALB, "ops@example.com")
    assert cw.calls == [] and sns.calls == []  # 덮어쓰지도, 주제를 새로 만들지도 않았다


def test_down_deletes_only_platform_tagged_alarms_and_topic():
    other = f"{platform.PREFIX}-web-down"
    cw = FakeCw({_API_DOWN: _PLATFORM_TAGS, other: [{"Key": "owner", "Value": "someone-else"}]})
    sns = FakeSns([])  # 이름만 같은 남의 주제 — 지우면 구독까지 함께 사라진다
    platform._delete_alarms(cw, sns)
    assert cw.calls == [("delete_alarms", (_API_DOWN,))]
    assert sns.calls == []


def test_up_stops_before_creating_anything_when_a_same_named_alarm_is_not_ours():
    ec2 = RecordingEc2(smoke_vpc="vpc-smoke")
    c = {"iam": FakeIam(policies=[_SMOKE_POLICY]), "ec2": ec2, "cw": FakeCw({_API_DOWN: []}), "sns": FakeSns()}
    with pytest.raises(SystemExit, match=_API_DOWN):
        platform.up(c, ACCOUNT, "ap-northeast-2", ["203.0.113.7/32"])
    assert ec2.calls == ["describe_vpcs"]  # VPC·NAT·RDS 어느 것도 만들지 않았다


# ⑧ 계정 분리(T-8) ----------------------------------------------------------------
CUSTOMER = "444455556666"
_CUSTOMER_SMOKE_POLICY = f"arn:aws:iam::{CUSTOMER}:policy/{platform.SMOKE_POLICY_NAME}"
_CUSTOMER_TAG = [{"Key": platform.CUSTOMER_TAG_KEY, "Value": ACCOUNT}]


def test_split_accounts_put_only_the_access_role_in_the_customer_account():
    plat, cust = FakeIam(), FakeIam(account=CUSTOMER)
    roles = platform._ensure_roles(plat, ACCOUNT, ["arn:secret"], "ext-1", {"iam": cust, "account": CUSTOMER})

    assert set(plat.roles) == {platform.ROLE_EXEC, platform.ROLE_TASK}
    assert set(cust.roles) == {platform.ROLE_ACCESS}
    assert roles["access"] == f"arn:aws:iam::{CUSTOMER}:role/{platform.ROLE_ACCESS}"
    # 태스크 역할은 고객 계정의 접근 역할 하나로만 갈아탄다
    (allowed,) = plat.inline[(platform.ROLE_TASK, "assume-access-role")]["Statement"]
    assert allowed["Resource"] == f"arn:aws:iam::{CUSTOMER}:role/{platform.ROLE_ACCESS}"
    # 접근 역할은 플랫폼 계정의 태스크 역할만, ExternalId가 맞을 때만 받는다
    (trusted,) = cust.trust[platform.ROLE_ACCESS]["Statement"]
    assert trusted["Principal"] == {"AWS": f"arn:aws:iam::{ACCOUNT}:role/{platform.ROLE_TASK}"}
    assert trusted["Condition"] == {"StringEquals": {"sts:ExternalId": "ext-1"}}
    # 권한은 고객 계정의 스모크 거울 정책이고, 고객 태그가 이 플랫폼 계정을 가리킨다
    assert cust.attached == [(platform.ROLE_ACCESS, _CUSTOMER_SMOKE_POLICY)]
    assert cust.roles[platform.ROLE_ACCESS]["Tags"] == _CUSTOMER_TAG


def test_old_platform_access_role_is_taken_over_and_survives_the_old_platform_down():
    cust = FakeIam(account=CUSTOMER, roles={platform.ROLE_ACCESS: _PLATFORM_TAGS})
    trust = platform.access_trust_policy(ACCOUNT, "ext-1")
    platform._ensure_customer_access_role(cust, ACCOUNT, trust)

    # 고객 태그를 먼저 단다 — 신뢰 정책을 바꾼 뒤 끊겨도 다음 실행이 이 역할을 알아본다
    assert [name for name, _ in cust.calls] == ["tag_role", "update_assume_role_policy", "untag_role"]
    assert cust.trust[platform.ROLE_ACCESS] == trust
    assert cust.roles[platform.ROLE_ACCESS]["Tags"] == _CUSTOMER_TAG
    # 학원 계정에서 옛 플랫폼을 단일 계정 모드로 down해도 넘겨받은 역할은 남는다
    platform._delete_roles(cust, CUSTOMER)
    assert platform.ROLE_ACCESS in cust.roles


def test_interrupted_takeover_is_finished_on_rerun():
    cust = FakeIam(account=CUSTOMER, roles={platform.ROLE_ACCESS: _PLATFORM_TAGS + _CUSTOMER_TAG})
    platform._ensure_customer_access_role(cust, ACCOUNT, {"Statement": []})
    assert [name for name, _ in cust.calls] == ["update_assume_role_policy", "untag_role"]
    assert cust.roles[platform.ROLE_ACCESS]["Tags"] == _CUSTOMER_TAG


def test_old_platform_down_spares_a_half_taken_over_access_role():
    """넘겨받기가 플랫폼 태그를 떼기 전에 끊기면 두 태그가 함께 남는다 — 이미 새 플랫폼이 쓰는 역할이다."""
    cust = FakeIam(account=CUSTOMER, roles={platform.ROLE_ACCESS: _PLATFORM_TAGS + _CUSTOMER_TAG})
    platform._delete_roles(cust, CUSTOMER)
    assert platform.ROLE_ACCESS in cust.roles
    assert cust.calls == []  # 정책 분리도 하지 않았다


@pytest.mark.parametrize("tags, reason", [
    ([{"Key": "owner", "Value": "someone-else"}], "고객 태그"),
    ([{"Key": platform.CUSTOMER_TAG_KEY, "Value": "999999999999"}], "999999999999"),
])
def test_customer_role_of_someone_else_is_not_overwritten(tags, reason):
    cust = FakeIam(account=CUSTOMER, roles={platform.ROLE_ACCESS: tags})
    with pytest.raises(SystemExit, match=reason):
        platform._ensure_customer_access_role(cust, ACCOUNT, {"Statement": []})
    assert cust.calls == []


@pytest.mark.parametrize("customer_policies, customer_vpc, missing", [
    ([], "vpc-smoke", platform.SMOKE_POLICY_NAME),
    ([_CUSTOMER_SMOKE_POLICY], None, "스모크 VPC"),
])
def test_split_up_looks_for_the_smoke_environment_in_the_customer_account(customer_policies, customer_vpc, missing):
    """플랫폼 계정에 같은 이름의 정책·VPC가 있어도 그것으로 통과하지 않는다."""
    plat_ec2, cust_ec2 = RecordingEc2(smoke_vpc="vpc-in-platform"), RecordingEc2(smoke_vpc=customer_vpc)
    c = {"iam": FakeIam(policies=[_SMOKE_POLICY]), "ec2": plat_ec2,
         "customer": {"iam": FakeIam(account=CUSTOMER, policies=customer_policies), "ec2": cust_ec2,
                      "account": CUSTOMER}}
    with pytest.raises(SystemExit, match=missing):
        platform.up(c, ACCOUNT, "ap-northeast-2", ["203.0.113.7/32"])
    assert plat_ec2.calls == []  # 플랫폼 계정에는 아무것도 묻거나 만들지 않았다


def test_customer_account_needs_its_profile(monkeypatch):
    _fake_main(monkeypatch, ["status", "--account", ACCOUNT, "--customer-account", CUSTOMER], {})
    with pytest.raises(SystemExit, match="함께 준다"):
        platform.main()


def test_customer_account_must_differ_from_the_platform_account(monkeypatch):
    _fake_main(monkeypatch, ["status", "--account", ACCOUNT, "--customer-account", ACCOUNT,
                             "--customer-profile", "p"], {})
    with pytest.raises(SystemExit, match="같다"):
        platform.main()


def test_customer_profile_must_belong_to_the_customer_account(monkeypatch):
    class _Sts:
        def get_caller_identity(self):
            return {"Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/x"}

    class _Session:
        def __init__(self, profile_name, region_name):
            pass

        def client(self, name, config=None):
            assert name == "sts"  # 계정을 대조하기 전에는 다른 클라이언트를 만들지 않는다
            return _Sts()

    monkeypatch.setattr(platform.boto3, "Session", _Session)
    with pytest.raises(SystemExit, match="--customer-profile"):
        platform._customer_clients("customer-admin", CUSTOMER, "ap-northeast-2")
