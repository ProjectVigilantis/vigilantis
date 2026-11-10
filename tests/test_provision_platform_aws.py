# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# scripts/provision_platform_aws.py의 정의(정책·규칙·컨테이너·배포 설정) 회귀 테스트입니다. (ADR-0010)
#
# AWS를 부르지 않는다(boto3 자리는 가짜 객체). 지키는 것은 "SaaS 모양"을 이루는 울타리 다섯 묶음과
# 구성 절차의 가드다.
#   ① api 태스크 역할은 고객 접근 역할(vigilantis-access*) 전환과 로그인 사용자 풀 관리뿐. AWS 자원을 직접
#      만지지 않는다. 고객 접근 역할 자체는 온보딩 템플릿이 만든다(tests/test_onboarding_template.py)
#   ② 로그인 사용자 풀은 auth README의 전제(MFA ON · TOTP · 관리자만 생성 · 비밀번호 12자)대로
#   ③ ALB는 비밀 헤더가 있는 요청만 앱으로 보낸다(CloudFront 우회 차단) · 도메인이 있으면 CloudFront→ALB는
#      origin.<도메인> HTTPS만(평문 80 리스너·SG 규칙을 걷어 낸다)
#   ④ WAF는 기본 차단 · 허용 IP만 통과 · 허용 CIDR은 /24보다 넓으면 거부
#   ⑤ api는 겹쳐 뜨지 않는다(min 0 · max 100) · AI 분석·조치 실행 디스패치는 명시해야 켜진다
#      · 비밀은 값이 아니라 참조로만
#   ⑥ 고객 계정은 건드리지 않는다: 관제 범위 필터 없이 테넌트 문맥으로만 고객 계정에 닿는다
#   ⑦ 운영 장치(T-8): 오류 알람은 배포 중 잠깐의 중단으로 울리지 않음 · api 5xx는 api 대상 그룹만 셈
#      · DB 자동 백업 7일
#   가드: 실 AWS·계정 대조 · 도메인 준비를 비싼 자원보다 먼저 확인 · DB 비밀번호는 RDS 생성 전에 저장
#         · 플랫폼 태그 없는 IAM 역할·오류 알람·알림 주제는 덮어쓰거나 지우지 않음
#         · 자동 롤백된 배포를 성공으로 보고하지 않음 · ECS가 전부 내려간 뒤에만 클러스터 삭제
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다.
# ==============================================================================

from __future__ import annotations

import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit

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

ACCOUNT = "111122223333"


# ① ---------------------------------------------------------------------------
POOL_ARN = f"arn:aws:cognito-idp:ap-northeast-2:{ACCOUNT}:userpool/ap-northeast-2_Abc"


def test_task_role_can_only_assume_customer_access_roles_and_manage_login_users():
    assume, login = platform.task_role_policy(POOL_ARN)["Statement"]
    assert (assume["Action"], assume["Resource"]) == ("sts:AssumeRole", f"arn:aws:iam::*:role/{platform.ROLE_ACCESS}*")
    assert login["Resource"] == POOL_ARN
    assert all(a.startswith("cognito-idp:Admin") for a in login["Action"])
    actions = json.dumps(platform.task_role_policy(POOL_ARN))
    # 고객 계정은 열려 있다. 이름 접두가 빠지면 플랫폼이 아무 역할(관리자 등)로나 갈아탈 수 있다
    assert f"role/{platform.ROLE_ACCESS}*" in actions and '"Resource": "*"' not in actions


def test_task_role_cognito_actions_match_the_auth_code():
    """auth/cognito.py가 부르는 Cognito 작업과 태스크 역할의 작업이 같다(한쪽만 바뀌면 로그인이 AccessDenied)."""
    source = (REPO_ROOT / "apps" / "core-api" / "auth" / "cognito.py").read_text(encoding="utf-8")
    called = {f"cognito-idp:{''.join(p.capitalize() for p in name.split('_'))}"
              for name in re.findall(r'"(admin_[a-z_]+)"', source)}
    assert called == set(platform.COGNITO_ACTIONS)


def test_onboarding_template_role_name_matches_the_assume_prefix():
    pattern = platform.onboarding.build_template()["Parameters"]["RoleName"]["AllowedPattern"]
    assert pattern.startswith(f"^{platform.ROLE_ACCESS}")


def test_onboarding_bucket_opens_only_the_template_object():
    (stmt,) = platform.onboarding_bucket_policy(ACCOUNT)["Statement"]
    assert stmt["Action"] == "s3:GetObject" and stmt["Principal"] == "*"
    assert stmt["Resource"] == (f"arn:aws:s3:::{platform.onboarding_bucket(ACCOUNT)}/"
                                f"{platform.onboarding.TEMPLATE_KEY}")
    assert platform.onboarding_template_url(ACCOUNT, "ap-northeast-2").endswith(platform.onboarding.TEMPLATE_KEY)


# ② ---------------------------------------------------------------------------
def test_login_pool_follows_the_auth_readme():
    pool = platform.user_pool_definition()
    assert pool["AdminCreateUserConfig"] == {"AllowAdminCreateUserOnly": True}
    assert pool["UsernameAttributes"] == ["email"] and pool["UsernameConfiguration"] == {"CaseSensitive": False}
    assert pool["AccountRecoverySetting"]["RecoveryMechanisms"] == [{"Priority": 1, "Name": "verified_email"}]
    policy = pool["Policies"]["PasswordPolicy"]
    assert policy["MinimumLength"] >= 12 and all(policy[k] for k in (
        "RequireUppercase", "RequireLowercase", "RequireNumbers", "RequireSymbols"))
    mfa = platform.user_pool_mfa_config("pool-1")
    assert mfa["MfaConfiguration"] == "ON" and mfa["SoftwareTokenMfaConfiguration"] == {"Enabled": True}
    assert "SmsMfaConfiguration" not in mfa  # TOTP만


def test_login_client_uses_only_the_admin_flow_with_a_secret():
    client = platform.app_client_definition("pool-1")
    assert client["GenerateSecret"] is True
    assert set(client["ExplicitAuthFlows"]) == {"ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"}
    assert (client["EnableTokenRevocation"], client["AuthSessionValidity"], client["PreventUserExistenceErrors"]) == (
        True, 3, "ENABLED")


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


# CloudFront→ALB HTTPS(#494) -----------------------------------------------------
def test_with_a_domain_cloudfront_reaches_the_alb_over_https_only():
    cfg = platform.distribution_config("origin.vigilantis.example", "secret-value", "arn:acl", "ref", https=True)
    (origin,) = cfg["Origins"]["Items"]
    assert origin["DomainName"] == "origin.vigilantis.example"  # ALB 기본 주소에는 인증서를 받을 수 없다
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    assert origin["CustomOriginConfig"]["OriginSslProtocols"] == {"Quantity": 1, "Items": ["TLSv1.2"]}


def test_without_a_domain_the_alb_leg_stays_plain_http():
    (origin,) = platform.distribution_config("alb.example", "secret-value", "arn:acl", "ref")["Origins"]["Items"]
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "http-only"


def test_attaching_the_domain_moves_an_existing_http_distribution_to_https():
    """도메인 없이 먼저 만든 배포도 재실행이 오리진을 origin.<도메인> HTTPS로 옮긴다. 비밀 헤더는 그대로다."""
    base = platform.distribution_config("alb.example", "secret-value", "arn:acl", "ref")
    updated = platform.custom_domain_config(base, "vigilantis.example", "arn:cert", "origin.vigilantis.example")
    (origin,) = updated["Origins"]["Items"]
    assert origin["DomainName"] == "origin.vigilantis.example"
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    assert origin["CustomHeaders"] == base["Origins"]["Items"][0]["CustomHeaders"]
    assert base["Origins"]["Items"][0]["DomainName"] == "alb.example"  # 원본을 고치지 않는다


def test_down_removes_only_the_origin_record_that_points_at_this_alb():
    host, alb = "origin.vigilantis.example", "vigilantis-platform-alb-1.ap-northeast-2.elb.amazonaws.com"
    ours = {"Name": f"{host}.", "Type": "A", "AliasTarget": {"DNSName": f"dualstack.{alb.upper()}."}}
    records = [
        ours,
        {"Name": f"{host}.", "Type": "A", "AliasTarget": {"DNSName": "dualstack.other-alb.elb.amazonaws.com."}},
        {"Name": "api.vigilantis.example.", "Type": "A", "AliasTarget": {"DNSName": f"dualstack.{alb}."}},
    ]
    assert platform.origin_records_for(records, host, alb) == [ours]


