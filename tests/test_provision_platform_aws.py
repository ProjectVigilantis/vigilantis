# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# scripts/provision_platform_aws.py의 정의(정책·규칙·컨테이너·배포 설정) 회귀 테스트입니다. (ADR-0010)
#
# AWS를 부르지 않는다. 지키는 것은 "SaaS 모양"을 이루는 울타리 다섯이다.
#   ① api 태스크 역할은 접근 역할 전환 하나만 — AWS 자원을 직접 만지지 않는다
#   ② 접근 역할은 플랫폼 태스크 역할만, ExternalId가 맞을 때만 받는다
#   ③ ALB는 비밀 헤더가 있는 요청만 앱으로 보낸다(CloudFront 우회 차단)
#   ④ WAF는 기본 차단 · 허용 IP만 통과
#   ⑤ api는 겹쳐 뜨지 않는다(min 0 · max 100) · AI 분석은 명시해야 켜진다 · 비밀은 값이 아니라 참조로만
#
# scripts/ 는 CI pytest 경로에 없어 여기(루트 tests/)에 둔다.
# ==============================================================================

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

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


# ⑤ ---------------------------------------------------------------------------
def test_api_service_never_runs_two_tasks_during_deploy():
    api = platform.service_deployment(platform.API_SERVICE)
    assert (api["minimumHealthyPercent"], api["maximumPercent"]) == (0, 100)
    web = platform.service_deployment(platform.WEB_SERVICE)
    assert web["maximumPercent"] > 100  # web은 상태가 없어 기본 롤링


def _api(dispatch: bool = False) -> dict:
    return platform.api_container(
        "repo/api:t", region="ap-northeast-2", cors_origin="https://d.cloudfront.net",
        access_role_arn="arn:aws:iam::1:role/vigilantis-access", collect_vpc_ids="vpc-1",
        dispatch_enabled=dispatch,
        secrets_arns={"DATABASE_URL": "arn:s1", "OPENAI_API_KEY": "arn:s2", "VIGILANTIS_ACCESS_EXTERNAL_ID": "arn:s3"})


def test_ai_dispatch_is_off_unless_asked():
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