class FakeElbv2:
    def __init__(self, listeners=()):
        self.listeners = {li["Port"]: dict(li) for li in listeners}
        self.calls: list[tuple[str, object]] = []

    def describe_listeners(self, LoadBalancerArn):
        return {"Listeners": list(self.listeners.values())}

    def create_listener(self, **kwargs):
        self.calls.append(("create_listener", kwargs["Port"]))
        self.listeners[kwargs["Port"]] = {**kwargs, "ListenerArn": f"listener-{kwargs['Port']}"}
        return {"Listeners": [self.listeners[kwargs["Port"]]]}

    def delete_listener(self, ListenerArn):
        self.calls.append(("delete_listener", ListenerArn))

    def modify_listener(self, ListenerArn, Certificates, SslPolicy):
        self.calls.append(("modify_listener", Certificates[0]["CertificateArn"]))


def test_https_listener_replaces_the_plain_listener():
    """80 리스너에 비밀 헤더 규칙이 남아 있으면 평문 경로가 그대로 열린다 — 지운다."""
    elbv2 = FakeElbv2([{"Port": 80, "ListenerArn": "listener-80"}])
    arn = platform._ensure_listener(elbv2, "lb", "arn:origin-cert")
    assert arn == "listener-443"
    assert elbv2.calls == [("delete_listener", "listener-80"), ("create_listener", 443)]
    created = elbv2.listeners[443]
    assert (created["Protocol"], created["Certificates"]) == ("HTTPS", [{"CertificateArn": "arn:origin-cert"}])
    assert created["SslPolicy"] == platform.ALB_TLS_POLICY
    assert created["DefaultActions"][0]["FixedResponseConfig"]["StatusCode"] == "403"  # 규칙 밖은 거절


def test_https_listener_follows_a_renewed_certificate_on_rerun():
    elbv2 = FakeElbv2([{"Port": 443, "ListenerArn": "listener-443", "Certificates": [{"CertificateArn": "old"}]}])
    assert platform._ensure_listener(elbv2, "lb", "new") == "listener-443"
    assert elbv2.calls == [("modify_listener", "new")]


def test_without_a_certificate_the_listener_stays_plain_http():
    elbv2 = FakeElbv2()
    assert platform._ensure_listener(elbv2, "lb", None) == "listener-80"
    assert elbv2.listeners[80]["Protocol"] == "HTTP"


class SgEc2:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": [{"GroupName": n, "GroupId": f"sg-{n}"} for n in platform.SECURITY_GROUPS]}

    def describe_managed_prefix_lists(self, Filters):
        return {"PrefixLists": [{"PrefixListId": "pl-cf"}]}

    def authorize_security_group_ingress(self, GroupId, IpPermissions):
        self.calls.append(("authorize", GroupId, IpPermissions[0]["FromPort"]))

    def revoke_security_group_ingress(self, GroupId, IpPermissions):
        self.calls.append(("revoke", GroupId, IpPermissions[0]["FromPort"]))


@pytest.mark.parametrize("https, opened, closed", [(True, 443, [80]), (False, 80, [])])
def test_alb_accepts_cloudfront_on_one_port_only(https, opened, closed):
    """CloudFront prefix list는 SG 규칙 한도를 항목 수만큼 써서 80·443을 함께 열면 한도를 넘을 수 있다."""
    ec2 = SgEc2()
    platform._ensure_sgs(ec2, "vpc-1", https=https)
    alb_sg = f"sg-{platform.SG_ALB}"
    assert [port for op, sg, port in ec2.calls if op == "revoke" and sg == alb_sg] == closed
    assert [port for op, sg, port in ec2.calls if op == "authorize" and sg == alb_sg] == [opened]


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
RULE_NAMES = ["rate-login-per-ip", "rate-per-ip", "allow-listed-ips"]


def _rules():
    return {rule["Name"]: rule for rule in platform.waf_rules("arn:ipset", "arn:nat")}


def test_waf_allows_only_the_ip_set():
    allow = _rules()["allow-listed-ips"]
    assert allow["Statement"] == {"IPSetReferenceStatement": {"ARN": "arn:ipset"}}
    assert allow["Action"] == {"Allow": {}}


# ⑦ 속도 제한 (#484) ------------------------------------------------------------
def test_rate_limits_are_evaluated_before_the_allow_rule():
    # 허용이 먼저 맞으면 WAF가 거기서 끝낸다 — 속도 규칙은 우선순위가 더 앞이어야 평가된다.
    # 로그인 규칙이 맨 앞 — Block 이 되면 더 낮은 한도가 먼저 끊는다(#522 Q5)
    rules = platform.waf_rules("arn:ipset", "arn:nat")
    assert [rule["Name"] for rule in rules] == RULE_NAMES
    assert [rule["Priority"] for rule in rules] == [0, 1, 2]


def test_rate_limit_counts_per_ip_but_never_the_nat_ip():
    rate = _rules()["rate-per-ip"]
    stmt = rate["Statement"]["RateBasedStatement"]
    assert stmt["AggregateKeyType"] == "IP"
    assert stmt["Limit"] == platform.RATE_LIMIT_PER_WINDOW
    assert stmt["EvaluationWindowSec"] == platform.RATE_WINDOW_SECONDS
    # NAT 공인 IP가 모든 사용자의 서버 측 호출을 싣는다 — 세면 플랫폼이 스스로를 막는다
    assert stmt["ScopeDownStatement"] == {
        "NotStatement": {"Statement": {"IPSetReferenceStatement": {"ARN": "arn:nat"}}}}


def _login_path_matches(path: bytes) -> bool:
    """로그인 규칙의 경로 조건(OR · URL 디코딩 뒤 STARTS_WITH)을 그대로 흉내 낸다."""
    rule = _rules()["rate-login-per-ip"]
    paths, _ = rule["Statement"]["RateBasedStatement"]["ScopeDownStatement"]["AndStatement"]["Statements"]
    def seen(match):
        decode = {"Priority": 0, "Type": "URL_DECODE"} in match["TextTransformations"]
        return unquote_to_bytes(path) if decode else path
    return any(seen(m).startswith(m["SearchString"])
               for m in (s["ByteMatchStatement"] for s in paths["OrStatement"]["Statements"]))


def test_login_rate_limit_counts_only_the_login_and_reset_paths_per_ip_without_the_nat_ip():
    rule = _rules()["rate-login-per-ip"]
    stmt = rule["Statement"]["RateBasedStatement"]
    assert (stmt["AggregateKeyType"], stmt["Limit"], stmt["EvaluationWindowSec"]) == (
        "IP", platform.LOGIN_RATE_LIMIT_PER_WINDOW, platform.RATE_WINDOW_SECONDS)
    assert platform.LOGIN_RATE_LIMIT_PER_WINDOW < platform.RATE_LIMIT_PER_WINDOW
    paths, not_nat = stmt["ScopeDownStatement"]["AndStatement"]["Statements"]
    # 대입 대상인 경로만 — /me · /activity 가 섞이면 관측 지표로 대입과 정상 사용을 가를 수 없다
    assert paths == {"OrStatement": {"Statements": [{"ByteMatchStatement": {
        # get_web_acl 이 bytes 로 돌려준다 — str 이면 두 번째 실행부터 매번 「다름」이 된다
        "SearchString": prefix,
        "FieldToMatch": {"UriPath": {}},
        # 앱은 %인코딩을 풀어 라우팅한다 — 풀지 않고 맞추면 /api/v1/auth/%6Cogin 이 비켜 간다
        "TextTransformations": [{"Priority": 0, "Type": "URL_DECODE"}],
        "PositionalConstraint": "STARTS_WITH",
    }} for prefix in (b"/api/v1/auth/login", b"/api/v1/auth/password-reset")]}}
    assert not_nat == {"NotStatement": {"Statement": {"IPSetReferenceStatement": {"ARN": "arn:nat"}}}}
    assert rule["VisibilityConfig"]["MetricName"] == f"{platform.PREFIX}-rate-login"


@pytest.mark.parametrize("path", [
    b"/api/v1/auth/login", b"/api/v1/auth/login/new-password", b"/api/v1/auth/login/totp-setup",
    b"/api/v1/auth/login/totp-setup/verify", b"/api/v1/auth/login/totp", b"/api/v1/auth/login/email-verification",
    b"/api/v1/auth/password-reset", b"/api/v1/auth/password-reset/confirm",
    b"/api/v1/auth/%6Cogin", b"/api/v1/%61uth/password-reset",
])
def test_login_rate_limit_counts_every_login_and_reset_step(path):
    assert _login_path_matches(path)


@pytest.mark.parametrize("path", [
    b"/api/v1/auth/me", b"/api/v1/auth/activity", b"/api/v1/auth/logout", b"/api/v1/incidents",
])
def test_login_rate_limit_skips_the_signed_in_session_calls(path):
    # 로그인한 화면이 화면을 열 때 · 입력이 있을 때 부른다 — 대입 대상이 아니다
    assert not _login_path_matches(path)


def test_login_rate_limit_starts_in_count_mode():
    # 정상 사용량을 재지 않았다 — 관측 전에는 막지 않는다(#522 C · B 와 같은 방식)
    assert _rules()["rate-login-per-ip"]["Action"] == {"Count": {}}


def test_rate_limit_starts_in_count_mode():
    # 정상 사용량을 재지 않았다 — 관측 전에는 막지 않는다(#484 ⓔ)
    rate = _rules()["rate-per-ip"]
    assert rate["Action"] == {"Count": {}}
    assert platform.RATE_WINDOW_SECONDS in (60, 120, 300, 600)  # WAF가 받는 창 길이


class FakeWaf:
    """IP 집합 · 웹 ACL 을 들고 있는 WAFv2 대역 — 생성 · 갱신 · 삭제가 상태를 바꾼다."""

    def __init__(self, ipsets=None, acl_rules=None, create_acl_errors=()):
        self.ipsets = {name: list(addrs) for name, addrs in (ipsets or {}).items()}
        self.acl_rules = acl_rules  # None = 웹 ACL 없음
        self.calls: list[str] = []
        # create_web_acl 이 차례로 던질 오류 코드 — 실제 WAF 의 IP 집합 전파 지연(#522)을 흉내 낸다
        self.create_acl_errors = list(create_acl_errors)

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
        if self.create_acl_errors:
            raise _client_error(self.create_acl_errors.pop(0), "CreateWebACL")
        self.acl_rules = Rules
        return {"Summary": {"ARN": "arn:acl"}}

    def update_web_acl(self, Rules, **_):
        self.calls.append("update_web_acl")
        self.acl_rules = Rules

    def delete_web_acl(self, **_):
        self.calls.append("delete_web_acl")
        self.acl_rules = None


ALLOW = ["203.0.113.7/32", "198.51.100.9/32"]
NATS = ["198.51.100.10/32", "198.51.100.9/32"]  # AZ a·c의 NAT. WAF가 정렬해 담는 순서


def test_new_acl_gets_the_rate_rule_and_a_separate_nat_set():
    waf = FakeWaf()
    platform._ensure_waf(waf, ALLOW, NATS)
    assert waf.ipsets[platform.WAF_NAT_IPSET] == NATS
    assert [r["Name"] for r in waf.acl_rules] == RULE_NAMES


def test_new_acl_waits_while_the_new_ip_sets_propagate(monkeypatch):
    # 새 계정 첫 up 에서 실제로 났다 — 방금 만든 IP 집합을 WAF 가 아직 못 찾는다(#522)
    slept: list[float] = []
    monkeypatch.setattr(platform.time, "sleep", slept.append)
    waf = FakeWaf(create_acl_errors=["WAFUnavailableEntityException", "WAFUnavailableEntityException"])
    assert platform._ensure_waf(waf, ALLOW, NATS) == "arn:acl"
    assert waf.calls.count("create_web_acl") == 3 and len(slept) == 2
    assert [r["Name"] for r in waf.acl_rules] == RULE_NAMES


def test_new_acl_does_not_retry_other_errors(monkeypatch):
    monkeypatch.setattr(platform.time, "sleep", lambda _: pytest.fail("다른 오류는 기다리지 않는다"))
    waf = FakeWaf(create_acl_errors=["WAFInvalidParameterException"])
    with pytest.raises(ClientError, match="WAFInvalidParameterException"):
        platform._ensure_waf(waf, ALLOW, NATS)
    assert waf.calls.count("create_web_acl") == 1


def test_existing_acl_without_the_rate_rule_is_updated():
    # 지금 배포된 플랫폼의 모양 — 허용 규칙 하나뿐인 웹 ACL(#484 「지금 상태」 ②)
    old = [{"Name": "allow-listed-ips", "Priority": 0, "Action": {"Allow": {}},
            "Statement": {"IPSetReferenceStatement": {"ARN": f"arn:{platform.WAF_IPSET}"}}}]
    waf = FakeWaf(ipsets={platform.WAF_IPSET: ALLOW}, acl_rules=old)
    platform._ensure_waf(waf, ALLOW, NATS)
    assert "update_web_acl" in waf.calls
    assert [r["Name"] for r in waf.acl_rules] == RULE_NAMES


def test_existing_acl_without_the_login_rule_gets_it_in_front():
    # 지금 배포된 플랫폼의 모양 — 전 경로 속도 규칙(0) · 허용(1)뿐인 웹 ACL(#484 적용 뒤)
    old = [rule for rule in platform.waf_rules(f"arn:{platform.WAF_IPSET}", f"arn:{platform.WAF_NAT_IPSET}")
           if rule["Name"] != "rate-login-per-ip"]
    waf = FakeWaf(ipsets={platform.WAF_IPSET: ALLOW, platform.WAF_NAT_IPSET: NATS},
                  acl_rules=[{**rule, "Priority": n} for n, rule in enumerate(old)])
    platform._ensure_waf(waf, ALLOW, NATS)
    assert waf.calls == ["update_web_acl"]
    assert [(r["Name"], r["Priority"]) for r in waf.acl_rules] == list(zip(RULE_NAMES, [0, 1, 2]))
    waf.calls.clear()
    platform._ensure_waf(waf, ALLOW, NATS)
    assert waf.calls == []


def test_second_run_changes_nothing():
    waf = FakeWaf()
    platform._ensure_waf(waf, ALLOW, NATS)
    waf.calls.clear()
    platform._ensure_waf(waf, ALLOW, NATS)
    assert waf.calls == []  # 응답에 기본 키가 덧붙어도 같은 규칙으로 본다


def test_switching_to_block_is_applied_on_rerun(monkeypatch):
    waf = FakeWaf()
    platform._ensure_waf(waf, ALLOW, NATS)
    monkeypatch.setattr(platform, "RATE_ACTION", "Block")
    waf.calls.clear()
    platform._ensure_waf(waf, ALLOW, NATS)
    assert waf.calls == ["update_web_acl"]
    actions = {rule["Name"]: rule["Action"] for rule in waf.acl_rules}
    assert actions["rate-per-ip"] == {"Block": {}}
    assert actions["rate-login-per-ip"] == {"Count": {}}  # 두 규칙의 전환은 따로다


def test_down_removes_the_acl_then_both_ip_sets():
    waf = FakeWaf(ipsets={platform.WAF_IPSET: ALLOW, platform.WAF_NAT_IPSET: NATS}, acl_rules=[])
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


_ONBOARDING_URL = "https://vigilantis-platform-onboarding-1.s3.ap-northeast-2.amazonaws.com/vigilantis-access.json"
_TASK_ARN = "arn:aws:iam::1:role/vigilantis-platform"
# api_container의 배포 공통 인자. 각 테스트는 여기에 바꿀 것만 덮는다
_API_KW = {"cognito": {"pool_id": "ap-northeast-2_Abc", "client_id": "client-1"},
           "onboarding_url": _ONBOARDING_URL, "platform_principal_arn": _TASK_ARN}


def _api(dispatch: bool = False) -> dict:
    return platform.api_container(
        "repo/api:t", region="ap-northeast-2", cors_origin="https://d.cloudfront.net", dispatch_enabled=dispatch,
        secrets_arns={"DATABASE_URL": "arn:s1", "OPENAI_API_KEY": "arn:s2", "COGNITO_CLIENT_SECRET": "arn:s3"},
        **_API_KW)


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
                      "COGNITO_CLIENT_SECRET", "AWS_ENDPOINT_URL"):
        assert forbidden not in env
    assert {s["name"] for s in container["secrets"]} == {"DATABASE_URL", "OPENAI_API_KEY", "COGNITO_CLIENT_SECRET"}


def test_api_reaches_customers_only_through_tenant_roles():
    """관제 범위 필터와 플랫폼 단위 접근 역할이 없다. 문맥 없는 AWS 호출은 플랫폼 계정으로 새지 않고 예외다."""
    env = {e["name"]: e["value"] for e in _api()["environment"]}
    assert env["TENANT_CONTEXT_REQUIRED"] == "true"
    for gone in ("COLLECT_VPC_IDS", "VIGILANTIS_ACCESS_ROLE_ARN", "VIGILANTIS_ACCESS_EXTERNAL_ID"):
        assert gone not in env


def test_api_gets_login_and_onboarding_settings():
    env = {e["name"]: e["value"] for e in _api()["environment"]}
    assert (env["COGNITO_REGION"], env["COGNITO_USER_POOL_ID"], env["COGNITO_CLIENT_ID"]) == (
        "ap-northeast-2", "ap-northeast-2_Abc", "client-1")
    assert env["TENANT_ONBOARDING_TEMPLATE_URL"] == _ONBOARDING_URL
    assert env["PLATFORM_PRINCIPAL_ARN"] == _TASK_ARN
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
    """어떤 호출도 답하지 않는다. 기록만 하고 AttributeError로 멈춘다."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AttributeError(name)


def test_existing_role_without_platform_tag_is_not_overwritten():
    iam = FakeIam(roles={platform.ROLE_TASK: [{"Key": "owner", "Value": "someone-else"}]})
    with pytest.raises(SystemExit, match="플랫폼 태그"):
        platform._ensure_role(iam, platform.ROLE_TASK, {"Statement": []}, "d")
    assert iam.calls == []  # 신뢰 정책을 덮어쓰지 않았다


def test_existing_platform_role_gets_its_trust_policy_refreshed():
    iam = FakeIam(roles={platform.ROLE_TASK: _PLATFORM_TAGS})
    arn = platform._ensure_role(iam, platform.ROLE_TASK, {"Statement": []}, "d")
    assert arn.endswith(f":role/{platform.ROLE_TASK}")
    assert iam.calls == [("update_assume_role_policy", platform.ROLE_TASK)]


def test_down_deletes_only_platform_tagged_roles():
    iam = FakeIam(roles={platform.ROLE_EXEC: [], platform.ROLE_TASK: _PLATFORM_TAGS})
    platform._delete_roles(iam)
    assert set(iam.roles) == {platform.ROLE_EXEC}  # 태그 없는 같은 이름 역할은 남는다
    assert iam.calls and all(role == platform.ROLE_TASK for _, role in iam.calls)


def test_roles_carry_the_login_pool_and_drop_the_old_task_policy():
    iam = FakeIam()
    platform._ensure_roles(iam, ["arn:secret"], POOL_ARN)
    assert set(iam.roles) == {platform.ROLE_EXEC, platform.ROLE_TASK}  # 고객 계정 역할은 만들지 않는다
    assert iam.inline[(platform.ROLE_TASK, platform.TASK_POLICY)] == platform.task_role_policy(POOL_ARN)
    assert ("delete_role_policy", platform.ROLE_TASK) in iam.calls  # 5차 개정 전 정책 이름을 걷는다


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
_ORIGIN_CERT = {"DomainName": f"origin.{_DOMAIN}",
                "CertificateArn": "arn:aws:acm:ap-northeast-2:123456789012:certificate/o"}


@pytest.mark.parametrize("certs, zones, missing", [
    ([], [_ZONE], r"us-east-1 ACM 인증서\(ISSUED\) 없음"),
    ([_CERT], [], "호스팅 영역 없음"),
    ([_CERT], [{**_ZONE, "Config": {"PrivateZone": True}}], "호스팅 영역 없음"),  # 비공개 영역은 별칭을 못 단다
])
def test_up_stops_before_creating_anything_when_the_domain_is_not_ready(certs, zones, missing):
    """CloudFront 인증서·공개 호스팅 영역이 없으면 VPC·RDS·WAF 어느 것도 바꾸기 전에 멈춘다."""
    ec2, r53 = RecordingEc2(), FakeR53(zones)
    c = {"iam": FakeIam(), "ec2": ec2, "acm": FakeAcm(certs), "r53": r53, "acm_origin": FakeAcm([_ORIGIN_CERT])}
    with pytest.raises(SystemExit, match=missing):
        platform.up(c, ACCOUNT, "ap-northeast-2", ["203.0.113.7/32"], _DOMAIN)
    assert ec2.calls == [] and r53.calls == []  # c에 sm·rds·waf·cf가 없으니 그쪽 호출은 KeyError로 드러난다


def test_missing_origin_certificate_does_not_stop_the_preview():
    """ALB용 서울 리전 인증서는 up --yes가 직접 받는다. 없다고 멈추지 않고 None으로 알린다."""
    c = {"acm": FakeAcm([_CERT]), "r53": FakeR53([_ZONE]), "acm_origin": FakeAcm([])}
    assert platform._require_custom_domain(c, _DOMAIN) == (_CERT["CertificateArn"], "Z1", None)


class IssuingAcm:
    """origin 인증서 요청 · 검증 레코드 · 발급 대기를 흉내 낸다. 검증 레코드는 두 번째 조회에야 나온다."""

    def __init__(self, pending=()):
        self.pending = list(pending)
        self.calls: list[str] = []
        self._described = 0

    def get_paginator(self, name):
        acm = self

        class _P:
            def paginate(self, CertificateStatuses):
                rows = [] if CertificateStatuses == ["ISSUED"] else acm.pending
                return iter([{"CertificateSummaryList": rows}])
        return _P()

    def request_certificate(self, DomainName, ValidationMethod, Tags):
        self.calls.append(f"request:{DomainName}:{ValidationMethod}")
        return {"CertificateArn": "arn:new"}

    def describe_certificate(self, CertificateArn):
        self._described += 1
        record = {"Name": "_x.origin.vigilantis.example.", "Type": "CNAME", "Value": "_y.acm-validations.aws."}
        return {"Certificate": {"DomainValidationOptions": [{"ResourceRecord": record} if self._described > 1 else {}]}}

    def get_waiter(self, name):
        acm = self

        class _W:
            def wait(self, CertificateArn, **_):
                acm.calls.append(f"{name}:{CertificateArn}")
        return _W()


class RecordingR53:
    def __init__(self):
        self.changes: list[dict] = []

    def change_resource_record_sets(self, HostedZoneId, ChangeBatch):
        self.changes.append({"zone": HostedZoneId, **ChangeBatch["Changes"][0]})


def test_origin_certificate_is_requested_validated_and_awaited(monkeypatch):
    monkeypatch.setattr(platform.time, "sleep", lambda _: None)
    acm, r53 = IssuingAcm(), RecordingR53()
    assert platform._ensure_origin_certificate(acm, r53, "Z1", _DOMAIN) == "arn:new"
    assert acm.calls == [f"request:origin.{_DOMAIN}:DNS", "certificate_validated:arn:new"]
    (change,) = r53.changes
    assert (change["zone"], change["Action"], change["ResourceRecordSet"]["Type"]) == ("Z1", "UPSERT", "CNAME")


def test_pending_origin_certificate_is_reused_not_requested_again(monkeypatch):
    monkeypatch.setattr(platform.time, "sleep", lambda _: None)
    acm = IssuingAcm(pending=[{**_ORIGIN_CERT, "CertificateArn": "arn:pending"}])
    assert platform._ensure_origin_certificate(acm, RecordingR53(), "Z1", _DOMAIN) == "arn:pending"
    assert not [c for c in acm.calls if c.startswith("request:")]


def test_up_preview_also_checks_the_domain(monkeypatch):
    """--yes 없는 미리 보기에서 먼저 알려야 --yes 실행이 중간에 멈추지 않는다."""
    _fake_main(monkeypatch, ["up", "--account", ACCOUNT, "--allow-cidr", "203.0.113.7/32", "--domain", _DOMAIN],
               {"iam": FakeIam(), "ec2": RecordingEc2(), "acm": FakeAcm(), "route53": FakeR53([_ZONE])})
    with pytest.raises(SystemExit, match=r"인증서\(ISSUED\) 없음"):
        platform.main()


def _fake_main(monkeypatch, argv: list[str], fakes: dict) -> None:
    monkeypatch.setattr(platform, "_require_real_aws", lambda account: {"Account": account})
    monkeypatch.setattr(platform, "default_region", lambda: "ap-northeast-2")
    monkeypatch.setattr(platform, "aws_client", lambda name, region=None: fakes.get(name, object()))
    monkeypatch.setattr(sys, "argv", ["provision_platform_aws.py", *argv])


def test_up_rejects_a_wide_cidr_before_touching_aws(monkeypatch):
    iam = FakeIam()
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
        self.deletes: list[tuple[str, str]] = []

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.puts.append((RoleName, PolicyName, json.loads(PolicyDocument)))

    def delete_role_policy(self, RoleName, PolicyName):
        super().delete_role_policy(RoleName, PolicyName)
        self.deletes.append((RoleName, PolicyName))


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
    monkeypatch.setattr(platform, "_find_user_pool", lambda cidp: {"id": "pool-1", "arn": "arn:pool"})
    monkeypatch.setattr(platform, "_find_app_client", lambda cidp, pool_id: "client-1")
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
    c = {"ec2": None, "cf": None, "sm": None, "ecr": None, "cidp": None, "iam": iam, "ecs": ecs}
    with pytest.raises(SystemExit, match="notification-iam"):
        platform.deploy(c, ACCOUNT, REGION, "t", False, plan)
    assert ecs.writes == [] and iam.puts == [] and iam.calls == []


def test_deploy_registration_failure_leaves_iam_unchanged(tmp_path, monkeypatch):
    _patch_deploy(monkeypatch)
    plan = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    iam = PolicyIam(_exact_docs(plan), roles=_both_roles())
    ecs = FakeEcs()
    c = {"ec2": None, "cf": None, "sm": None, "ecr": None, "cidp": None, "iam": iam, "ecs": ecs}
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
    c = platform.api_container("i", region=REGION, cors_origin="https://x", dispatch_enabled=False,
                               secrets_arns={"DATABASE_URL": "a"}, notification=plan, **_API_KW)
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
        platform.api_container("i", region=REGION, cors_origin="https://x", dispatch_enabled=False,
                               secrets_arns={"DATABASE_URL": "a"}, notification=plan, **_API_KW)


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
    # 토픽 정책과 함께, 입력이 켜지 않은 고객 설정 정책(T-7)도 남아 있으면 지운다
    assert iam.deletes == [(platform.ROLE_TASK, platform.NOTIFY_POLICY_TASK),
                           (platform.ROLE_TASK, platform.SETTINGS_POLICY_TASK)]


def test_revoke_deletes_only_our_policies_on_tagged_roles():
    iam = NotifIam(roles={platform.ROLE_TASK: _PLATFORM_TAGS, platform.ROLE_EXEC: []})
    platform.revoke_notification_policies(iam, QuietEcs())
    assert iam.deletes == [(platform.ROLE_TASK, platform.NOTIFY_POLICY_TASK),
                           (platform.ROLE_TASK, platform.SETTINGS_POLICY_TASK)]
    assert all(role == platform.ROLE_TASK for _, role in iam.calls)
    assert iam.puts == []


# ⑩ 고객 설정 자원 관리(T-7) — 접두 아래 토픽·비밀만, 켜야 받고, 끄면 거둔다 ----------------------
_T7_TENANT = "22222222-2222-4222-8222-222222222222"
_T7_GEN = "33333333-3333-4333-8333-333333333333"
_T7_ARNS = platform.settings_resource_arns(ACCOUNT, REGION)
_SNS_PREFIX = platform.SETTINGS_SNS_TOPIC_PREFIX
_SECRET_PREFIX = platform.SETTINGS_SECRET_PREFIX


def _iam_match(pattern: str, arn: str) -> bool:
    """IAM Resource 와일드카드 — `*`는 `:`·`/`를 포함한 모든 문자에 맞는다."""
    return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), arn) is not None


def _t7_cfg(tmp_path, **over):
    # T-7 전용 입력 — 라우트·Slack 비밀 ARN은 없고(런타임이 DB·관리 자원에서 읽는다) 관리 권한만 켠다
    return _cfg(tmp_path, routes=[], slack_secret_arns={}, tenant_settings=True, **over)


def _statements(doc: dict) -> dict[str, dict]:
    return {s["Sid"]: s for s in doc["Statement"]}


def test_settings_policy_uses_the_shared_prefixes_of_this_account_and_region():
    assert _T7_ARNS == {"topics": f"arn:aws:sns:{REGION}:{ACCOUNT}:{_SNS_PREFIX}*",
                        "secrets": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{_SECRET_PREFIX}*"}
    stmts = _statements(platform.settings_task_policy(_T7_ARNS))
    assert set(stmts) == {"ManageTenantSettingsTopicsOnly", "SubscribeTenantSettingsTopicsByEmailOnly",
                          "ManageTenantSettingsSecretsOnly"}
    assert all(s["Effect"] == "Allow" for s in stmts.values())
    topics, subscribe, secrets = (stmts["ManageTenantSettingsTopicsOnly"],
                                  stmts["SubscribeTenantSettingsTopicsByEmailOnly"],
                                  stmts["ManageTenantSettingsSecretsOnly"])
    assert topics["Resource"] == subscribe["Resource"] == _T7_ARNS["topics"]
    assert secrets["Resource"] == _T7_ARNS["secrets"]
    # 런타임(services/aws/settings_notifications.py · notifications/channels.py)이 부르는 호출과 1:1
    assert sorted(topics["Action"]) == sorted(platform.SETTINGS_SNS_ACTIONS) == [
        "sns:CreateTopic", "sns:DeleteTopic", "sns:GetSubscriptionAttributes", "sns:ListSubscriptionsByTopic",
        "sns:Publish"]
    assert subscribe["Action"] == "sns:Subscribe"
    assert subscribe["Condition"] == {"StringEquals": {"sns:Protocol": "email"}}
    assert sorted(secrets["Action"]) == sorted(platform.SETTINGS_SECRET_ACTIONS) == [
        "secretsmanager:CreateSecret", "secretsmanager:DeleteSecret", "secretsmanager:GetSecretValue"]
    assert "Condition" not in topics and "Condition" not in secrets


def test_settings_policy_grants_no_wildcard_action_or_account_wide_listing():
    doc = platform.settings_task_policy(_T7_ARNS)
    actions = {a for s in doc["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
    assert not any("*" in a for a in actions)
    # 자원 ARN으로 좁힐 수 없거나(계정 전체 나열) 권한·정책·암호화를 넓히는 호출, 런타임이 부르지 않는 호출은 없다
    assert not actions & {"sns:ListTopics", "sns:ListSubscriptions", "sns:Unsubscribe", "sns:SetTopicAttributes", "sns:AddPermission", "sns:RemovePermission", "sns:TagResource",
                          "sns:ConfirmSubscription", "secretsmanager:ListSecrets", "secretsmanager:DescribeSecret",
                          "secretsmanager:PutSecretValue", "secretsmanager:UpdateSecret",
                          "secretsmanager:PutResourcePolicy", "secretsmanager:RestoreSecret", "kms:Decrypt"}
    assert all(s["Resource"] != "*" for s in doc["Statement"])
    assert "sts:AssumeRole" not in actions  # 접근 역할 전환은 기존 정책(assume-access-role) 그대로


@pytest.mark.parametrize("arn", [
    f"arn:aws:sns:{REGION}:{ACCOUNT}:{_SNS_PREFIX}{_T7_TENANT}-{_T7_GEN}",
    f"arn:aws:sns:{REGION}:{ACCOUNT}:{_SNS_PREFIX}{_T7_TENANT}-{_T7_GEN}:9f1c2d3e-sub",  # 구독 ARN
])
def test_topic_pattern_covers_managed_topics_and_their_subscriptions(arn):
    assert _iam_match(_T7_ARNS["topics"], arn)


@pytest.mark.parametrize("arn", [
    TOPIC,                                                            # 레거시 입력의 알림 토픽
    f"arn:aws:sns:{REGION}:{ACCOUNT}:{platform.ALARM_TOPIC}",         # 플랫폼 오류 알람 토픽
    f"arn:aws:sns:{REGION}:{ACCOUNT}:vigilantis-t7",                  # 접두에서 하이픈이 빠진 이름
    f"arn:aws:sns:{REGION}:{ACCOUNT}:xvigilantis-t7-{_T7_TENANT}",    # 접두가 맨 앞이 아님
    f"arn:aws:sns:{REGION}:999900001111:{_SNS_PREFIX}{_T7_TENANT}",   # 고객 계정
    f"arn:aws:sns:us-east-1:{ACCOUNT}:{_SNS_PREFIX}{_T7_TENANT}",     # 다른 리전
])
def test_topic_pattern_excludes_other_topics_accounts_and_regions(arn):
    assert not _iam_match(_T7_ARNS["topics"], arn)


@pytest.mark.parametrize("arn", [
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{_SECRET_PREFIX}{_T7_TENANT}/{_T7_GEN}-AbCdEf",
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{_SECRET_PREFIX}{_T7_TENANT}-AbCdEf",
])
def test_secret_pattern_covers_managed_secrets_with_their_random_suffix(arn):
    assert _iam_match(_T7_ARNS["secrets"], arn)


@pytest.mark.parametrize("arn", [
    SECRET,                                                                            # 레거시 Slack 비밀
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{platform.SECRET_OPENAI}-AbCdEf",
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{platform.SECRET_DATABASE_URL}-AbCdEf",
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{platform.SECRET_COGNITO_CLIENT}-AbCdEf",
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:vigilantis/t7-AbCdEf",           # 접두 끝 슬래시 없음
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:x/{_SECRET_PREFIX}{_T7_TENANT}-AbCdEf",
    f"arn:aws:secretsmanager:{REGION}:999900001111:secret:{_SECRET_PREFIX}{_T7_TENANT}-AbCdEf",
    f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:{_SECRET_PREFIX}{_T7_TENANT}-AbCdEf",
])
def test_secret_pattern_excludes_platform_secrets_other_accounts_and_regions(arn):
    assert not _iam_match(_T7_ARNS["secrets"], arn)


def test_tenant_settings_is_off_unless_the_config_says_true(tmp_path):
    assert platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)["settings_arns"] == {}
    assert platform.load_notification_config(_cfg(tmp_path, tenant_settings=False), ACCOUNT, REGION)["settings_arns"] == {}
    assert platform.load_notification_config(_t7_cfg(tmp_path), ACCOUNT, REGION)["settings_arns"] == _T7_ARNS
    assert platform.load_notification_config(_t7_cfg(tmp_path, enabled=False), ACCOUNT, REGION) is None
    for value in ("true", 1, None):
        with pytest.raises(SystemExit, match="tenant_settings"):
            platform.load_notification_config(_cfg(tmp_path, tenant_settings=value), ACCOUNT, REGION)


def test_tenant_settings_plan_can_coexist_with_legacy_routes(tmp_path):
    plan = platform.load_notification_config(_cfg(tmp_path, tenant_settings=True), ACCOUNT, REGION)
    assert plan["topic_arns"] == [TOPIC] and list(plan["secret_arns"].values()) == [SECRET]
    assert plan["settings_arns"] == _T7_ARNS


def test_tenant_settings_adds_no_env_and_no_secret_reference_to_the_container(tmp_path):
    plan = platform.load_notification_config(_t7_cfg(tmp_path), ACCOUNT, REGION)
    c = platform.api_container("i", region=REGION, cors_origin="https://x", dispatch_enabled=False,
                               secrets_arns={"DATABASE_URL": "a"}, notification=plan, **_API_KW)
    env = {e["name"]: e["value"] for e in c["environment"]}
    assert env["NOTIFICATION_ENABLED"] == "true" and env["NOTIFICATION_ROUTES_JSON"] == '{"routes":[]}'
    assert [s["name"] for s in c["secrets"]] == ["DATABASE_URL"]
    assert _SNS_PREFIX not in json.dumps(c) and _SECRET_PREFIX not in json.dumps(c)


def test_apply_with_tenant_settings_writes_the_settings_policy_on_the_task_role_only(tmp_path):
    iam = NotifIam(roles=_both_roles())
    plan = platform.load_notification_config(_t7_cfg(tmp_path), ACCOUNT, REGION)
    platform.apply_notification_policies(iam, QuietEcs(), plan)
    assert [(r, p) for r, p, _ in iam.puts] == [(platform.ROLE_TASK, platform.SETTINGS_POLICY_TASK)]
    assert iam.puts[0][2] == platform.settings_task_policy(_T7_ARNS)
    # 라우트·Slack 비밀이 없는 입력이라 알림 정책 둘은 지운다 — 실행 역할에는 T-7 권한을 주지 않는다
    assert iam.deletes == [(platform.ROLE_TASK, platform.NOTIFY_POLICY_TASK),
                           (platform.ROLE_EXEC, platform.NOTIFY_POLICY_EXEC)]


def test_apply_with_tenant_settings_still_refuses_an_active_api(tmp_path):
    iam = NotifIam(roles=_both_roles())
    plan = platform.load_notification_config(_t7_cfg(tmp_path), ACCOUNT, REGION)
    with pytest.raises(SystemExit, match="정책은 바꾸지 않았다"):
        platform.apply_notification_policies(iam, FakeEcs(service=_nsvc(), tasks=[_task()], env=_on()), plan)
    assert iam.puts == [] and iam.deletes == []


_T7_KEY = (platform.ROLE_TASK, platform.SETTINGS_POLICY_TASK)


def _t7_docs(plan):
    return {_T7_KEY: platform.settings_task_policy(plan["settings_arns"])}


def test_verify_requires_the_settings_policy_to_match_the_plan_exactly(tmp_path):
    plan = platform.load_notification_config(_t7_cfg(tmp_path), ACCOUNT, REGION)
    platform.verify_notification_policies(PolicyIam(_t7_docs(plan), roles=_both_roles()), plan)
    # 문장 순서·Sid가 달라도 같은 내용이면 통과
    docs = _t7_docs(plan)
    docs[_T7_KEY]["Statement"] = [dict(s, Sid=f"x{i}") for i, s in enumerate(reversed(docs[_T7_KEY]["Statement"]))]
    platform.verify_notification_policies(PolicyIam(docs, roles=_both_roles()), plan)
    bad = [
        lambda d: d.clear(),                                                       # 켰는데 정책이 없다
        lambda d: _statements(d[_T7_KEY])["ManageTenantSettingsTopicsOnly"].update(Resource="*"),
        lambda d: _statements(d[_T7_KEY])["ManageTenantSettingsSecretsOnly"].update(
            Resource=f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:*"),
        lambda d: _statements(d[_T7_KEY])["ManageTenantSettingsTopicsOnly"]["Action"].append("sns:SetTopicAttributes"),
        lambda d: _statements(d[_T7_KEY])["SubscribeTenantSettingsTopicsByEmailOnly"].pop("Condition"),  # 프로토콜 제한 빠짐
        lambda d: _statements(d[_T7_KEY])["SubscribeTenantSettingsTopicsByEmailOnly"].update(
            Condition={"StringEquals": {"sns:Protocol": "https"}}),
        lambda d: d[_T7_KEY]["Statement"].append({"Effect": "Allow", "Action": "sns:Publish", "Resource": TOPIC}),
        lambda d: d[_T7_KEY]["Statement"].pop(),                                   # 비밀 문장이 빠짐
    ]
    for mutate in bad:
        d = _t7_docs(plan)
        mutate(d)
        with pytest.raises(SystemExit, match="notification-iam"):
            platform.verify_notification_policies(PolicyIam(d, roles=_both_roles()), plan)


def test_verify_rejects_a_leftover_settings_policy_when_the_config_does_not_ask_for_it(tmp_path):
    legacy = platform.load_notification_config(_cfg(tmp_path), ACCOUNT, REGION)
    docs = _exact_docs(legacy)
    platform.verify_notification_policies(PolicyIam(docs, roles=_both_roles()), legacy)
    docs[_T7_KEY] = platform.settings_task_policy(_T7_ARNS)
    with pytest.raises(SystemExit, match="notification-iam"):
        platform.verify_notification_policies(PolicyIam(docs, roles=_both_roles()), legacy)


def test_deploy_with_tenant_settings_reads_iam_only_and_stops_on_mismatch(tmp_path, monkeypatch):
    _patch_deploy(monkeypatch)
    plan = platform.load_notification_config(_t7_cfg(tmp_path), ACCOUNT, REGION)
    iam, ecs = PolicyIam({}, roles=_both_roles()), FakeEcs()
    c = {"ec2": None, "cf": None, "sm": None, "ecr": None, "cidp": None, "iam": iam, "ecs": ecs}
    with pytest.raises(SystemExit, match="notification-iam"):
        platform.deploy(c, ACCOUNT, REGION, "t", False, plan)
    assert ecs.writes == [] and iam.puts == [] and iam.deletes == []
    iam = PolicyIam(_t7_docs(plan), roles=_both_roles())
    c["iam"] = iam
    with pytest.raises(RuntimeError):  # 정책이 맞으면 등록까지 간다 — 그래도 IAM은 쓰지 않는다
        platform.deploy(c, ACCOUNT, REGION, "t", False, plan)
    assert ecs.writes == ["register_task_definition"] and iam.puts == [] and iam.deletes == []


def test_iam_preview_and_deploy_preview_with_tenant_settings_write_nothing(tmp_path, monkeypatch, capsys):
    iam = NotifIam(roles=_both_roles())
    _fake_main(monkeypatch, ["notification-iam", "--account", ACCOUNT, "--notification-config", _t7_cfg(tmp_path)],
               {"iam": iam, "secretsmanager": NotifSm(), "ecs": QuietEcs()})
    platform.main()
    assert iam.puts == [] and iam.deletes == [] and iam.calls == []
    out = capsys.readouterr().out
    assert platform.SETTINGS_POLICY_TASK in out and _T7_ARNS["topics"] in out and "--yes" in out


def test_iam_with_yes_and_tenant_settings_applies_only_the_dedicated_policies(tmp_path, monkeypatch):
    iam = NotifIam(roles=_both_roles())
    _fake_main(monkeypatch, ["notification-iam", "--account", ACCOUNT, "--notification-config", _t7_cfg(tmp_path),
                             "--yes"], {"iam": iam, "secretsmanager": NotifSm(), "ecs": QuietEcs()})
    platform.main()
    assert [(r, p) for r, p, _ in iam.puts] == [(platform.ROLE_TASK, platform.SETTINGS_POLICY_TASK)]
    assert iam.puts[0][2] == platform.settings_task_policy(_T7_ARNS)
    assert (platform.TASK_POLICY not in {p for _, p, _ in iam.puts}
            and "read-platform-secrets" not in {p for _, p, _ in iam.puts})


def test_down_removes_the_settings_policy_before_deleting_the_task_role():
    iam = NotifIam(roles={platform.ROLE_TASK: _PLATFORM_TAGS})
    platform._delete_roles(iam)
    assert (platform.ROLE_TASK, platform.SETTINGS_POLICY_TASK) in iam.deletes
    assert platform.ROLE_TASK not in iam.roles


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


# ⑨ 운영 장치: DB Multi-AZ(ADR-0010 5차 개정) --------------------------------------
def test_new_db_is_multi_az():
    events: list = []
    rds = FakeRds(events)
    platform._ensure_db(rds, FakeSm(events), _SUBNETS, "sg-db")
    assert rds.created["MultiAZ"] is True


def test_existing_single_az_db_is_switched_to_multi_az():
    events: list = []
    rds = FakeRds(events, exists=True)
    rds.db.update(DBInstanceStatus="available", BackupRetentionPeriod=platform.DB_BACKUP_DAYS, MultiAZ=False,
                  Endpoint={"Address": FakeRds.HOST})
    sm = FakeSm(events, {platform.SECRET_DATABASE_URL: platform.database_url("pw", FakeRds.HOST)})
    platform._ensure_db(rds, sm, _SUBNETS, "sg-db")
    assert rds.modified == {"DBInstanceIdentifier": platform.DB_INSTANCE, "MultiAZ": True}


def test_multi_az_db_with_a_week_of_backups_is_left_alone():
    events: list = []
    rds = FakeRds(events, exists=True)
    rds.db.update(DBInstanceStatus="available", BackupRetentionPeriod=platform.DB_BACKUP_DAYS, MultiAZ=True,
                  Endpoint={"Address": FakeRds.HOST})
    sm = FakeSm(events, {platform.SECRET_DATABASE_URL: platform.database_url("pw", FakeRds.HOST)})
    platform._ensure_db(rds, sm, _SUBNETS, "sg-db")
    assert events == []


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
    ec2 = RecordingEc2()
    c = {"iam": FakeIam(), "ec2": ec2, "cw": FakeCw({_API_DOWN: []}), "sns": FakeSns()}
    with pytest.raises(SystemExit, match=_API_DOWN):
        platform.up(c, ACCOUNT, "ap-northeast-2", ["203.0.113.7/32"])
    assert ec2.calls == []  # VPC·NAT·RDS 어느 것도 만들지 않았다


# ② 네트워크: AZ별 NAT · AZ별 프라이빗 경로(ADR-0010 5차 개정) ------------------------
class NetEc2:
    """IGW · 경로 · NAT · 탄력적 IP만 흉내 낸다. 필터는 이름(tag:Name) · VPC · 서브넷 연결만 본다."""

    def __init__(self, nats=None, tables=None):
        self.nats = list(nats or [])
        self.tables = list(tables or [])
        self.addresses: list[dict] = []
        self.calls: list[tuple] = []
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}-{self._n}"

    @staticmethod
    def _name(tags):
        return next((t["Value"] for t in tags if t["Key"] == "Name"), None)

    def describe_internet_gateways(self, Filters):
        return {"InternetGateways": [{"InternetGatewayId": "igw-1"}]}

    def describe_route_tables(self, Filters):
        by = {f["Name"]: f["Values"][0] for f in Filters}
        rows = self.tables
        if "tag:Name" in by:
            rows = [t for t in rows if self._name(t["Tags"]) == by["tag:Name"]]
        if "association.subnet-id" in by:
            rows = [t for t in rows if any(a.get("SubnetId") == by["association.subnet-id"]
                                           for a in t["Associations"])]
        return {"RouteTables": [dict(t, Associations=list(t["Associations"])) for t in rows]}

    def create_route_table(self, VpcId, TagSpecifications):
        rt = {"RouteTableId": self._id("rtb"), "Tags": TagSpecifications[0]["Tags"], "Routes": [], "Associations": []}
        self.tables.append(rt)
        return {"RouteTable": rt}

    def _table(self, rt_id):
        return next(t for t in self.tables if t["RouteTableId"] == rt_id)

    def create_route(self, RouteTableId, DestinationCidrBlock, **target):
        self._table(RouteTableId)["Routes"].append({"DestinationCidrBlock": DestinationCidrBlock, **target})

    def associate_route_table(self, RouteTableId, SubnetId):
        if any(a.get("SubnetId") == SubnetId for t in self.tables for a in t["Associations"]):
            raise _client_error("Resource.AlreadyAssociated", "AssociateRouteTable")
        self._table(RouteTableId)["Associations"].append(
            {"SubnetId": SubnetId, "RouteTableAssociationId": self._id("rtbassoc")})

    def replace_route_table_association(self, AssociationId, RouteTableId):
        self.calls.append(("replace_route_table_association", RouteTableId))
        for t in self.tables:
            for a in list(t["Associations"]):
                if a["RouteTableAssociationId"] == AssociationId:
                    t["Associations"].remove(a)
                    self._table(RouteTableId)["Associations"].append(a)

    def delete_route_table(self, RouteTableId):
        self.calls.append(("delete_route_table", self._name(self._table(RouteTableId)["Tags"])))
        self.tables = [t for t in self.tables if t["RouteTableId"] != RouteTableId]

    def describe_addresses(self, Filters):
        name = next(f["Values"][0] for f in Filters if f["Name"] == "tag:Name")
        return {"Addresses": [a for a in self.addresses if a["Name"] == name]}

    def allocate_address(self, Domain, TagSpecifications):
        alloc = self._id("eipalloc")
        self.addresses.append({"AllocationId": alloc, "Name": self._name(TagSpecifications[0]["Tags"])})
        return {"AllocationId": alloc}

    def create_nat_gateway(self, SubnetId, AllocationId, TagSpecifications):
        nat = {"NatGatewayId": self._id("nat"), "SubnetId": SubnetId, "State": "available",
               "NatGatewayAddresses": [{"PublicIp": f"198.51.100.{10 + len(self.nats)}"}]}
        self.nats.append(nat)
        self.calls.append(("create_nat_gateway", SubnetId))
        return {"NatGateway": nat}

    def describe_nat_gateways(self, Filters=None, NatGatewayIds=None):
        rows = [n for n in self.nats if not NatGatewayIds or n["NatGatewayId"] in NatGatewayIds]
        return {"NatGateways": rows}

    def get_waiter(self, name):
        class _Waiter:
            def wait(self, **kwargs):
                pass
        return _Waiter()


_NET_SUBNETS = {f"{platform.PREFIX}-{tier}-{az}": f"subnet-{tier}-{az}"
                for tier in ("public", "private") for az in platform.PRIVATE_SUBNETS}


def _private_routes(ec2):
    """프라이빗 서브넷 → 그 서브넷 경로의 NAT가 놓인 공개 서브넷."""
    nat_subnet = {n["NatGatewayId"]: n["SubnetId"] for n in ec2.nats}
    return {a["SubnetId"]: nat_subnet[r["NatGatewayId"]]
            for t in ec2.tables for a in t["Associations"] for r in t["Routes"] if "NatGatewayId" in r
            if a["SubnetId"].startswith("subnet-private")}


def test_new_network_gets_one_nat_and_one_private_route_per_az():
    ec2 = NetEc2()
    ips = platform._ensure_network(ec2, "vpc-1", _NET_SUBNETS)
    assert [c for c in ec2.calls if c[0] == "create_nat_gateway"] == [
        ("create_nat_gateway", f"subnet-public-{az}") for az in platform.PRIVATE_SUBNETS]
    # 각 AZ의 프라이빗 서브넷은 같은 AZ의 NAT로 나간다. 한 AZ가 죽어도 다른 AZ는 밖으로 나간다
    assert _private_routes(ec2) == {f"subnet-private-{az}": f"subnet-public-{az}" for az in platform.PRIVATE_SUBNETS}
    assert len(ips) == len(set(ips)) == len(platform.PRIVATE_SUBNETS)  # WAF에 NAT IP 전부가 들어간다


def test_second_network_run_changes_nothing():
    ec2 = NetEc2()
    first = platform._ensure_network(ec2, "vpc-1", _NET_SUBNETS)
    ec2.calls.clear()
    assert platform._ensure_network(ec2, "vpc-1", _NET_SUBNETS) == first
    assert ec2.calls == []


def test_single_nat_network_is_moved_to_per_az_routes():
    """5차 개정 전 모양: AZ a의 NAT 하나와 두 프라이빗 서브넷이 함께 묶인 공용 경로."""
    legacy_nat = {"NatGatewayId": "nat-old", "SubnetId": "subnet-public-a", "State": "available",
                  "NatGatewayAddresses": [{"PublicIp": "198.51.100.9"}]}
    legacy = {"RouteTableId": "rtb-old", "Tags": platform._tags(platform.LEGACY_PRIVATE_TABLE),
              "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-old"}],
              "Associations": [{"SubnetId": f"subnet-private-{az}", "RouteTableAssociationId": f"assoc-{az}"}
                               for az in platform.PRIVATE_SUBNETS]}
    public = {"RouteTableId": "rtb-pub", "Tags": platform._tags(f"{platform.PREFIX}-public"),
              "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"}],
              "Associations": [{"SubnetId": f"subnet-public-{az}", "RouteTableAssociationId": f"pub-{az}"}
                               for az in platform.PRIVATE_SUBNETS]}
    ec2 = NetEc2(nats=[legacy_nat], tables=[public, legacy])
    ips = platform._ensure_network(ec2, "vpc-1", _NET_SUBNETS)
    # AZ a의 옛 NAT는 그대로 쓰고(공인 IP가 바뀌지 않는다) AZ c에만 새로 만든다
    assert [c for c in ec2.calls if c[0] == "create_nat_gateway"] == [("create_nat_gateway", "subnet-public-c")]
    assert ips[0] == "198.51.100.9"
    assert _private_routes(ec2) == {f"subnet-private-{az}": f"subnet-public-{az}" for az in platform.PRIVATE_SUBNETS}
    # 서브넷을 다른 경로로 옮겼다. 새로 묶으려 했으면 AlreadyAssociated로 멈췄다
    assert sum(c[0] == "replace_route_table_association" for c in ec2.calls) == len(platform.PRIVATE_SUBNETS)
    assert ("delete_route_table", platform.LEGACY_PRIVATE_TABLE) in ec2.calls


def test_down_releases_old_and_per_az_nat_addresses():
    assert platform.nat_eip_names() == [f"{platform.PREFIX}-nat"] + [
        f"{platform.PREFIX}-nat-{az}" for az in platform.PRIVATE_SUBNETS]


# ② 로그인(Cognito) 구성 ---------------------------------------------------------
class FakeCognito:
    def __init__(self, pools=(), clients=()):
        self.pools = [dict(p) for p in pools]
        self.clients = list(clients)
        self.calls: list[tuple] = []

    def get_paginator(self, name):
        fake = self

        class _P:
            def paginate(self, **kwargs):
                if name == "list_user_pools":
                    return iter([{"UserPools": [{"Id": p["Id"], "Name": p["Name"]} for p in fake.pools]}])
                return iter([{"UserPoolClients": [c for c in fake.clients if c["UserPoolId"] == kwargs["UserPoolId"]]}])
        return _P()

    def describe_user_pool(self, UserPoolId):
        return {"UserPool": next(p for p in self.pools if p["Id"] == UserPoolId)}

    def create_user_pool(self, **kwargs):
        self.calls.append(("create_user_pool", kwargs["PoolName"]))
        pool = {"Id": "ap-northeast-2_New", "Name": kwargs["PoolName"], "Arn": "arn:pool:new"}
        self.pools.append(pool)
        return {"UserPool": pool}

    def set_user_pool_mfa_config(self, **kwargs):
        self.calls.append(("set_user_pool_mfa_config", kwargs["MfaConfiguration"]))

    def create_user_pool_client(self, **kwargs):
        self.calls.append(("create_user_pool_client", kwargs["ClientName"]))
        client = {"ClientId": "client-new", "ClientName": kwargs["ClientName"], "UserPoolId": kwargs["UserPoolId"]}
        self.clients.append(client)
        return {"UserPoolClient": {**client, "ClientSecret": "s3cr3t"}}


def test_login_pool_client_and_secret_are_created_once():
    events: list = []
    cidp, sm = FakeCognito(), FakeSm(events)
    login = platform._ensure_login(cidp, sm)
    assert login == {"pool_id": "ap-northeast-2_New", "pool_arn": "arn:pool:new", "client_id": "client-new"}
    assert sm.values[platform.SECRET_COGNITO_CLIENT] == "s3cr3t"  # 비밀은 Secrets Manager로만 간다
    cidp.calls.clear()
    assert platform._ensure_login(cidp, sm) == login
    # 두 번째 실행은 풀 · 클라이언트를 만들지 않는다. MFA ON만 다시 맞춘다
    assert cidp.calls == [("set_user_pool_mfa_config", "ON")]


def test_recreated_client_replaces_the_stored_secret():
    events: list = []
    pool = {"Id": "p1", "Name": platform.USER_POOL, "Arn": "arn:pool"}
    sm = FakeSm(events, {platform.SECRET_COGNITO_CLIENT: "old"})
    platform._ensure_login(FakeCognito([pool]), sm)
    assert sm.values[platform.SECRET_COGNITO_CLIENT] == "s3cr3t"


# ⑩ 고객 온보딩 템플릿 배포 --------------------------------------------------------
class FakeS3:
    def __init__(self, exists=False):
        self.exists = exists
        self.calls: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.policy = None
        self.block = None

    def head_bucket(self, Bucket):
        if not self.exists:
            raise _client_error("404", "HeadBucket")

    def create_bucket(self, Bucket, CreateBucketConfiguration):
        self.calls.append(f"create_bucket:{Bucket}:{CreateBucketConfiguration['LocationConstraint']}")
        self.exists = True

    def put_bucket_tagging(self, Bucket, Tagging):
        self.calls.append("put_bucket_tagging")

    def put_public_access_block(self, Bucket, PublicAccessBlockConfiguration):
        self.block = PublicAccessBlockConfiguration

    def put_bucket_policy(self, Bucket, Policy):
        self.policy = json.loads(Policy)

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body


def test_onboarding_template_is_published_as_the_repository_file():
    s3 = FakeS3()
    url = platform._ensure_onboarding(s3, ACCOUNT, "ap-northeast-2")
    assert url == platform.onboarding_template_url(ACCOUNT, "ap-northeast-2")
    assert s3.calls[0] == f"create_bucket:{platform.onboarding_bucket(ACCOUNT)}:ap-northeast-2"
    assert s3.objects[platform.onboarding.TEMPLATE_KEY].decode("utf-8") == \
        platform.onboarding.TEMPLATE_PATH.read_text(encoding="utf-8")
    assert s3.policy == platform.onboarding_bucket_policy(ACCOUNT)
    # ACL로 여는 길은 막혀 있다. 버킷 정책(템플릿 객체 하나)만 공개를 허용한다
    assert s3.block["BlockPublicAcls"] and s3.block["IgnorePublicAcls"]


def test_onboarding_rerun_does_not_recreate_the_bucket():
    s3 = FakeS3(exists=True)
    platform._ensure_onboarding(s3, ACCOUNT, "ap-northeast-2")
    assert not [c for c in s3.calls if c.startswith("create_bucket")]


# ⑪ 첫 중앙 관리자 ----------------------------------------------------------------
def test_bootstrap_admin_runs_the_auth_cli_as_central_admin():
    command = platform.bootstrap_admin_command("ops@example.com")
    assert command[command.index("-m") + 1] == "auth.bootstrap_admin"
    assert {"--platform", "--central-admin", "--yes"} <= set(command)
    assert command[command.index("--email") + 1] == "ops@example.com"


def test_bootstrap_admin_needs_an_email(monkeypatch):
    _fake_main(monkeypatch, ["bootstrap-admin", "--account", ACCOUNT], {})
    with pytest.raises(SystemExit, match="--email"):
        platform.main()
