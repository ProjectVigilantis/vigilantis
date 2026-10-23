# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# Vigilantis 플랫폼 배포 환경 구성·배포·정리 스크립트입니다. (ADR-0010)
#
# MVP 마감 판정(10/15(목)) 기준에 배포가 들어오면서, 플랫폼(api·web·DB)을 실 AWS에 올린다.
# 단일 계정이지만 **실제 SaaS와 같은 모양**으로 세운다 — 플랫폼과 관제 대상(스모크 환경)을
# VPC·역할로 가르고, 앱은 장기 키 없이 역할 전환(AssumeRole + ExternalId)으로만 관제 대상에
# 닿는다. Post-MVP에서 계정을 가르면(#438 ①②) 접근 역할의 계정 번호만 바뀐다.
#
# Post-MVP T-8(#494)부터 계정을 가른다. 플랫폼 자원은 플랫폼 멤버 계정(--account)에 두고, 접근 역할
# vigilantis-access만 고객 계정(--customer-account · 학원 계정)에 둔다. 스모크 정책·VPC도 고객 계정에서
# 찾는다. 고객 인자를 빼면 MVP처럼 한 계정에 전부 둔다 — 학원 계정의 옛 플랫폼을 정리할 때 쓴다.
#
# 구성 (서울 리전 — 관제 대상과 같은 리전, ADR-0010 §2):
#   브라우저 ─HTTPS─ CloudFront(기본 도메인 · --domain을 주면 그 도메인도) + WAF(IP당 속도 제한 → 허용 IP만)
#            └─HTTP + 비밀 헤더─ 공개 ALB(CloudFront IP만 수신)
#                 ├ /api/* · /health → ECS api (태스크 1개 고정)
#                 └ 그 밖           → ECS web
#   ECS(프라이빗 서브넷) → RDS PostgreSQL(프라이빗) · NAT → OpenAI·AWS API
#   api 태스크 역할(vigilantis-platform) ─AssumeRole + ExternalId─ vigilantis-access(고객 계정) → 스모크 VPC
#   오류 알람(CloudWatch → SNS 메일): api·web 정상 대상 10분 연속 0 · api 5xx · DB 남은 저장 공간
#
# Terraform이 아니라 Boto3인 이유: IaC는 Post-MVP다(ADR-0006 §2 · #438 ⑨). 방식은
# scripts/provision_smoke_aws.py와 같다 — 태그 식별·멱등·미리 보기·실 AWS 전용 가드.
#
# 실행 (repo 루트, **플랫폼 계정의 관리자 프로필**로 — 앱 키가 아니다. [고객]은 아래 고객 인자):
#   읽기만     : uv run python scripts/provision_platform_aws.py status --account <계정 ID> [고객]
#   세울 목록  : ... up --account <계정 ID> --allow-cidr <허용 IP>/32 [--domain <도메인>] [--alarm-email <주소>] [고객]
#   실제 생성  : ... up --account <계정 ID> --allow-cidr <허용 IP>/32 [--domain <도메인>] [--alarm-email <주소>] [고객] --yes
#                --domain: Route53 호스팅 영역과 us-east-1 ACM 인증서(ISSUED)가 먼저 있어야 한다.
#                CloudFront에 별칭·인증서를 달고 Route53에 A 별칭 레코드를 만든다
#                --alarm-email: 오류 알람을 받을 주소. SNS가 보낸 확인 메일의 링크를 눌러야 알람이 온다
#   이미지     : ... build --account <계정 ID> --tag <태그>        (docker 필요 — ECR에 api·web 푸시)
#   배포       : ... deploy --account <계정 ID> --tag <태그> [--enable-dispatch] [고객] --yes
#   전부 정리  : ... down --account <계정 ID> [고객] --yes
#   고객 인자  : --customer-account <고객 계정 ID> --customer-profile <고객 계정의 관리자 프로필>
#   알림(#493) : 기본 배포는 NOTIFICATION_ENABLED=false다. 켜려면 비밀 아닌 입력 JSON을 명시한다:
#                {"enabled": true, "public_base_url": "https://…", "routes": [{tenant_id, aws_account_id,
#                 slack_webhook_env?, sns_topic_arn?}], "slack_secret_arns": {"<env>": "<기존 비밀 ARN>"},
#                 "settings": {"NOTIFICATION_READ_TIMEOUT_SECONDS": 5}}
#                ... deploy --account <ID> --tag <태그> --notification-config <파일> [--yes]
#                ... notification-iam --account <ID> --notification-config <파일> [--yes]   (IAM만 먼저)
#                ... notification-revoke --account <ID> [--yes]   (알림 전용 인라인 정책 둘만 회수)
#                비밀·토픽·구독·KMS·교차 계정 정책은 만들지 않는다 — 비밀은 이미 있어야 하고 값은 PM이 넣는다.
#                태스크 역할은 지정 토픽 sns:Publish만, 실행 역할은 지정 비밀 GetSecretValue만 받는다.
#                deploy는 IAM을 쓰지 않고 정책이 계획과 같은지 읽기만 한다. 정책은 notification-iam/revoke만 쓰며
#                api가 알림 꺼짐·안정·이전 태스크 종료 상태일 때만 쓴다. 처음 켜기: notification-iam → (실효 확인) →
#                deploy --notification-config. 바꾸기/끄기: 설정 없이 deploy → 태스크 종료 확인 → iam/revoke.
#
# 안전 가드 (스모크 스크립트와 같다):
#   ① AWS_ENDPOINT_URL이 잡혀 있으면 멈춘다 — 전부 실 AWS 과금 자원이다.
#   ② --account가 호출 주체의 계정과 다르면 멈춘다. --customer-account도 --customer-profile의 계정과
#      대조한다. 계정 ID와 허용 IP는 저장소에 적지 않는다.
#   ③ up·deploy·down은 --yes 없이는 아무것도 바꾸지 않는다.
#   ④ down이 지우는 것은 플랫폼 태그 자원과 **플랫폼 VPC 안의 것**뿐이다. 스모크 환경과
#      그 앱 정책(vigilantis-smoke-app)은 건드리지 않는다 — 접근 역할은 그 정책을 빌려 쓸 뿐이다.
#      반대로 스모크 down은 접근 역할이 그 정책을 붙잡고 있으면 멈춘다 — 정리 순서는
#      **플랫폼 down → 스모크 down**이다(ADR-0010 §7).
#   ⑤ 이름이 같아도 플랫폼 태그(vigilantis:platform=true)가 없는 IAM 역할은 덮어쓰거나 지우지 않는다.
#   ⑥ --allow-cidr는 IPv4 CIDR이고 /24보다 넓으면 거부한다 — 로그인 없는 MVP에서 허용 IP가 출입문이다.
#   ⑦ WAF 속도 제한(#484 · Post-MVP T-3) — IP당 RATE_LIMIT_PER_WINDOW 건 / RATE_WINDOW_SECONDS 초를
#      허용 규칙보다 **앞**(우선순위 0)에서 센다. 허용이 먼저 맞으면 WAF가 거기서 평가를 끝내기 때문이다.
#      NAT 공인 IP는 세지 않는다 — 웹의 서버 측 API 호출이 전부 그 IP 하나로 CloudFront를 지나므로
#      세면 모든 사용자의 화면 요청이 한 IP에 쌓여 플랫폼이 스스로를 막는다(ADR-0010 §4).
#      동작은 Count(세기만)로 시작한다 — 정상 사용량을 재지 않았고, 캐시가 꺼져 있어 정적 파일까지
#      전부 센다. 관측(CloudWatch 지표 vigilantis-platform-rate) 뒤 RATE_ACTION을 Block으로 바꾼다.
#      up을 다시 돌리면 기존 웹 ACL의 규칙도 이 정의로 맞춘다 — 규칙을 고친 뒤 재실행이 곧 적용이다.
#   ⑧ 고객 계정의 접근 역할은 고객 태그(vigilantis:customer-access=<플랫폼 계정 ID>)로 식별한다. 다른 플랫폼
#      계정의 태그가 달려 있거나 어느 태그도 없으면 덮어쓰지 않는다. 플랫폼 태그만 달린 역할(같은 계정의 옛
#      플랫폼이 만든 것)은 넘겨받는다 — 고객 태그를 달고 플랫폼 태그를 떼어 옛 플랫폼의 down이 지우지 않게
#      한다. 넘겨받는 순간 신뢰 정책이 바뀌어 옛 플랫폼은 그 역할로 들어가지 못한다. 플랫폼 down은 고객
#      계정의 역할을 지우지 않는다 — 고객 계정의 자원이다.
#
# 비밀값은 출력하지 않는다. OpenAI 키 비밀은 자리만 만들고 값은 PM이 직접 넣는다:
#   aws secretsmanager put-secret-value --secret-id vigilantis-platform/openai-api-key --secret-string <키>
# ==============================================================================

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Callable, Optional
from urllib.parse import urlsplit

from pydantic import TypeAdapter

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_REPO_ROOT / "apps" / "core-api"), str(_REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

import config as app_config  # noqa: E402
from notifications.runtime import validate_notification_timing  # noqa: E402
from notifications.routing import (  # noqa: E402
    MAX_CONFIG_BYTES, RouteConfigError, RouteRegistry, _no_duplicate_keys, parse_sns_topic_arn, validate_public_base_url,
)
from services.aws.client import aws_client, default_region, endpoint_url  # noqa: E402

PREFIX = "vigilantis-platform"
TAG_KEY = "vigilantis:platform"
TAG_VALUE = "true"

VPC_NAME = f"{PREFIX}-vpc"
VPC_CIDR = "10.50.0.0/16"  # 기본 VPC(172.31/16)·스모크 VPC(10.42/16)와 겹치지 않게
PUBLIC_SUBNETS = {"a": "10.50.0.0/24", "c": "10.50.1.0/24"}    # ALB·NAT
PRIVATE_SUBNETS = {"a": "10.50.10.0/24", "c": "10.50.11.0/24"}  # ECS·RDS
NAT_AZ = "a"  # NAT 1개 — 비용 우선(AZ 장애 시 c의 아웃바운드도 끊기는 대가, ADR-0010 §5)

SG_ALB = f"{PREFIX}-alb"
SG_API = f"{PREFIX}-api"
SG_WEB = f"{PREFIX}-web"
SG_DB = f"{PREFIX}-db"
SECURITY_GROUPS = (SG_ALB, SG_API, SG_WEB, SG_DB)
# ALB는 CloudFront에서만 받는다 — AWS 관리형 prefix list. 우회해 ALB 주소로 직접 들어오는 길은
# 이 목록과 아래 비밀 헤더 두 겹으로 막는다
CLOUDFRONT_PREFIX_LIST = "com.amazonaws.global.cloudfront.origin-facing"
API_PORT = 8000
WEB_PORT = 3000

DB_INSTANCE = f"{PREFIX}-db"
DB_SUBNET_GROUP = f"{PREFIX}-db"
DB_CLASS = "db.t4g.micro"
# compose의 postgres:16과 맞춘다. 메이저만 넘기면 RDS가 그 메이저의 기본 마이너를 고른다 —
# 버전 목록을 조회해 고르지 않는다(목록이 페이지로 나뉘면 16.x를 놓친다)
DB_MAJOR = "16"
DB_STORAGE_GB = 20
# 자동 백업 보존 일수(T-8 운영 장치). MVP의 1일은 주말을 넘긴 사고를 되돌리지 못한다
DB_BACKUP_DAYS = 7
DB_NAME = "vigilantis"
DB_USER = "vigilantis"
# RDS 생성 요청 전에 접속 문자열 비밀을 먼저 저장할 때의 호스트 자리 — 대기 뒤 실제 엔드포인트로 채운다.
# .invalid는 절대 풀리지 않는 예약 도메인이라 채워지기 전 배포가 엉뚱한 곳에 붙지 않는다
DB_HOST_PENDING = "pending.invalid"

ECR_REPOS = {"api": f"{PREFIX}-api", "web": f"{PREFIX}-web"}

SECRET_DATABASE_URL = f"{PREFIX}/database-url"
SECRET_OPENAI = f"{PREFIX}/openai-api-key"
SECRET_ORIGIN = f"{PREFIX}/origin-verify"        # CloudFront → ALB 비밀 헤더 값
SECRET_EXTERNAL_ID = f"{PREFIX}/access-external-id"
OPENAI_PLACEHOLDER = "SET_ME"  # 값은 PM이 직접 넣는다 — 스크립트는 키를 다루지 않는다

ROLE_EXEC = f"{PREFIX}-exec"   # ECS가 이미지를 받고 비밀을 주입하는 역할
ROLE_TASK = PREFIX              # api 컨테이너의 역할 — 접근 역할 전환 하나만 허용
ROLE_ACCESS = "vigilantis-access"
# 고객 계정에 둔 접근 역할의 식별 태그(안전 가드 ⑧) — 값은 그 역할을 쓰는 플랫폼 계정 ID다
CUSTOMER_TAG_KEY = "vigilantis:customer-access"
# 접근 역할의 권한은 스모크 스크립트가 만든 Action Whitelist 거울 정책을 그대로 빌린다(ADR-0009 §2).
# 정책을 두 벌 두면 tests/test_provision_smoke_aws.py의 두 방향 보증이 한쪽에만 걸린다
SMOKE_POLICY_NAME = "vigilantis-smoke-app"
SMOKE_VPC_NAME = "vigilantis-smoke-vpc"
SESSION_SECONDS = 900  # 원격 회수(#438 ②)의 노출 상한과 같은 값
# WAF 허용 CIDR의 최소 접두 길이 — 이보다 넓은 범위(/0–/23)는 받지 않는다. WAF는 /0만 거절한다
MIN_ALLOW_PREFIX = 24

CLUSTER = PREFIX
LOG_GROUPS = {"api": f"/{PREFIX}/api", "web": f"/{PREFIX}/web"}
LOG_RETENTION_DAYS = 30

ALB_NAME = f"{PREFIX}-alb"
TG_API = f"{PREFIX}-api"
TG_WEB = f"{PREFIX}-web"
ORIGIN_HEADER = "X-Vigilantis-Origin"
API_PATHS = ("/api/*", "/health")
WEB_HEALTH_PATH = "/favicon.ico"  # 페이지는 서버에서 API를 부르므로 정적 파일로 생존만 본다
DEREGISTRATION_DELAY_SECONDS = 30  # TG 연결 비우기 대기 — target_group_attributes

WAF_IPSET = f"{PREFIX}-allow"
WAF_NAT_IPSET = f"{PREFIX}-nat"  # 속도 제한에서 빼는 NAT 공인 IP(안전 가드 ⑦)
WAF_ACL = f"{PREFIX}-waf"
# 속도 제한(안전 가드 ⑦) — 잠정값. Count 기간의 관측으로 다시 정한다(#484)
RATE_LIMIT_PER_WINDOW = 1000
RATE_WINDOW_SECONDS = 300
RATE_ACTION = "Count"  # 관측 뒤 "Block"
CF_COMMENT = PREFIX  # CloudFront 배포는 태그로 찾기 어렵다 — comment로 식별한다
CF_HOSTED_ZONE_ID = "Z2FDTNDATAQYW2"  # CloudFront 별칭 레코드의 고정 호스팅 영역 ID(AWS 문서 · 전 계정 공통)
# AWS 관리형 정책 ID(전 계정 공통)
CF_CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CF_ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"

API_SERVICE = f"{PREFIX}-api"
WEB_SERVICE = f"{PREFIX}-web"
API_TASK_FAMILY = f"{PREFIX}-api"
WEB_TASK_FAMILY = f"{PREFIX}-web"

# 플랫폼 오류 알람(T-8 운영 장치) — 이름은 alarm_definitions()가 정한다
ALARM_TOPIC = f"{PREFIX}-alarms"
ALARM_NAMES = (f"{PREFIX}-api-down", f"{PREFIX}-web-down", f"{PREFIX}-api-5xx", f"{PREFIX}-db-storage")
# api는 겹쳐 뜨지 않게 내린 뒤 띄우므로(service_deployment) 배포마다 정상 대상이 0인 시간이 생긴다.
# 그 시간보다 길게 잡아 배포로는 울리지 않게 한다
DOWN_MINUTES = 10
API_5XX_PER_5MIN = 10
DB_FREE_BYTES_MIN = 2 * 1024 ** 3


# ------------------------------------------------------------------ 순수 정의 (테스트 대상)
def task_role_policy(customer_account: str) -> dict:
    """api 태스크 역할 — **접근 역할 전환 하나뿐**이다. AWS 자원을 직접 만지는 권한은 없다.

    플랫폼이 관제 대상에 닿는 길을 이 한 줄로 좁히는 것이 SaaS 모양의 핵심이다(ADR-0010 §3).
    원격 회수(#438 ②)는 이 문장에서 고객 역할 ARN을 빼는 것으로 한다. 계정을 가르지 않았으면
    customer_account는 플랫폼 계정 자신이다.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AssumeAccessRoleOnly",
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": f"arn:aws:iam::{customer_account}:role/{ROLE_ACCESS}",
        }],
    }


def access_trust_policy(platform_account: str, external_id: str) -> dict:
    """접근 역할의 신뢰 정책 — 플랫폼 계정의 태스크 역할만, ExternalId가 맞을 때만."""
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{platform_account}:role/{ROLE_TASK}"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"sts:ExternalId": external_id}},
        }],
    }


def ecs_tasks_trust_policy() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }


def exec_secrets_policy(secret_arns: list[str]) -> dict:
    """실행 역할이 주입할 비밀만 읽는다."""
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": sorted(secret_arns),
        }],
    }


def listener_rules(tg_api: str, tg_web: str, origin_value: str) -> list[dict]:
    """ALB 규칙 — **비밀 헤더가 있어야만** 앱으로 간다. 없으면 기본 동작(403)."""
    header = {"Field": "http-header", "HttpHeaderConfig": {
        "HttpHeaderName": ORIGIN_HEADER, "Values": [origin_value]}}
    return [
        {"Priority": 10, "Conditions": [header, {"Field": "path-pattern", "Values": list(API_PATHS)}],
         "Actions": [{"Type": "forward", "TargetGroupArn": tg_api}]},
        {"Priority": 20, "Conditions": [header],
         "Actions": [{"Type": "forward", "TargetGroupArn": tg_web}]},
    ]


def waf_rules(ipset_arn: str, nat_ipset_arn: str) -> list[dict]:
    """IP당 속도 제한(NAT 제외) → 허용 IP만 통과 → 기본 차단(안전 가드 ⑦).

    로그인이 없는 MVP라 허용 IP가 출입문이다(#438 ④ 전까지). 속도 규칙은 허용 규칙보다 앞이어야
    평가된다 — 허용이 맞으면 WAF가 거기서 끝낸다.
    """
    return [{
        "Name": "rate-per-ip",
        "Priority": 0,
        "Statement": {"RateBasedStatement": {
            "Limit": RATE_LIMIT_PER_WINDOW,
            "EvaluationWindowSec": RATE_WINDOW_SECONDS,
            "AggregateKeyType": "IP",
            "ScopeDownStatement": {"NotStatement": {"Statement": {
                "IPSetReferenceStatement": {"ARN": nat_ipset_arn}}}},
        }},
        "Action": {RATE_ACTION: {}},
        "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True,
                             "MetricName": f"{PREFIX}-rate"},
    }, {
        "Name": "allow-listed-ips",
        "Priority": 1,
        "Statement": {"IPSetReferenceStatement": {"ARN": ipset_arn}},
        "Action": {"Allow": {}},
        "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True,
                             "MetricName": f"{PREFIX}-allow"},
    }]


# ---- 외부 알림 배포(#493 PR3) — 기본은 꺼짐. --notification-config로 명시해야 켜진다
# 알림 전용 인라인 정책 이름 — 기본 정책(assume-access-role · read-platform-secrets)과 겹치지 않는다
NOTIFY_POLICY_TASK = "publish-notification-topics"   # ROLE_TASK: 지정 SNS 토픽 Publish만
NOTIFY_POLICY_EXEC = "read-notification-secrets"     # ROLE_EXEC: 지정 Slack 비밀 GetSecretValue만(앱은 읽지 않는다)
NOTIFY_POLICIES = ((ROLE_TASK, NOTIFY_POLICY_TASK), (ROLE_EXEC, NOTIFY_POLICY_EXEC))
BASE_SECRET_ENV = ("DATABASE_URL", "OPENAI_API_KEY", "VIGILANTIS_ACCESS_EXTERNAL_ID")
# config.Settings에서 그대로 전달할 수 있는 알림 조정값(기본값은 config.py가 소유한다)
NOTIFY_SETTING_KEYS = (
    "NOTIFICATION_POLL_SECONDS", "NOTIFICATION_BATCH_SIZE", "NOTIFICATION_MAX_ATTEMPTS",
    "NOTIFICATION_BACKOFF_SECONDS", "NOTIFICATION_MAX_BACKOFF_SECONDS",
    "NOTIFICATION_CONNECT_TIMEOUT_SECONDS", "NOTIFICATION_READ_TIMEOUT_SECONDS", "NOTIFICATION_LEASE_SECONDS",
)
_NOTIFY_CONFIG_KEYS = {"enabled", "public_base_url", "routes", "slack_secret_arns", "settings"}
_RESERVED_ENV = {"HOME", "PATH", "PORT", "NOTIFICATION_ROUTES_JSON", "NOTIFICATION_ROUTES_FILE"}
_MAX_NOTIFY_CONFIG_BYTES = 80 * 1024


def _reserved_env_names() -> set[str]:
    """대소문자 무시 비교용(pydantic Settings는 env 이름을 대소문자 구분하지 않는다) — 전부 대문자로 돌려준다."""
    names = set(_RESERVED_ENV) | set(BASE_SECRET_ENV)
    for cls in (app_config.Settings, app_config.AwsSettings, app_config.AuthSettings, app_config.CollectorSettings):
        names |= set(cls.model_fields)
    return {n.upper() for n in names}


def notification_task_policy(topic_arns: list[str]) -> dict:
    """api 태스크 역할 — 정확히 지정한 SNS 토픽 ARN에 Publish만. 와일드카드·고객 계정 권한 없음."""
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "PublishNotificationTopicsOnly", "Effect": "Allow", "Action": "sns:Publish",
        "Resource": sorted(topic_arns)}]}


def notification_exec_policy(secret_arns: list[str]) -> dict:
    """실행 역할 — 정확히 지정한 Slack 비밀 ARN의 GetSecretValue만. 앱 태스크 역할에는 주지 않는다."""
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "InjectNotificationSecretsOnly", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
        "Resource": sorted(secret_arns)}]}


def load_notification_config(path: str, account: str, region: str) -> Optional[dict]:
    """비밀 아닌 알림 배포 입력을 검증한다. AWS를 부르지 않는다. 꺼진 입력이면 None.

    형식: {"enabled": true, "public_base_url": "https://…", "routes": [<라우트 스키마 그대로>],
           "slack_secret_arns": {"<slack_webhook_env>": "<Secrets Manager 비밀 ARN>"},
           "settings": {"NOTIFICATION_READ_TIMEOUT_SECONDS": 5, …}}
    enabled가 true일 때만 켠다(routes가 있다는 이유로 켜지 않는다). 실패 문구는 고정이며 입력 내용을 싣지 않는다.
    """
    def bad(why: str):
        return SystemExit(f"--notification-config 거부: {why}")

    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_NOTIFY_CONFIG_BYTES + 1)
        if len(raw) > _MAX_NOTIFY_CONFIG_BYTES:
            raise bad("파일이 너무 크다")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except SystemExit:
        raise
    except (OSError, ValueError, RecursionError):
        raise bad("읽을 수 없거나 JSON이 아니다") from None
    if not isinstance(data, dict) or not set(data) <= _NOTIFY_CONFIG_KEYS:
        raise bad("알 수 없는 최상위 키")
    if not isinstance(data.get("enabled"), bool):
        raise bad("enabled는 true/false를 명시해야 한다")
    if not data["enabled"]:
        return None
    if not isinstance(data.get("public_base_url"), str) or "routes" not in data:
        raise bad("public_base_url과 routes가 필요하다")
    try:
        base_url = validate_public_base_url(data["public_base_url"])
    except RouteConfigError:
        raise bad("public_base_url이 origin 형식이 아니다") from None
    routes = data["routes"]
    if not isinstance(routes, list):
        raise bad("routes 형식 오류")
    envs = sorted({r["slack_webhook_env"] for r in routes if isinstance(r, dict)
                   and isinstance(r.get("slack_webhook_env"), str)})
    # 합성 webhook으로 런타임과 같은 스키마·충돌 판정을 돌린다 — 막힌 항목이 있으면 배포하지 않는다
    probe = {name: f"https://hooks.slack.com/services/T0/B0/X{i}" for i, name in enumerate(envs)}
    try:
        registry = RouteRegistry.from_mapping({"routes": routes}, probe)
    except RouteConfigError:
        raise bad("routes가 라우트 스키마에 맞지 않는다") from None
    if registry.blocked:
        raise bad("routes에 소유권·목적지 충돌이 있다")
    topics = sorted({r["sns_topic_arn"] for r in routes if r.get("sns_topic_arn") is not None})
    for arn in topics:
        if parse_sns_topic_arn(arn)[1] != account:
            raise bad("SNS 토픽은 플랫폼 계정 소유여야 한다(교차 계정 정책은 다루지 않는다)")
    refs = data.get("slack_secret_arns", {})
    if not isinstance(refs, dict) or set(refs) != set(envs):
        raise bad("slack_secret_arns 키는 routes의 slack_webhook_env와 정확히 같아야 한다")
    reserved = _reserved_env_names()
    secret_re = re.compile(rf"arn:aws:secretsmanager:{re.escape(region)}:{account}:secret:[A-Za-z0-9/_+=.@-]{{1,512}}")
    for name, arn in refs.items():
        if name.upper() in reserved or name.upper().startswith(("AWS_", "VIGILANTIS_")):
            raise bad(f"환경변수 이름 {name}이 기본 설정과 충돌한다")
        if not isinstance(arn, str) or secret_re.fullmatch(arn) is None or "*" in arn:
            raise bad(f"{name}의 비밀 ARN 형식 오류(이 계정·리전의 정확한 ARN만)")
    if len(set(refs.values())) != len(refs):
        raise bad("서로 다른 환경변수가 같은 비밀 ARN을 가리킨다")
    raw_settings = data.get("settings", {})
    if not isinstance(raw_settings, dict) or not set(raw_settings) <= set(NOTIFY_SETTING_KEYS):
        raise bad("settings에 허용되지 않은 키")
    # 호스트 환경·.env와 무관하게 config.Settings의 기본값 + 제공값만으로 런타임과 같은 규칙을 적용한다
    effective = {}
    for key in NOTIFY_SETTING_KEYS:
        field = app_config.Settings.model_fields[key]
        value = raw_settings.get(key, field.default)
        try:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError
            effective[key] = TypeAdapter(Annotated[field.annotation, *field.metadata]).validate_python(value)
        except ValueError:  # pydantic ValidationError는 ValueError다
            raise bad(f"settings.{key} 값이 config 제약에 맞지 않는다") from None
    try:
        validate_notification_timing(SimpleNamespace(**effective))
    except ValueError:
        raise bad("settings의 backoff·lease 조합이 런타임 규칙에 맞지 않는다") from None
    routes_json = json.dumps({"routes": routes}, ensure_ascii=False, separators=(",", ":"))
    if len(routes_json.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise bad("routes JSON이 런타임 상한을 넘는다")
    return {
        "public_base_url": base_url,
        "routes_json": routes_json,
        "secret_arns": dict(sorted(refs.items())),
        "topic_arns": topics,
        "settings": {k: str(v) for k, v in sorted(raw_settings.items())},
    }


def notification_plan(c: dict, account: str, region: str, path: str) -> Optional[dict]:
    """설정 검증 + 읽기 전용 점검(비밀 존재 · 역할 태그). 무엇이든 바꾸기 전에 부른다."""
    plan = load_notification_config(path, account, region)
    if plan is None:
        return None
    for name, arn in plan["secret_arns"].items():
        try:
            found = c["sm"].describe_secret(SecretId=arn)["ARN"]
        except ClientError as exc:
            if _code(exc) == "ResourceNotFoundException":
                sys.exit(f"--notification-config 거부: {name}의 비밀이 없다 — 이 스크립트는 비밀을 만들지 않는다")
            raise
        if found != arn:
            sys.exit(f"--notification-config 거부: {name}의 비밀 ARN이 실제와 다르다")
    _require_notification_roles(c["iam"])
    return plan


def _require_notification_roles(iam) -> None:
    for role, _ in NOTIFY_POLICIES:
        found = _get_role(iam, role)
        if not found or not is_platform_tagged(found.get("Tags", [])):
            sys.exit(f"IAM 역할 {role}이 없거나 플랫폼 태그({TAG_KEY}={TAG_VALUE})가 없다 — 알림 정책을 붙이지 않는다")


_ENABLED_TRUE = {"1", "true", "t", "yes", "y", "on"}
_ENABLED_FALSE = {"0", "false", "f", "no", "n", "off"}


def _enabled_flag(env: list[dict]) -> bool:
    """컨테이너 environment의 NOTIFICATION_ENABLED. 없으면 꺼진 레거시, 알 수 없는 값은 켜진 것으로 본다(닫힘)."""
    values = [str(e.get("value", "")).strip().lower() for e in env or []
              if str(e.get("name", "")).strip().upper() == "NOTIFICATION_ENABLED"]  # Settings는 이름의 대소문자를 구분하지 않는다
    return any(v not in _ENABLED_FALSE for v in values)


def _live_api_tasks(ecs) -> list[dict]:
    """api 패밀리의 아직 STOPPED가 아닌 태스크 전부(STOPPING 포함). 페이지·100건 제한을 넘겨 본다."""
    listed: set[str] = set()
    for status in ("RUNNING", "STOPPED"):  # desiredStatus=STOPPED에 아직 내려가는 중인 태스크가 있다
        for page in ecs.get_paginator("list_tasks").paginate(cluster=CLUSTER, family=API_TASK_FAMILY,
                                                              desiredStatus=status):
            listed.update(page.get("taskArns", []))
    arns = sorted(listed)
    tasks: list[dict] = []
    for i in range(0, len(arns), 100):
        got = ecs.describe_tasks(cluster=CLUSTER, tasks=arns[i:i + 100])
        if got.get("failures"):
            sys.exit("api 태스크 조회에 실패 항목이 있다 — 상태를 확정할 수 없어 정책은 바꾸지 않았다")
        tasks += got["tasks"]
    if {t.get("taskArn") for t in tasks} != listed:
        sys.exit("나열된 api 태스크를 전부 확인하지 못했다 — 상태를 확정할 수 없어 정책은 바꾸지 않았다")
    return [t for t in tasks if t.get("lastStatus") != "STOPPED"]


def require_notifications_quiescent(ecs) -> None:
    """정책을 쓰기 전에 api가 알림을 쓰지 않는 안정 상태인지 본다. 아니면 아무것도 바꾸지 않고 멈춘다.

    서비스가 없으면 살아 있는 api 태스크가 없을 때만 통과한다. 서비스가 있으면 PRIMARY 배포가 COMPLETED이고
    다른 배포가 없어야 한다. 살아 있는 태스크(내려가는 중 포함)의 태스크 정의와 오버라이드에서 알림이
    켜졌거나 알 수 없으면 거부한다.
    """
    why = None
    if _find_cluster(ecs):
        svc = _find_service(ecs, API_SERVICE)
        if svc:
            primary = next((d for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"), None)
            if primary is None or deployment_problems([svc], {API_SERVICE: primary.get("taskDefinition")}) \
                    or len(svc.get("deployments", [])) != 1:
                why = "api 서비스가 안정 상태가 아니다(진행 중·실패·롤백 중 배포)"
        if why is None:
            defs: dict[str, bool] = {}

            def td_enabled(td: str) -> bool:
                if td not in defs:
                    cdefs = ecs.describe_task_definition(taskDefinition=td)["taskDefinition"]["containerDefinitions"]
                    defs[td] = any(_enabled_flag(cd.get("environment")) for cd in cdefs if cd.get("name") == "api")
                return defs[td]

            if svc and td_enabled(primary["taskDefinition"]):  # 태스크가 잠시 없어도 서비스가 켜진 채 다시 띄울 수 있다
                why = "api 서비스의 PRIMARY 태스크 정의에서 알림이 켜져 있다"
            for task in _live_api_tasks(ecs) if why is None else ():
                td = task.get("taskDefinitionArn", "")
                td_enabled(td)
                overrides = [o for o in (task.get("overrides") or {}).get("containerOverrides", [])
                             if o.get("name") == "api"]
                if defs[td] or any(_enabled_flag(o.get("environment")) for o in overrides):
                    why = "알림이 켜진 api 태스크가 아직 살아 있다"
                    break
    if why:
        sys.exit(f"{why} — 알림 없이 deploy해 끄고 이전 태스크가 내려간 뒤에 정책을 바꿀 것(정책은 바꾸지 않았다)")


def _policy_shape(doc: object) -> Optional[tuple]:
    """우리 정책 문서의 정규형(Allow 한 문장 · Action·Resource 집합). 모양이 다르면 None."""
    if not isinstance(doc, dict) or set(doc) != {"Version", "Statement"} or doc["Version"] != "2012-10-17":
        return None
    stmts = doc["Statement"]
    stmts = [stmts] if isinstance(stmts, dict) else stmts
    if not isinstance(stmts, list) or len(stmts) != 1 or not isinstance(stmts[0], dict):
        return None
    s = stmts[0]
    if set(s) - {"Sid"} != {"Effect", "Action", "Resource"} or s["Effect"] != "Allow":
        return None
    sets = []
    for key in ("Action", "Resource"):
        v = [s[key]] if isinstance(s[key], str) else s[key]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            return None
        sets.append(frozenset(v))
    return tuple(sets)


def _notification_wanted(plan: dict) -> dict:
    return {
        NOTIFY_POLICY_TASK: (ROLE_TASK, plan["topic_arns"], notification_task_policy),
        NOTIFY_POLICY_EXEC: (ROLE_EXEC, sorted(plan["secret_arns"].values()), notification_exec_policy),
    }


def verify_notification_policies(iam, plan: dict) -> None:
    """읽기 전용: 전용 정책 둘이 계획과 정확히 같은지(없어야 하는 쪽은 없는지) 본다. 실효(SCP·KMS·전파)는 증명하지 않는다."""
    _require_notification_roles(iam)
    for policy_name, (role, resources, build_policy) in _notification_wanted(plan).items():
        try:
            actual = iam.get_role_policy(RoleName=role, PolicyName=policy_name)["PolicyDocument"]
        except ClientError as exc:
            if _code(exc) != "NoSuchEntity":
                raise
            actual = None
        want = _policy_shape(build_policy(resources)) if resources else None
        if (None if actual is None else _policy_shape(actual)) != want or (actual is not None and want is None):
            sys.exit(f"IAM {role}/{policy_name}이 계획과 다르다 — deploy는 IAM을 쓰지 않는다. "
                     "알림이 꺼진 상태에서 notification-iam을 먼저 실행할 것")


def apply_notification_policies(iam, ecs, plan: dict) -> None:
    """알림 전용 인라인 정책 둘만 맞춘다(없어야 하는 쪽은 지운다). 알림이 꺼진 안정 상태에서만 쓴다."""
    _require_notification_roles(iam)
    require_notifications_quiescent(ecs)
    for policy_name, (role, resources, build_policy) in _notification_wanted(plan).items():
        if resources:
            iam.put_role_policy(RoleName=role, PolicyName=policy_name, PolicyDocument=json.dumps(build_policy(resources)))
            print(f"[platform] IAM {role}/{policy_name} 적용 — {len(resources)}건")
        else:
            _try(lambda r=role, p=policy_name: iam.delete_role_policy(RoleName=r, PolicyName=p), "NoSuchEntity")


def revoke_notification_policies(iam, ecs) -> None:
    """알림 전용 인라인 정책 둘만 지운다. 알림이 꺼진 안정 상태에서만, 태그 없는 역할·없는 역할은 건드리지 않는다."""
    require_notifications_quiescent(ecs)
    for role, policy_name in NOTIFY_POLICIES:
        found = _get_role(iam, role)
        if not found or not is_platform_tagged(found.get("Tags", [])):
            print(f"[platform] IAM 역할 {role}이 없거나 플랫폼 태그가 없어 건드리지 않았다")
            continue
        _try(lambda r=role, p=policy_name: iam.delete_role_policy(RoleName=r, PolicyName=p), "NoSuchEntity")
        print(f"[platform] IAM {role}/{policy_name} 회수")


def api_container(image: str, *, region: str, cors_origin: str, access_role_arn: str,
                  collect_vpc_ids: str, dispatch_enabled: bool, secrets_arns: dict[str, str],
                  notification: Optional[dict] = None) -> dict:
    env = {
        "AWS_REGION": region,
        "CORS_ALLOW_ORIGINS": cors_origin,
        "COLLECT_VPC_IDS": collect_vpc_ids,
        "VIGILANTIS_ACCESS_ROLE_ARN": access_role_arn,
        "VIGILANTIS_ACCESS_SESSION_SECONDS": str(SESSION_SECONDS),
        # AI 분석과 조치 실행 디스패치를 **한 스위치로** 켜고 끈다(config.py — 둘이 이 값을 공유한다).
        # 끄면 승인한 조치도 실행되지 않고 접수 상태로 멈춘다. 켜면 인시던트마다 OpenAI가 과금된다.
        # 기본은 끈다(배포 시 --enable-dispatch)
        "DISPATCH_ENABLED": "true" if dispatch_enabled else "false",
        "SCAN_ENABLED": "true",
        # 파일 로그는 끈다 — 컨테이너 파일은 태스크와 함께 사라진다. stdout이 CloudWatch Logs로 간다
        "LOG_FILE_ENABLED": "false",
        "NOTIFICATION_ENABLED": "true" if notification else "false",
    }
    secrets_arns = dict(secrets_arns)
    if notification:
        # routes JSON은 비밀이 아니다 — serve.py가 파일로 쓴다. webhook은 비밀 참조로만 주입한다
        env.update(notification["settings"])
        env["NOTIFICATION_PUBLIC_BASE_URL"] = notification["public_base_url"]
        env["NOTIFICATION_ROUTES_JSON"] = notification["routes_json"]
        taken = {n.upper() for n in (*env, *secrets_arns)} | _reserved_env_names()
        if any(n.upper() in taken for n in notification["secret_arns"]):
            raise ValueError("notification secret env name collides with base settings")
        secrets_arns.update(notification["secret_arns"])
    return {
        "name": "api",
        "image": image,
        "essential": True,
        "portMappings": [{"containerPort": API_PORT, "protocol": "tcp"}],
        "environment": [{"name": k, "value": v} for k, v in sorted(env.items())],
        "secrets": [{"name": k, "valueFrom": v} for k, v in sorted(secrets_arns.items())],
        "logConfiguration": {"logDriver": "awslogs", "options": {
            "awslogs-group": LOG_GROUPS["api"], "awslogs-region": region, "awslogs-stream-prefix": "api"}},
    }


def web_container(image: str, *, region: str) -> dict:
    return {
        "name": "web",
        "image": image,
        "essential": True,
        "portMappings": [{"containerPort": WEB_PORT, "protocol": "tcp"}],
        "environment": [{"name": "PORT", "value": str(WEB_PORT)}, {"name": "HOSTNAME", "value": "0.0.0.0"}],
        "logConfiguration": {"logDriver": "awslogs", "options": {
            "awslogs-group": LOG_GROUPS["web"], "awslogs-region": region, "awslogs-stream-prefix": "web"}},
    }


def target_group_attributes() -> list[dict]:
    """연결 비우기 대기(deregistration delay)를 기본 300초에서 줄인다.

    api는 겹쳐 뜨지 않게 이전 태스크를 내린 뒤 새 태스크를 띄우므로(service_deployment) 이 대기가
    그대로 api 중단 시간에 더해진다 — 기본값이면 재배포마다 6–8분 끊겼다(10/13 실측). 요청은 짧은
    REST라 30초면 비워지고, WS는 끊겨도 화면이 재연결한다.
    """
    return [{"Key": "deregistration_delay.timeout_seconds", "Value": str(DEREGISTRATION_DELAY_SECONDS)}]


def service_deployment(service: str) -> dict:
    """api는 **겹쳐 뜨지 않게** 먼저 내리고 띄운다(min 0 · max 100).

    스케줄러·실행 디스패처·AI 디스패처가 프로세스 메모리에서 돌고 디스패처에는 프로세스 간
    잠금이 없다(#435 4-2). 기본 롤링(새 태스크를 먼저 띄움)이면 그 사이 같은 실행이 두 번
    디스패치될 수 있다. 대가는 배포 중 수십 초의 중단이다. web은 상태가 없어 기본 롤링이다.
    """
    if service == API_SERVICE:
        return {"minimumHealthyPercent": 0, "maximumPercent": 100,
                "deploymentCircuitBreaker": {"enable": True, "rollback": True}}
    return {"minimumHealthyPercent": 100, "maximumPercent": 200,
            "deploymentCircuitBreaker": {"enable": True, "rollback": True}}


def public_origins(dist: dict) -> list[str]:
    """화면이 열리는 주소 목록 — **별칭(사용자 도메인)이 먼저**, CloudFront 기본 도메인이 마지막이다.

    첫 값이 웹 번들에 박힐 API 주소이고(build), 전부가 api의 CORS 허용 출처다(deploy). 기본 도메인을
    빼지 않는 이유: 별칭을 단 뒤에도 그 주소로 열 수 있고, 거기서 실시간 연결(WS의 Origin 검사)이
    끊기면 안 된다. 받는 모양은 list_distributions의 배포 요약이다.
    """
    aliases = (dist.get("Aliases") or {}).get("Items") or []
    return [f"https://{a}" for a in aliases] + [f"https://{dist['DomainName']}"]


def custom_domain_config(config: dict, domain: str, cert_arn: str) -> dict:
    """배포 설정에 별칭과 인증서를 단 사본. 나머지 설정은 그대로 둔다(update_distribution은 전체 교체다)."""
    updated = dict(config)
    updated["Aliases"] = {"Quantity": 1, "Items": [domain]}
    updated["ViewerCertificate"] = {"ACMCertificateArn": cert_arn, "SSLSupportMethod": "sni-only",
                                    "MinimumProtocolVersion": "TLSv1.2_2021"}
    return updated


def certificate_covers(cert: dict, domain: str) -> bool:
    """ACM 인증서 요약이 이 도메인을 덮는가 — 정확히 같거나 한 단계 와일드카드(*.상위)."""
    names = {cert.get("DomainName", "")} | set(cert.get("SubjectAlternativeNameSummaries") or [])
    parent = domain.split(".", 1)[1] if "." in domain else ""
    return domain in names or (bool(parent) and f"*.{parent}" in names)


def validate_allow_cidrs(cidrs: list[str]) -> list[str]:
    """WAF 허용 목록 검사 — IPv4 CIDR만, /MIN_ALLOW_PREFIX보다 넓으면 거부한다.

    로그인이 없는 MVP라 허용 IP가 출입문이다(ADR-0010 §4). WAF는 /0만 거절하므로 0.0.0.0/1 같은
    광역이 그대로 통과한다 — 여기서 막는다. 호스트 비트가 섞인 값(1.2.3.4/24)도 오타로 보고 거부한다.
    """
    out = []
    for raw in cidrs:
        try:
            net = ipaddress.ip_network(raw, strict=True)
        except ValueError as exc:
            sys.exit(f"--allow-cidr {raw!r}는 올바른 CIDR가 아니다 — {exc}")
        if net.version != 4:
            sys.exit(f"--allow-cidr {raw}: IPv4만 받는다(WAF IP 집합이 IPV4다)")
        if net.prefixlen < MIN_ALLOW_PREFIX:
            sys.exit(f"--allow-cidr {raw}: /{MIN_ALLOW_PREFIX}보다 넓은 범위는 받지 않는다 — "
                     "로그인이 없는 MVP라 허용 IP가 출입문이다(ADR-0010 §4)")
        out.append(str(net))
    return out


def database_url(password: str, host: str) -> str:
    return f"postgresql+psycopg://{DB_USER}:{password}@{host}:5432/{DB_NAME}"


def is_platform_tagged(tags: list[dict]) -> bool:
    """이름이 같아도 이 태그가 없으면 플랫폼 스크립트가 만든 자원이 아니다."""
    return any(t.get("Key") == TAG_KEY and t.get("Value") == TAG_VALUE for t in tags or [])


def customer_access_owner(tags: list[dict]) -> Optional[str]:
    """고객 계정 접근 역할의 고객 태그 값 — 그 역할을 쓰는 플랫폼 계정 ID. 태그가 없으면 None."""
    return next((t.get("Value") for t in tags or [] if t.get("Key") == CUSTOMER_TAG_KEY), None)


def alarm_definitions(lb_arn: str, tg_api_arn: str, tg_web_arn: str, topic_arn: str) -> list[dict]:
    """플랫폼 오류 알람의 put_metric_alarm 인자(T-8 운영 장치). ALARM과 OK 둘 다 메일로 알린다.

    정상 대상 알람은 `Maximum`으로 1분마다 보고 DOWN_MINUTES분 연속 0일 때만 울린다 — 배포 중 api가
    잠시 0이 되는 것(service_deployment)으로는 울리지 않는다. 지표가 끊겨도 울린다(breaching).
    대상이 없으면 ALB가 정상 대상 수를 싣지 않을 수 있어서다.
    """
    lb_dim = {"Name": "LoadBalancer", "Value": lb_arn.split(":loadbalancer/", 1)[1]}

    def down(name: str, tg_arn: str) -> dict:
        return {"AlarmName": name, "Namespace": "AWS/ApplicationELB", "MetricName": "HealthyHostCount",
                "Dimensions": [{"Name": "TargetGroup", "Value": tg_arn.rsplit(":", 1)[1]}, lb_dim],
                "Statistic": "Maximum", "Period": 60, "EvaluationPeriods": DOWN_MINUTES,
                "DatapointsToAlarm": DOWN_MINUTES, "Threshold": 1, "ComparisonOperator": "LessThanThreshold",
                "TreatMissingData": "breaching"}

    alarms = [
        {**down(ALARM_NAMES[0], tg_api_arn),
         "AlarmDescription": f"api 정상 대상이 {DOWN_MINUTES}분 연속 0 — 화면이 데이터를 못 받는다"},
        {**down(ALARM_NAMES[1], tg_web_arn),
         "AlarmDescription": f"web 정상 대상이 {DOWN_MINUTES}분 연속 0 — 화면이 열리지 않는다"},
        {"AlarmName": ALARM_NAMES[2], "AlarmDescription": f"api 5xx가 5분에 {API_5XX_PER_5MIN}건 이상",
         "Namespace": "AWS/ApplicationELB", "MetricName": "HTTPCode_Target_5XX_Count", "Dimensions": [lb_dim],
         "Statistic": "Sum", "Period": 300, "EvaluationPeriods": 1, "Threshold": API_5XX_PER_5MIN,
         "ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching"},
        {"AlarmName": ALARM_NAMES[3], "AlarmDescription": "DB 남은 저장 공간이 2 GiB 미만",
         "Namespace": "AWS/RDS", "MetricName": "FreeStorageSpace",
         "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": DB_INSTANCE}],
         "Statistic": "Minimum", "Period": 300, "EvaluationPeriods": 1, "Threshold": DB_FREE_BYTES_MIN,
         "ComparisonOperator": "LessThanThreshold", "TreatMissingData": "missing"},
    ]
    return [{**a, "ActionsEnabled": True, "AlarmActions": [topic_arn], "OKActions": [topic_arn]} for a in alarms]


# ------------------------------------------------------------------ 공통
def _require_real_aws(expected_account: str) -> dict:
    endpoint = endpoint_url()
    if endpoint:
        sys.exit(f"AWS_ENDPOINT_URL={endpoint} 설정됨 — 이 스크립트는 실 AWS 전용이다. 셸에서 지우고 다시 실행할 것.")
    identity = aws_client("sts").get_caller_identity()
    if identity["Account"] != expected_account:
        sys.exit(f"--account {expected_account} ≠ 호출 주체의 계정 {identity['Account']} — AWS 프로필을 확인할 것.")
    print(f"[platform] 계정 {identity['Account']} · 주체 {identity['Arn']} · 리전 {default_region()}")
    return identity


def _customer_clients(profile: str, expected_account: str, region: str) -> dict:
    """고객 계정 관리자 세션 — 접근 역할과 스모크 정책·VPC에만 쓴다. 환경 변수·앱 클라이언트 설정을 바꾸지 않는다."""
    session = boto3.Session(profile_name=profile, region_name=region)
    config = Config(ignore_configured_endpoint_urls=True)
    identity = session.client("sts", config=config).get_caller_identity()
    if identity["Account"] != expected_account:
        sys.exit(f"--customer-account {expected_account} ≠ --customer-profile {profile}의 계정 {identity['Account']}"
                 " — AWS 프로필을 확인할 것.")
    print(f"[platform] 고객 계정 {identity['Account']} · 주체 {identity['Arn']}")
    return {"iam": session.client("iam", config=config), "ec2": session.client("ec2", config=config),
            "account": expected_account}


def _customer(c: dict, account: str) -> dict:
    """접근 역할을 둘 계정(iam · ec2 · account). 고객 인자가 없으면 플랫폼 계정 자신이다(MVP 단일 계정)."""
    return c.get("customer") or {"iam": c["iam"], "ec2": c["ec2"], "account": account}


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _tags(name: str) -> list[dict]:
    return [{"Key": "Name", "Value": name}, {"Key": TAG_KEY, "Value": TAG_VALUE}]


def _tag_spec(resource_type: str, name: str) -> list[dict]:
    return [{"ResourceType": resource_type, "Tags": _tags(name)}]


def _kv_tags(name: str) -> list[dict]:
    return _tags(name)


def _named(ec2_call, key: str, name: str, extra: Optional[list] = None) -> list[dict]:
    filters = [{"Name": "tag:Name", "Values": [name]}, {"Name": f"tag:{TAG_KEY}", "Values": [TAG_VALUE]}]
    return ec2_call(Filters=filters + (extra or []))[key]


def _retry(fn: Callable[[], Any], what: str, codes: tuple[str, ...], timeout: int = 900) -> Any:
    deadline = time.time() + timeout
    while True:
        try:
            return fn()
        except ClientError as exc:
            if _code(exc) not in codes or time.time() > deadline:
                raise
            print(f"[platform] {what} 대기 — {_code(exc)}")
            time.sleep(15)


def _vpc_filter(vpc_id: str) -> dict:
    return {"Name": "vpc-id", "Values": [vpc_id]}


# ------------------------------------------------------------------ 찾기
def _find_vpc(ec2) -> Optional[str]:
    vpcs = _named(ec2.describe_vpcs, "Vpcs", VPC_NAME)
    return vpcs[0]["VpcId"] if vpcs else None


def _find_smoke_vpc(ec2) -> Optional[str]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [SMOKE_VPC_NAME]}])["Vpcs"]
    return vpcs[0]["VpcId"] if vpcs else None


def _find_subnets(ec2, vpc_id: str) -> dict[str, str]:
    out = {}
    for s in ec2.describe_subnets(Filters=[_vpc_filter(vpc_id)])["Subnets"]:
        name = next((t["Value"] for t in s.get("Tags", []) if t["Key"] == "Name"), "")
        out[name] = s["SubnetId"]
    return out


def _find_sgs(ec2, vpc_id: str) -> dict[str, str]:
    groups = ec2.describe_security_groups(Filters=[_vpc_filter(vpc_id)])["SecurityGroups"]
    return {g["GroupName"]: g["GroupId"] for g in groups if g["GroupName"] in SECURITY_GROUPS}


def _find_nat(ec2, vpc_id: str) -> Optional[dict]:
    nats = ec2.describe_nat_gateways(Filters=[_vpc_filter(vpc_id),
                                              {"Name": "state", "Values": ["pending", "available"]}])["NatGateways"]
    return nats[0] if nats else None


def _find_db(rds) -> Optional[dict]:
    try:
        return rds.describe_db_instances(DBInstanceIdentifier=DB_INSTANCE)["DBInstances"][0]
    except ClientError as exc:
        if _code(exc) == "DBInstanceNotFound":
            return None
        raise


def _find_secret(sm, name: str) -> Optional[str]:
    try:
        return sm.describe_secret(SecretId=name)["ARN"]
    except ClientError as exc:
        if _code(exc) == "ResourceNotFoundException":
            return None
        raise


def _get_role(iam, name: str) -> Optional[dict]:
    try:
        return iam.get_role(RoleName=name)["Role"]
    except ClientError as exc:
        if _code(exc) == "NoSuchEntity":
            return None
        raise


def _find_role(iam, name: str) -> Optional[str]:
    role = _get_role(iam, name)
    return role["Arn"] if role else None


def _find_repo(ecr, name: str) -> Optional[str]:
    try:
        return ecr.describe_repositories(repositoryNames=[name])["repositories"][0]["repositoryUri"]
    except ClientError as exc:
        if _code(exc) == "RepositoryNotFoundException":
            return None
        raise


def _find_lb(elbv2) -> Optional[dict]:
    try:
        return elbv2.describe_load_balancers(Names=[ALB_NAME])["LoadBalancers"][0]
    except ClientError as exc:
        if _code(exc) == "LoadBalancerNotFound":
            return None
        raise


def _find_tg(elbv2, name: str) -> Optional[str]:
    try:
        return elbv2.describe_target_groups(Names=[name])["TargetGroups"][0]["TargetGroupArn"]
    except ClientError as exc:
        if _code(exc) == "TargetGroupNotFound":
            return None
        raise


def _find_ipset(waf, name: str = WAF_IPSET) -> Optional[dict]:
    for s in waf.list_ip_sets(Scope="CLOUDFRONT", Limit=100).get("IPSets", []):
        if s["Name"] == name:
            return s
    return None


def _find_acl(waf) -> Optional[dict]:
    for a in waf.list_web_acls(Scope="CLOUDFRONT", Limit=100).get("WebACLs", []):
        if a["Name"] == WAF_ACL:
            return a
    return None


def _find_distribution(cf) -> Optional[dict]:
    for page in cf.get_paginator("list_distributions").paginate():
        for d in page.get("DistributionList", {}).get("Items", []) or []:
            if d.get("Comment") == CF_COMMENT:
                return d
    return None


def _find_certificate(acm, domain: str) -> Optional[str]:
    for page in acm.get_paginator("list_certificates").paginate(CertificateStatuses=["ISSUED"]):
        for cert in page.get("CertificateSummaryList", []):
            if certificate_covers(cert, domain):
                return cert["CertificateArn"]
    return None


def _find_zone(r53, domain: str) -> Optional[str]:
    """도메인을 담는 가장 긴 이름의 공개 호스팅 영역 ID."""
    best: Optional[tuple[int, str]] = None
    for page in r53.get_paginator("list_hosted_zones").paginate():
        for zone in page["HostedZones"]:
            name = zone["Name"].rstrip(".")
            if zone.get("Config", {}).get("PrivateZone"):
                continue
            if (domain == name or domain.endswith(f".{name}")) and (best is None or len(name) > best[0]):
                best = (len(name), zone["Id"].rsplit("/", 1)[-1])
    return best[1] if best else None


def alias_records_for(records: list[dict], domain: str, cf_domain: str) -> list[dict]:
    """이 배포를 가리키는 domain의 A·AAAA 별칭 레코드만. 다른 곳을 가리키는 레코드는 대상이 아니다."""
    return [r for r in records
            if r["Name"].rstrip(".") == domain and r["Type"] in ("A", "AAAA")
            and r.get("AliasTarget", {}).get("DNSName", "").rstrip(".") == cf_domain]


def _find_topic(sns) -> Optional[str]:
    for page in sns.get_paginator("list_topics").paginate():
        for topic in page.get("Topics", []):
            if topic["TopicArn"].rsplit(":", 1)[-1] == ALARM_TOPIC:
                return topic["TopicArn"]
    return None


def _find_cluster(ecs) -> Optional[str]:
    found = ecs.describe_clusters(clusters=[CLUSTER])["clusters"]
    return found[0]["clusterArn"] if found and found[0]["status"] == "ACTIVE" else None


def _find_service(ecs, name: str) -> Optional[dict]:
    if not _find_cluster(ecs):
        return None
    found = ecs.describe_services(cluster=CLUSTER, services=[name])["services"]
    return found[0] if found and found[0]["status"] == "ACTIVE" else None


def inventory(c: dict) -> list[tuple[str, str, Optional[str]]]:
    ec2, elbv2 = c["ec2"], c["elbv2"]
    vpc_id = _find_vpc(ec2)
    rows: list[tuple[str, str, Optional[str]]] = [("VPC", VPC_NAME, vpc_id)]
    subnets = _find_subnets(ec2, vpc_id) if vpc_id else {}
    for tier, table in (("public", PUBLIC_SUBNETS), ("private", PRIVATE_SUBNETS)):
        for az in table:
            name = f"{PREFIX}-{tier}-{az}"
            rows.append(("Subnet", name, subnets.get(name)))
    nat = _find_nat(ec2, vpc_id) if vpc_id else None
    rows.append(("NatGateway", f"{PREFIX}-nat", nat["NatGatewayId"] if nat else None))
    sgs = _find_sgs(ec2, vpc_id) if vpc_id else {}
    rows += [("SecurityGroup", n, sgs.get(n)) for n in SECURITY_GROUPS]
    db = _find_db(c["rds"])
    rows.append(("RDS", DB_INSTANCE, f"{db['DBInstanceIdentifier']} ({db['DBInstanceStatus']})" if db else None))
    rows += [("Secret", n, _find_secret(c["sm"], n)) for n in
             (SECRET_DATABASE_URL, SECRET_OPENAI, SECRET_ORIGIN, SECRET_EXTERNAL_ID)]
    rows += [("ECR", n, _find_repo(c["ecr"], n)) for n in ECR_REPOS.values()]
    rows += [("IamRole", n, _find_role(c["iam"], n)) for n in (ROLE_EXEC, ROLE_TASK)]
    customer = c.get("customer")
    rows.append(("IamRole(고객)" if customer else "IamRole", ROLE_ACCESS,
                 _find_role((customer or c)["iam"], ROLE_ACCESS)))
    rows.append(("EcsCluster", CLUSTER, _find_cluster(c["ecs"])))
    lb = _find_lb(elbv2)
    rows.append(("LoadBalancer", ALB_NAME, lb["DNSName"] if lb else None))
    rows += [("TargetGroup", n, _find_tg(elbv2, n)) for n in (TG_API, TG_WEB)]
    rows.append(("WafIpSet", WAF_IPSET, (_find_ipset(c["waf"]) or {}).get("Id")))
    rows.append(("WafIpSet", WAF_NAT_IPSET, (_find_ipset(c["waf"], WAF_NAT_IPSET) or {}).get("Id")))
    rows.append(("WafWebAcl", WAF_ACL, (_find_acl(c["waf"]) or {}).get("Id")))
    dist = _find_distribution(c["cf"])
    rows.append(("CloudFront", CF_COMMENT, " · ".join(public_origins(dist)) if dist else None))
    rows.append(("SnsTopic", ALARM_TOPIC, _find_topic(c["sns"])))
    alarms = c["cw"].describe_alarms(AlarmNames=list(ALARM_NAMES))["MetricAlarms"]
    rows.append(("Alarm", f"{PREFIX}-* {len(ALARM_NAMES)}종",
                 ", ".join(f"{a['AlarmName'].removeprefix(PREFIX + '-')} {a['StateValue']}" for a in alarms) or None))
    for name in (API_SERVICE, WEB_SERVICE):
        svc = _find_service(c["ecs"], name)
        rows.append(("EcsService", name,
                     f"running {svc['runningCount']}/{svc['desiredCount']}" if svc else None))
    return rows


def _print_rows(rows: list[tuple[str, str, Optional[str]]]) -> None:
    for kind, name, value in rows:
        mark = "✅" if value else "—"
        print(f"  {mark}  {kind:<14} {name:<36} {value or '없음'}")


def status(c: dict) -> None:
    print("[platform] 플랫폼 자원")
    _print_rows(inventory(c))
    for name in (TG_API, TG_WEB):
        arn = _find_tg(c["elbv2"], name)
        if arn:
            states = [t["TargetHealth"]["State"] for t in
                      c["elbv2"].describe_target_health(TargetGroupArn=arn)["TargetHealthDescriptions"]]
            print(f"  TG {name}: {', '.join(states) or '대상 없음'}")


# ------------------------------------------------------------------ 구성(up)
def _ensure_vpc(ec2) -> str:
    vpc_id = _find_vpc(ec2)
    if vpc_id:
        return vpc_id
    vpc_id = ec2.create_vpc(CidrBlock=VPC_CIDR, TagSpecifications=_tag_spec("vpc", VPC_NAME))["Vpc"]["VpcId"]
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
    # RDS 엔드포인트 이름을 VPC 안에서 풀어야 한다
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    print(f"[platform] VPC {vpc_id} 생성")
    return vpc_id


def _ensure_subnets(ec2, vpc_id: str, region: str) -> dict[str, str]:
    have = _find_subnets(ec2, vpc_id)
    for tier, table in (("public", PUBLIC_SUBNETS), ("private", PRIVATE_SUBNETS)):
        for az, cidr in table.items():
            name = f"{PREFIX}-{tier}-{az}"
            if name in have:
                continue
            sid = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=f"{region}{az}",
                                    TagSpecifications=_tag_spec("subnet", name))["Subnet"]["SubnetId"]
            have[name] = sid
            print(f"[platform] Subnet {name} {sid} 생성")
    return have


def _ensure_route_table(ec2, vpc_id: str, name: str, subnet_ids: list[str], route: dict) -> None:
    tables = _named(ec2.describe_route_tables, "RouteTables", name, [_vpc_filter(vpc_id)])
    if tables:
        rt_id = tables[0]["RouteTableId"]
    else:
        rt_id = ec2.create_route_table(VpcId=vpc_id, TagSpecifications=_tag_spec("route-table", name))[
            "RouteTable"]["RouteTableId"]
        print(f"[platform] RouteTable {name} {rt_id} 생성")
        tables = [{"Routes": [], "Associations": []}]
    if not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" for r in tables[0]["Routes"]):
        ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", **route)
    associated = {a.get("SubnetId") for a in tables[0]["Associations"]}
    for sid in subnet_ids:
        if sid not in associated:
            ec2.associate_route_table(RouteTableId=rt_id, SubnetId=sid)


def _ensure_network(ec2, vpc_id: str, subnets: dict[str, str]) -> str:
    """IGW·공개 경로·NAT·프라이빗 경로. NAT의 공인 IP를 돌려준다(WAF 허용 목록에 들어간다)."""
    igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])[
        "InternetGateways"]
    if igws:
        igw_id = igws[0]["InternetGatewayId"]
    else:
        igw_id = ec2.create_internet_gateway(TagSpecifications=_tag_spec("internet-gateway", f"{PREFIX}-igw"))[
            "InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        print(f"[platform] IGW {igw_id} 생성·연결")
    public_ids = [subnets[f"{PREFIX}-public-{az}"] for az in PUBLIC_SUBNETS]
    _ensure_route_table(ec2, vpc_id, f"{PREFIX}-public", public_ids, {"GatewayId": igw_id})

    nat = _find_nat(ec2, vpc_id)
    if not nat:
        eips = _named(ec2.describe_addresses, "Addresses", f"{PREFIX}-nat")
        alloc = eips[0]["AllocationId"] if eips else ec2.allocate_address(
            Domain="vpc", TagSpecifications=_tag_spec("elastic-ip", f"{PREFIX}-nat"))["AllocationId"]
        nat_id = ec2.create_nat_gateway(SubnetId=subnets[f"{PREFIX}-public-{NAT_AZ}"], AllocationId=alloc,
                                        TagSpecifications=_tag_spec("natgateway", f"{PREFIX}-nat"))[
            "NatGateway"]["NatGatewayId"]
        print(f"[platform] NAT {nat_id} 생성 — available 대기")
        ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat_id])
        nat = ec2.describe_nat_gateways(NatGatewayIds=[nat_id])["NatGateways"][0]
    private_ids = [subnets[f"{PREFIX}-private-{az}"] for az in PRIVATE_SUBNETS]
    _ensure_route_table(ec2, vpc_id, f"{PREFIX}-private", private_ids, {"NatGatewayId": nat["NatGatewayId"]})
    return nat["NatGatewayAddresses"][0]["PublicIp"]


def _cloudfront_prefix_list(ec2) -> str:
    lists = ec2.describe_managed_prefix_lists(
        Filters=[{"Name": "prefix-list-name", "Values": [CLOUDFRONT_PREFIX_LIST]}])["PrefixLists"]
    if not lists:
        sys.exit(f"관리형 prefix list {CLOUDFRONT_PREFIX_LIST}를 찾지 못했다")
    return lists[0]["PrefixListId"]


def _ensure_sgs(ec2, vpc_id: str) -> dict[str, str]:
    have = _find_sgs(ec2, vpc_id)
    for name in SECURITY_GROUPS:
        if name not in have:
            have[name] = ec2.create_security_group(
                GroupName=name, Description=f"{PREFIX} {name.rsplit('-', 1)[-1]}", VpcId=vpc_id,
                TagSpecifications=_tag_spec("security-group", name))["GroupId"]
            print(f"[platform] SG {name} {have[name]} 생성")
    rules = {
        SG_ALB: [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                  "PrefixListIds": [{"PrefixListId": _cloudfront_prefix_list(ec2),
                                     "Description": "CloudFront origin-facing only"}]}],
        SG_API: [{"IpProtocol": "tcp", "FromPort": API_PORT, "ToPort": API_PORT,
                  "UserIdGroupPairs": [{"GroupId": have[SG_ALB]}]}],
        SG_WEB: [{"IpProtocol": "tcp", "FromPort": WEB_PORT, "ToPort": WEB_PORT,
                  "UserIdGroupPairs": [{"GroupId": have[SG_ALB]}]}],
        SG_DB: [{"IpProtocol": "tcp", "FromPort": 5432, "ToPort": 5432,
                 "UserIdGroupPairs": [{"GroupId": have[SG_API]}]}],
    }
    for name, perms in rules.items():
        try:
            ec2.authorize_security_group_ingress(GroupId=have[name], IpPermissions=perms)
        except ClientError as exc:
            if _code(exc) != "InvalidPermission.Duplicate":
                raise
    return have


def _put_secret(sm, name: str, value: str) -> str:
    arn = _find_secret(sm, name)
    if arn:
        return arn
    arn = sm.create_secret(Name=name, SecretString=value, Tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}])["ARN"]
    print(f"[platform] Secret {name} 생성")
    return arn


def _secret_value(sm, name: str) -> str:
    return sm.get_secret_value(SecretId=name)["SecretString"]


def _ensure_db(rds, sm, subnets: dict[str, str], sg_db: str) -> None:
    """비밀번호는 **RDS 생성 요청 전에** 비밀로 저장한다 — 대기(수 분–30분) 중 중단돼도 잃지 않는다.

    순서: 접속 문자열 비밀(호스트 자리 DB_HOST_PENDING) → create_db_instance → available 대기 →
    호스트를 채워 put_secret_value. 어디서 끊겨도 재실행이 비밀의 비밀번호를 다시 쓰고 호스트만 채운다.
    RDS 관리 비밀(ManageMasterUserPassword)을 쓰지 않는 이유: 그 비밀은 기본 7일마다 회전되는데
    앱은 접속 문자열 하나(DATABASE_URL)를 받으므로, 회전 때마다 사본이 낡는다.
    """
    db = _find_db(rds)
    if not db and not _find_secret(sm, SECRET_DATABASE_URL):
        # 비밀번호는 출력하지 않고 DATABASE_URL 비밀로만 남긴다. URL에 넣을 수 있는 문자만 쓴다
        password = secrets.token_urlsafe(24).replace("-", "a").replace("_", "b")
        _put_secret(sm, SECRET_DATABASE_URL, database_url(password, DB_HOST_PENDING))
    if not _find_secret(sm, SECRET_DATABASE_URL):
        sys.exit(f"RDS {DB_INSTANCE}는 있는데 {SECRET_DATABASE_URL} 비밀이 없다 — 비밀번호를 알 수 없으므로 "
                 "RDS 마스터 비밀번호를 재설정하고 비밀을 직접 만들 것.")
    url = urlsplit(_secret_value(sm, SECRET_DATABASE_URL))
    if not db:
        try:
            rds.create_db_subnet_group(
                DBSubnetGroupName=DB_SUBNET_GROUP, DBSubnetGroupDescription=f"{PREFIX} private",
                SubnetIds=[subnets[f"{PREFIX}-private-{az}"] for az in PRIVATE_SUBNETS],
                Tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}])
        except ClientError as exc:
            if _code(exc) != "DBSubnetGroupAlreadyExists":
                raise
        rds.create_db_instance(
            DBInstanceIdentifier=DB_INSTANCE, DBInstanceClass=DB_CLASS, Engine="postgres",
            EngineVersion=DB_MAJOR, AllocatedStorage=DB_STORAGE_GB, StorageType="gp3",
            StorageEncrypted=True, DBName=DB_NAME, MasterUsername=DB_USER, MasterUserPassword=url.password,
            DBSubnetGroupName=DB_SUBNET_GROUP, VpcSecurityGroupIds=[sg_db], PubliclyAccessible=False,
            MultiAZ=False, BackupRetentionPeriod=DB_BACKUP_DAYS, CopyTagsToSnapshot=True, DeletionProtection=False,
            Tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}, {"Key": "Name", "Value": DB_INSTANCE}])
        print(f"[platform] RDS {DB_INSTANCE} 생성 요청 — available 대기(수 분)")
    elif db["DBInstanceStatus"] == "available" and db.get("BackupRetentionPeriod", 0) < DB_BACKUP_DAYS:
        # MVP 때 1일로 만든 인스턴스. 생성·수정 중에는 바꿀 수 없어 available일 때만 맞춘다
        rds.modify_db_instance(DBInstanceIdentifier=DB_INSTANCE, BackupRetentionPeriod=DB_BACKUP_DAYS,
                               ApplyImmediately=True)
        print(f"[platform] RDS {DB_INSTANCE} 자동 백업 보존 {db.get('BackupRetentionPeriod')}일 → {DB_BACKUP_DAYS}일")
    # 새로 만들었으면 호스트를 항상 다시 적는다(비밀이 지운 옛 인스턴스의 호스트를 들고 있을 수 있다)
    if not db or url.hostname == DB_HOST_PENDING:
        rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=DB_INSTANCE)
        host = _find_db(rds)["Endpoint"]["Address"]
        sm.put_secret_value(SecretId=SECRET_DATABASE_URL, SecretString=database_url(url.password, host))
        print(f"[platform] Secret {SECRET_DATABASE_URL}에 RDS 호스트 기록")


def _ensure_repos(ecr) -> dict[str, str]:
    out = {}
    for key, name in ECR_REPOS.items():
        uri = _find_repo(ecr, name)
        if not uri:
            uri = ecr.create_repository(repositoryName=name, imageScanningConfiguration={"scanOnPush": True},
                                        tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}])["repository"]["repositoryUri"]
            print(f"[platform] ECR {name} 생성")
        out[key] = uri
    return out


def _ensure_role(iam, name: str, trust: dict, description: str) -> str:
    role = _get_role(iam, name)
    if role:
        # 이름만 같은 남의 역할이면 신뢰 정책을 덮어쓰지 않는다(안전 가드 ⑤)
        if not is_platform_tagged(role.get("Tags", [])):
            sys.exit(f"IAM 역할 {name}이 이미 있는데 플랫폼 태그({TAG_KEY}={TAG_VALUE})가 없다 — "
                     "다른 용도의 역할일 수 있어 덮어쓰지 않는다. 확인 뒤 이름을 비우거나 태그를 달 것.")
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        return role["Arn"]
    arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description=description,
                          MaxSessionDuration=3600, Tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}])["Role"]["Arn"]
    print(f"[platform] IAM 역할 {name} 생성")
    return arn


def _smoke_policy_arn(account: str) -> str:
    return f"arn:aws:iam::{account}:policy/{SMOKE_POLICY_NAME}"


def _require_smoke_env(iam, ec2, account: str) -> None:
    """스모크 환경(앱 정책·VPC)이 먼저 있어야 한다 — **비싼 자원을 만들기 전에** 본다.

    접근 역할은 스모크의 Action Whitelist 거울 정책을 빌려 쓰고, 배포는 스모크 VPC를 관제 범위로
    넣는다. 둘 중 하나라도 없으면 NAT·RDS(시간 과금)를 세운 뒤에야 멈추게 되므로 up의 맨 앞에 둔다.
    계정을 갈랐으면 iam·ec2·account는 고객 계정의 것이다(_customer).
    """
    hint = " 계정을 갈랐으면 --customer-account·--customer-profile로 고객 계정을 줄 것."
    try:
        iam.get_policy(PolicyArn=_smoke_policy_arn(account))
    except ClientError as exc:
        if _code(exc) == "NoSuchEntity":
            sys.exit(f"{SMOKE_POLICY_NAME} 정책이 없다 — scripts/provision_smoke_aws.py up을 먼저 돌릴 것"
                     "(접근 역할은 그 Action Whitelist 거울 정책을 빌려 쓴다)." + hint)
        raise
    if not _find_smoke_vpc(ec2):
        sys.exit(f"스모크 VPC({SMOKE_VPC_NAME})가 없다 — scripts/provision_smoke_aws.py up을 먼저 돌릴 것"
                 "(배포가 그 VPC를 관제 범위 COLLECT_VPC_IDS로 넣는다)." + hint)


def _ensure_customer_access_role(iam, platform_account: str, trust: dict) -> str:
    """고객 계정의 접근 역할(안전 가드 ⑧). 고객 태그로 식별하고 신뢰 정책을 이 플랫폼 계정으로 맞춘다.

    플랫폼 태그만 달린 역할은 같은 계정의 옛 플랫폼이 만든 것이라 넘겨받는다. 고객 태그를 먼저 달고
    신뢰 정책을 바꾼 뒤 플랫폼 태그를 뗀다 — 어디서 끊겨도 재실행이 마저 맞춘다.
    """
    description = "Vigilantis monitored-account access (SaaS customer role)"
    role = _get_role(iam, ROLE_ACCESS)
    if role is None:
        arn = iam.create_role(RoleName=ROLE_ACCESS, AssumeRolePolicyDocument=json.dumps(trust),
                              Description=description, MaxSessionDuration=3600,
                              Tags=[{"Key": CUSTOMER_TAG_KEY, "Value": platform_account}])["Role"]["Arn"]
        print(f"[platform] 고객 계정 IAM 역할 {ROLE_ACCESS} 생성")
        return arn
    tags = role.get("Tags", [])
    owner = customer_access_owner(tags)
    if owner is None and not is_platform_tagged(tags):
        sys.exit(f"고객 계정의 IAM 역할 {ROLE_ACCESS}에 고객 태그({CUSTOMER_TAG_KEY})도 플랫폼 태그도 없다 — "
                 "다른 용도의 역할일 수 있어 덮어쓰지 않는다.")
    if owner not in (None, platform_account):
        sys.exit(f"고객 계정의 IAM 역할 {ROLE_ACCESS}는 플랫폼 계정 {owner}의 것이다 — 덮어쓰지 않는다.")
    if owner is None:
        iam.tag_role(RoleName=ROLE_ACCESS, Tags=[{"Key": CUSTOMER_TAG_KEY, "Value": platform_account}])
        print(f"[platform] 고객 계정의 옛 플랫폼 접근 역할 {ROLE_ACCESS}를 넘겨받는다 — 옛 플랫폼은 이제 이 역할로 "
              "들어가지 못하고, 옛 플랫폼 down은 이 역할을 지우지 않는다")
    iam.update_assume_role_policy(RoleName=ROLE_ACCESS, PolicyDocument=json.dumps(trust))
    if is_platform_tagged(tags):
        iam.untag_role(RoleName=ROLE_ACCESS, TagKeys=[TAG_KEY])
    return role["Arn"]


def _ensure_roles(iam, account: str, secret_arns: list[str], external_id: str,
                  customer: Optional[dict] = None) -> dict[str, str]:
    """실행·태스크 역할은 플랫폼 계정에, 접근 역할은 고객 계정에 둔다(customer — 없으면 플랫폼 계정 자신)."""
    customer = customer or {"iam": iam, "account": account}
    smoke_policy = _smoke_policy_arn(customer["account"])  # 존재는 up 맨 앞(_require_smoke_env)에서 확인했다
    exec_arn = _ensure_role(iam, ROLE_EXEC, ecs_tasks_trust_policy(), "ECS image pull and secret injection")
    iam.attach_role_policy(RoleName=ROLE_EXEC,
                           PolicyArn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
    iam.put_role_policy(RoleName=ROLE_EXEC, PolicyName="read-platform-secrets",
                        PolicyDocument=json.dumps(exec_secrets_policy(secret_arns)))
    task_arn = _ensure_role(iam, ROLE_TASK, ecs_tasks_trust_policy(), "Vigilantis api - assume access role only")
    iam.put_role_policy(RoleName=ROLE_TASK, PolicyName="assume-access-role",
                        PolicyDocument=json.dumps(task_role_policy(customer["account"])))
    trust = access_trust_policy(account, external_id)

    def ensure_access() -> str:
        if customer["account"] == account:
            return _ensure_role(iam, ROLE_ACCESS, trust, "Vigilantis monitored-account access (SaaS customer role shape)")
        return _ensure_customer_access_role(customer["iam"], account, trust)

    # 신뢰 정책의 주체(태스크 역할)는 이미 있어야 IAM이 받아 준다 — 새 역할 직후에는 전파 대기
    access_arn = _retry(ensure_access, "접근 역할 신뢰 정책(주체 전파)", ("MalformedPolicyDocument",), timeout=120)
    customer["iam"].attach_role_policy(RoleName=ROLE_ACCESS, PolicyArn=smoke_policy)
    return {"exec": exec_arn, "task": task_arn, "access": access_arn}


def _ensure_logs(logs) -> None:
    for name in LOG_GROUPS.values():
        try:
            logs.create_log_group(logGroupName=name, tags={TAG_KEY: TAG_VALUE})
            logs.put_retention_policy(logGroupName=name, retentionInDays=LOG_RETENTION_DAYS)
        except ClientError as exc:
            if _code(exc) != "ResourceAlreadyExistsException":
                raise


def _ensure_cluster(ecs) -> None:
    if not _find_cluster(ecs):
        ecs.create_cluster(clusterName=CLUSTER, tags=[{"key": TAG_KEY, "value": TAG_VALUE}])
        print(f"[platform] ECS 클러스터 {CLUSTER} 생성")


def _ensure_target_group(elbv2, vpc_id: str, name: str, port: int, health: str) -> str:
    arn = _find_tg(elbv2, name)
    if not arn:
        arn = elbv2.create_target_group(
            Name=name, Protocol="HTTP", Port=port, VpcId=vpc_id, TargetType="ip",
            HealthCheckPath=health, Matcher={"HttpCode": "200-399"}, Tags=_kv_tags(name))["TargetGroups"][0][
            "TargetGroupArn"]
        print(f"[platform] TG {name} 생성")
    # 이미 있는 TG에도 매번 맞춘다 — 같은 값이면 바뀌는 것이 없다
    elbv2.modify_target_group_attributes(TargetGroupArn=arn, Attributes=target_group_attributes())
    return arn


def _ensure_alb(elbv2, vpc_id: str, subnets: dict[str, str], sg_alb: str, origin_value: str) -> dict:
    tg_api = _ensure_target_group(elbv2, vpc_id, TG_API, API_PORT, "/health")
    tg_web = _ensure_target_group(elbv2, vpc_id, TG_WEB, WEB_PORT, WEB_HEALTH_PATH)
    lb = _find_lb(elbv2)
    if not lb:
        lb = elbv2.create_load_balancer(
            Name=ALB_NAME, Scheme="internet-facing", Type="application", IpAddressType="ipv4",
            Subnets=[subnets[f"{PREFIX}-public-{az}"] for az in PUBLIC_SUBNETS], SecurityGroups=[sg_alb],
            Tags=_kv_tags(ALB_NAME))["LoadBalancers"][0]
        print(f"[platform] ALB {ALB_NAME} 생성(internet-facing · CloudFront만 수신)")
        elbv2.get_waiter("load_balancer_available").wait(LoadBalancerArns=[lb["LoadBalancerArn"]])
    listeners = elbv2.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])["Listeners"]
    if listeners:
        listener_arn = listeners[0]["ListenerArn"]
    else:
        listener_arn = elbv2.create_listener(
            LoadBalancerArn=lb["LoadBalancerArn"], Protocol="HTTP", Port=80,
            DefaultActions=[{"Type": "fixed-response", "FixedResponseConfig": {
                "StatusCode": "403", "ContentType": "text/plain", "MessageBody": "forbidden"}}])["Listeners"][0][
            "ListenerArn"]
    existing = {r["Priority"] for r in elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]}
    for rule in listener_rules(tg_api, tg_web, origin_value):
        if str(rule["Priority"]) not in existing:
            elbv2.create_rule(ListenerArn=listener_arn, **rule)
    return {"arn": lb["LoadBalancerArn"], "dns": lb["DNSName"], "tg_api": tg_api, "tg_web": tg_web}


def _ensure_alarms(cw, sns, alb: dict, email: Optional[str]) -> None:
    """오류 알람과 그 알림 주제(SNS)를 맞춘다. put_metric_alarm은 같은 이름이면 덮어쓰므로 재실행이 곧 갱신이다."""
    topic_arn = _find_topic(sns) or sns.create_topic(
        Name=ALARM_TOPIC, Tags=[{"Key": TAG_KEY, "Value": TAG_VALUE}])["TopicArn"]
    subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
    if email and not any(s["Protocol"] == "email" and s["Endpoint"] == email for s in subs):
        sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
        print("[platform] 알람 메일 구독 요청 — 받은 확인 메일의 링크를 눌러야 알람이 온다")
    elif not email and not subs:
        print("[platform] 주의: 알람 주제에 구독이 없다 — up --alarm-email <주소>로 받을 곳을 달 것")
    for alarm in alarm_definitions(alb["arn"], alb["tg_api"], alb["tg_web"], topic_arn):
        cw.put_metric_alarm(**alarm, Tags=_kv_tags(alarm["AlarmName"]))
    print(f"[platform] 오류 알람 {len(ALARM_NAMES)}종 — 첫 배포 전에는 정상 대상이 없어 down 알람이 울린다")


def _ensure_ipset(waf, name: str, cidrs: list[str], label: str) -> str:
    """이름으로 IP 집합을 찾아 주소를 맞추고 ARN을 돌려준다. 없으면 만든다."""
    ipset = _find_ipset(waf, name)
    if ipset:
        cur = waf.get_ip_set(Name=name, Scope="CLOUDFRONT", Id=ipset["Id"])
        if sorted(cur["IPSet"]["Addresses"]) != sorted(cidrs):
            waf.update_ip_set(Name=name, Scope="CLOUDFRONT", Id=ipset["Id"], Addresses=sorted(cidrs),
                              LockToken=cur["LockToken"])
            print(f"[platform] WAF {label} 갱신 — {len(cidrs)}건")
        return cur["IPSet"]["ARN"]
    arn = waf.create_ip_set(Name=name, Scope="CLOUDFRONT", IPAddressVersion="IPV4",
                            Addresses=sorted(cidrs), Tags=_kv_tags(name))["Summary"]["ARN"]
    print(f"[platform] WAF {label} 생성 — {len(cidrs)}건")
    return arn


def _covers(current, desired) -> bool:
    """current가 desired의 키·값을 전부 같은 값으로 갖는가. AWS가 응답에 덧붙이는 기본 키는 보지 않는다.

    get_web_acl 응답은 요청에 없던 기본값을 실을 수 있다 — 통째로 비교하면 매 실행이 「다름」이 되어
    멱등이 깨진다. 목록은 길이와 순서까지 같아야 한다.
    """
    if isinstance(desired, dict):
        return isinstance(current, dict) and all(k in current and _covers(current[k], v) for k, v in desired.items())
    if isinstance(desired, list):
        return (isinstance(current, list) and len(current) == len(desired)
                and all(_covers(c, d) for c, d in zip(current, desired)))
    return current == desired


def _same_rules(current: list[dict], desired: list[dict]) -> bool:
    by_priority = sorted(current, key=lambda r: r.get("Priority", 0))
    return _covers(by_priority, desired)


_ACL_DEFAULT_ACTION = {"Block": {}}
_ACL_VISIBILITY = {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": WAF_ACL}


def _ensure_waf(waf, cidrs: list[str], nat_cidr: str) -> str:
    """허용 IP 집합 · NAT IP 집합 · 웹 ACL을 맞춘다. 웹 ACL이 이미 있으면 규칙을 waf_rules()로 맞춘다.

    규칙을 생성 때만 넣으면 waf_rules()를 고쳐도 이미 배포된 플랫폼에는 들어가지 않는다(#484).
    규칙이 이미 같으면 바꾸지 않는다 — 두 번째 실행은 변경 0이다.
    """
    ipset_arn = _ensure_ipset(waf, WAF_IPSET, cidrs, "허용 IP")
    nat_ipset_arn = _ensure_ipset(waf, WAF_NAT_IPSET, [nat_cidr], "NAT IP(속도 제한 제외)")
    rules = waf_rules(ipset_arn, nat_ipset_arn)
    acl = _find_acl(waf)
    if acl:
        cur = waf.get_web_acl(Name=WAF_ACL, Scope="CLOUDFRONT", Id=acl["Id"])
        if not _same_rules(cur["WebACL"].get("Rules", []), rules):
            waf.update_web_acl(Name=WAF_ACL, Scope="CLOUDFRONT", Id=acl["Id"], DefaultAction=_ACL_DEFAULT_ACTION,
                               Rules=rules, VisibilityConfig=_ACL_VISIBILITY, LockToken=cur["LockToken"])
            print(f"[platform] WAF 웹 ACL {WAF_ACL} 규칙 갱신 — 속도 제한 {RATE_LIMIT_PER_WINDOW}건/"
                  f"{RATE_WINDOW_SECONDS}초({RATE_ACTION}) → 허용 IP")
        return acl["ARN"]
    acl_arn = waf.create_web_acl(
        Name=WAF_ACL, Scope="CLOUDFRONT", DefaultAction=_ACL_DEFAULT_ACTION, Rules=rules,
        VisibilityConfig=_ACL_VISIBILITY, Tags=_kv_tags(WAF_ACL))["Summary"]["ARN"]
    print(f"[platform] WAF 웹 ACL {WAF_ACL} 생성(기본 차단 · 속도 제한 {RATE_ACTION})")
    return acl_arn


def _delete_waf(waf) -> None:
    """웹 ACL → IP 집합 둘 순서로 지운다. 웹 ACL이 두 집합을 참조하므로 ACL이 먼저다."""
    acl = _find_acl(waf)
    if acl:
        _retry(lambda: waf.delete_web_acl(Name=WAF_ACL, Scope="CLOUDFRONT", Id=acl["Id"],
                                          LockToken=waf.get_web_acl(Name=WAF_ACL, Scope="CLOUDFRONT",
                                                                    Id=acl["Id"])["LockToken"]),
               "WAF 웹 ACL 삭제", ("WAFAssociatedItemException",), timeout=600)
    for name in (WAF_IPSET, WAF_NAT_IPSET):
        ipset = _find_ipset(waf, name)
        if ipset:
            waf.delete_ip_set(Name=name, Scope="CLOUDFRONT", Id=ipset["Id"], LockToken=ipset["LockToken"])


def distribution_config(alb_dns: str, origin_value: str, acl_arn: str, caller_ref: str) -> dict:
    return {
        "CallerReference": caller_ref,
        "Comment": CF_COMMENT,
        "Enabled": True,
        "PriceClass": "PriceClass_200",  # 한국 엣지 포함 · 최상위 요금대 제외
        "HttpVersion": "http2",
        "WebACLId": acl_arn,
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "alb",
            "DomainName": alb_dns,
            # 도메인이 없어 ALB에 인증서를 못 단다 — CloudFront→ALB는 평문 HTTP다. 오리진은 공개 ALB의
            # 공인 주소라 이 구간과 비밀 헤더 값은 인터넷 구간을 평문으로 지날 수 있다(ADR-0010 §4)
            "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
                                   "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5},
            "CustomHeaders": {"Quantity": 1, "Items": [{"HeaderName": ORIGIN_HEADER, "HeaderValue": origin_value}]},
        }]},
        "DefaultCacheBehavior": {
            "TargetOriginId": "alb",
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            "CachePolicyId": CF_CACHING_DISABLED,               # 관제 화면은 캐시하지 않는다
            "OriginRequestPolicyId": CF_ALL_VIEWER_EXCEPT_HOST,  # WebSocket 업그레이드 헤더도 넘긴다
            "Compress": True,
        },
    }


def _ensure_distribution(cf, alb_dns: str, origin_value: str, acl_arn: str) -> str:
    dist = _find_distribution(cf)
    if dist:
        return dist["DomainName"]
    created = cf.create_distribution_with_tags(DistributionConfigWithTags={
        "DistributionConfig": distribution_config(alb_dns, origin_value, acl_arn, f"{PREFIX}-{int(time.time())}"),
        "Tags": {"Items": [{"Key": TAG_KEY, "Value": TAG_VALUE}]},
    })["Distribution"]
    print(f"[platform] CloudFront {created['Id']} 생성 — 전파에 10–15분 걸린다")
    return created["DomainName"]


def _require_custom_domain(c: dict, domain: str) -> tuple[str, str]:
    """--domain의 인증서·공개 호스팅 영역을 찾는다 — 없으면 **무엇이든 바꾸기 전에** 멈춘다.

    인증서 발급은 DNS 검증을 기다려야 하고 도메인 구입은 사람이 한다 — 둘 다 이 스크립트 밖이다.
    up 끝에서야 보면 NAT·RDS(시간 과금)와 WAF 허용 목록 변경이 남은 채 멈추므로 up 맨 앞에서 본다.
    """
    cert_arn = _find_certificate(c["acm"], domain)
    zone_id = _find_zone(c["r53"], domain)
    if not (cert_arn and zone_id):
        sys.exit(f"--domain {domain}: us-east-1 ACM 인증서(ISSUED) {'있음' if cert_arn else '없음'} · "
                 f"Route53 호스팅 영역 {'있음' if zone_id else '없음'} — 둘 다 먼저 준비할 것")
    return cert_arn, zone_id


def _ensure_custom_domain(c: dict, domain: str, cert_arn: str, zone_id: str) -> None:
    """CloudFront에 별칭·인증서를 달고 Route53 A 별칭을 이 배포로 맞춘다. 인증서·호스팅 영역은 만들지 않는다."""
    cf = c["cf"]
    dist = _find_distribution(cf)
    got = cf.get_distribution_config(Id=dist["Id"])
    config = got["DistributionConfig"]
    if ((config.get("Aliases") or {}).get("Items") or []) != [domain] or \
            (config.get("ViewerCertificate") or {}).get("ACMCertificateArn") != cert_arn:
        cf.update_distribution(Id=dist["Id"], IfMatch=got["ETag"],
                               DistributionConfig=custom_domain_config(config, domain, cert_arn))
        print(f"[platform] CloudFront 별칭 {domain} 연결 — 전파에 수 분 걸린다")
    c["r53"].change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={"Changes": [{
        "Action": "UPSERT",
        "ResourceRecordSet": {"Name": domain, "Type": "A", "AliasTarget": {
            "HostedZoneId": CF_HOSTED_ZONE_ID, "DNSName": dist["DomainName"], "EvaluateTargetHealth": False}},
    }]})
    print(f"[platform] Route53 {domain} A → {dist['DomainName']}")


def up(c: dict, account: str, region: str, allow_cidrs: list[str], domain: Optional[str] = None,
       alarm_email: Optional[str] = None) -> None:
    ec2 = c["ec2"]
    customer = _customer(c, account)
    _require_smoke_env(customer["iam"], customer["ec2"], customer["account"])  # 무엇이든 만들기 전에
    custom = _require_custom_domain(c, domain) if domain else None
    vpc_id = _ensure_vpc(ec2)
    subnets = _ensure_subnets(ec2, vpc_id, region)
    nat_ip = _ensure_network(ec2, vpc_id, subnets)
    sgs = _ensure_sgs(ec2, vpc_id)
    origin_value = _secret_value(c["sm"], SECRET_ORIGIN) if _find_secret(c["sm"], SECRET_ORIGIN) else None
    if origin_value is None:
        _put_secret(c["sm"], SECRET_ORIGIN, secrets.token_urlsafe(32))
        origin_value = _secret_value(c["sm"], SECRET_ORIGIN)
    if not _find_secret(c["sm"], SECRET_EXTERNAL_ID):
        _put_secret(c["sm"], SECRET_EXTERNAL_ID, secrets.token_urlsafe(24))
    external_id = _secret_value(c["sm"], SECRET_EXTERNAL_ID)
    _put_secret(c["sm"], SECRET_OPENAI, OPENAI_PLACEHOLDER)
    _ensure_db(c["rds"], c["sm"], subnets, sgs[SG_DB])
    _ensure_repos(c["ecr"])
    secret_arns = [_find_secret(c["sm"], n) for n in (SECRET_DATABASE_URL, SECRET_OPENAI, SECRET_EXTERNAL_ID)]
    _ensure_roles(c["iam"], account, [a for a in secret_arns if a], external_id, customer)
    _ensure_logs(c["logs"])
    _ensure_cluster(c["ecs"])
    alb = _ensure_alb(c["elbv2"], vpc_id, subnets, sgs[SG_ALB], origin_value)
    _ensure_alarms(c["cw"], c["sns"], alb, alarm_email)
    # 웹 컨테이너의 서버 측 API 호출도 CloudFront를 거친다 — NAT 공인 IP를 허용 목록에 넣는다
    # 속도 제한에서는 NAT IP를 뺀다(안전 가드 ⑦) — 같은 이유로 그 IP가 모든 사용자의 서버 측 호출을 싣는다
    acl_arn = _ensure_waf(c["waf"], sorted(set(allow_cidrs) | {f"{nat_ip}/32"}), f"{nat_ip}/32")
    _ensure_distribution(c["cf"], alb["dns"], origin_value, acl_arn)
    if custom:
        _ensure_custom_domain(c, domain, *custom)
    print("[platform] 다음 단계(ADR-0010 §7)")
    print(f"  배포 주소          : {' · '.join(public_origins(_find_distribution(c['cf'])))}")
    print("  (도메인을 새로 달거나 바꿨으면 2·3을 다시 — 웹 번들의 API 주소와 api의 CORS가 그 주소를 따른다)")
    if _secret_value(c["sm"], SECRET_OPENAI) == OPENAI_PLACEHOLDER:
        print(f"  1. OpenAI 키 넣기  : aws secretsmanager put-secret-value --secret-id {SECRET_OPENAI} --secret-string <키>")
    print("  2. 이미지          : ... build --account <계정 ID> --tag <태그>")
    customer_args = " --customer-account <고객 계정 ID> --customer-profile <프로필>" if c.get("customer") else ""
    print(f"  3. 배포            : ... deploy --account <계정 ID> --tag <태그>{customer_args} --yes")


# ------------------------------------------------------------------ 이미지(build)
def build(c: dict, region: str, tag: str) -> None:
    repos = {k: _find_repo(c["ecr"], n) for k, n in ECR_REPOS.items()}
    if not all(repos.values()):
        sys.exit("ECR 저장소가 없다 — up을 먼저 돌릴 것")
    dist = _find_distribution(c["cf"])
    if not dist:
        sys.exit("CloudFront 배포가 없다 — 웹 이미지에 박을 API 주소를 정할 수 없다")
    registry = repos["api"].split("/")[0]
    token = c["ecr"].get_authorization_token()["authorizationData"][0]["authorizationToken"]
    user, password = base64.b64decode(token).decode().split(":", 1)
    subprocess.run(["docker", "login", "--username", user, "--password-stdin", registry],
                   input=password.encode(), check=True)
    root = str(_REPO_ROOT)
    builds = [
        (repos["api"], ["docker", "build", "-f", "apps/core-api/Dockerfile", "-t", f"{repos['api']}:{tag}", root]),
        (repos["web"], ["docker", "build", "-f", "apps/web/Dockerfile",
                        "--build-arg", f"NEXT_PUBLIC_API_BASE_URL={public_origins(dist)[0]}",
                        "-t", f"{repos['web']}:{tag}", root]),
    ]
    for repo, cmd in builds:
        subprocess.run(cmd, check=True, cwd=root)
        subprocess.run(["docker", "push", f"{repo}:{tag}"], check=True)
        print(f"[platform] 푸시 {repo}:{tag}")


# ------------------------------------------------------------------ 배포(deploy)
def _register(ecs, family: str, container: dict, *, cpu: str, memory: str, exec_arn: str,
              task_arn: Optional[str]) -> str:
    kwargs = dict(family=family, networkMode="awsvpc", requiresCompatibilities=["FARGATE"], cpu=cpu, memory=memory,
                  executionRoleArn=exec_arn, containerDefinitions=[container],
                  runtimePlatform={"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
                  tags=[{"key": TAG_KEY, "value": TAG_VALUE}])
    if task_arn:
        kwargs["taskRoleArn"] = task_arn
    return ecs.register_task_definition(**kwargs)["taskDefinition"]["taskDefinitionArn"]


def _network(subnets: dict[str, str], sg: str) -> dict:
    return {"awsvpcConfiguration": {"subnets": [subnets[f"{PREFIX}-private-{az}"] for az in PRIVATE_SUBNETS],
                                    "securityGroups": [sg], "assignPublicIp": "DISABLED"}}


def _migrate(ecs, task_def: str, network: dict) -> None:
    task = ecs.run_task(cluster=CLUSTER, taskDefinition=task_def, launchType="FARGATE", networkConfiguration=network,
                        overrides={"containerOverrides": [{"name": "api", "command": [
                            "uv", "run", "--no-sync", "alembic", "upgrade", "head"]}]})["tasks"][0]
    print("[platform] 마이그레이션 태스크 실행 — 종료 대기")
    ecs.get_waiter("tasks_stopped").wait(cluster=CLUSTER, tasks=[task["taskArn"]],
                                         WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    done = ecs.describe_tasks(cluster=CLUSTER, tasks=[task["taskArn"]])["tasks"][0]
    code = done["containers"][0].get("exitCode")
    if code != 0:
        sys.exit(f"마이그레이션 실패 — exitCode {code} · {done.get('stoppedReason')} (로그: {LOG_GROUPS['api']})")
    print("[platform] 마이그레이션 완료")


def _upsert_service(ecs, name: str, task_def: str, network: dict, tg_arn: str, container: str, port: int) -> None:
    svc = _find_service(ecs, name)
    if svc:
        ecs.update_service(cluster=CLUSTER, service=name, taskDefinition=task_def, desiredCount=1,
                           deploymentConfiguration=service_deployment(name), forceNewDeployment=True)
        print(f"[platform] 서비스 {name} 갱신")
        return
    ecs.create_service(cluster=CLUSTER, serviceName=name, taskDefinition=task_def, desiredCount=1,
                       launchType="FARGATE", networkConfiguration=network,
                       loadBalancers=[{"targetGroupArn": tg_arn, "containerName": container, "containerPort": port}],
                       healthCheckGracePeriodSeconds=90, deploymentConfiguration=service_deployment(name),
                       tags=[{"key": TAG_KEY, "value": TAG_VALUE}])
    print(f"[platform] 서비스 {name} 생성")


def deployment_problems(services: list[dict], expected: dict[str, str]) -> list[str]:
    """요청한 task definition으로 배포가 **끝났는지** 본다. 빈 목록이면 성공이다.

    `services_stable` 대기만으로는 모자란다 — 서킷 브레이커가 새 태스크 실패를 보고 이전 버전으로
    롤백해도 서비스는 다시 안정되므로 대기가 통과한다. 그러면 요청한 버전이 돌지 않는데 "배포
    완료"가 찍힌다(PR #441 리뷰). 그래서 PRIMARY 배포의 task definition과 rolloutState를 함께 본다.
    """
    by_name = {svc.get("serviceName"): svc for svc in services}
    problems = []
    for name, task_def in expected.items():
        svc = by_name.get(name)
        if svc is None:
            problems.append(f"{name}: 서비스가 없다")
            continue
        primary = next((d for d in svc.get("deployments", []) if d.get("status") == "PRIMARY"), None)
        if primary is None:
            problems.append(f"{name}: PRIMARY 배포가 없다")
        elif primary.get("taskDefinition") != task_def:
            problems.append(f"{name}: 요청한 {task_def}가 아니라 {primary.get('taskDefinition')}가 돌고 있다"
                            " — 새 태스크가 실패해 자동 롤백됐다")
        elif primary.get("rolloutState") != "COMPLETED":
            problems.append(f"{name}: 배포 상태 {primary.get('rolloutState')}"
                            f" ({primary.get('rolloutStateReason', '사유 없음')})")
    return problems


def deploy(c: dict, account: str, region: str, tag: str, dispatch_enabled: bool,
           notification: Optional[dict] = None) -> None:
    ec2, ecs = c["ec2"], c["ecs"]
    customer = _customer(c, account)
    vpc_id = _find_vpc(ec2)
    smoke_vpc = _find_smoke_vpc(customer["ec2"])
    dist = _find_distribution(c["cf"])
    if not (vpc_id and dist):
        sys.exit("플랫폼 VPC 또는 CloudFront가 없다 — up을 먼저 돌릴 것")
    if not smoke_vpc:
        sys.exit("스모크 VPC가 없다 — 관제 범위(COLLECT_VPC_IDS)를 정할 수 없다"
                 "(계정을 갈랐으면 --customer-account·--customer-profile로 고객 계정을 줄 것)")
    if _secret_value(c["sm"], SECRET_OPENAI) == OPENAI_PLACEHOLDER and dispatch_enabled:
        sys.exit(f"{SECRET_OPENAI}가 아직 자리표시값이다 — AI 분석·조치 실행 디스패치를 켜려면 키를 먼저 넣을 것")
    subnets = _find_subnets(ec2, vpc_id)
    sgs = _find_sgs(ec2, vpc_id)
    repos = {k: _find_repo(c["ecr"], n) for k, n in ECR_REPOS.items()}
    roles = {k: _find_role(c["iam"], n) for k, n in (("exec", ROLE_EXEC), ("task", ROLE_TASK))}
    roles["access"] = _find_role(customer["iam"], ROLE_ACCESS)
    if not all(roles.values()):
        sys.exit(f"IAM 역할이 없다({', '.join(k for k, v in roles.items() if not v)}) — up을 먼저 돌릴 것")
    secret_arns = {
        "DATABASE_URL": _find_secret(c["sm"], SECRET_DATABASE_URL),
        "OPENAI_API_KEY": _find_secret(c["sm"], SECRET_OPENAI),
        "VIGILANTIS_ACCESS_EXTERNAL_ID": _find_secret(c["sm"], SECRET_EXTERNAL_ID),
    }
    container = api_container(
        f"{repos['api']}:{tag}", region=region, cors_origin=",".join(public_origins(dist)),
        access_role_arn=roles["access"], collect_vpc_ids=smoke_vpc, dispatch_enabled=dispatch_enabled,
        secrets_arns=secret_arns, notification=notification)
    if notification:  # deploy는 IAM을 쓰지 않는다 — 정책이 계획과 다르면 등록·마이그레이션 전에 멈춘다
        verify_notification_policies(c["iam"], notification)
    api_def = _register(ecs, API_TASK_FAMILY, container, cpu="512", memory="1024", exec_arn=roles["exec"], task_arn=roles["task"])
    web_def = _register(ecs, WEB_TASK_FAMILY, web_container(f"{repos['web']}:{tag}", region=region),
                        cpu="256", memory="512", exec_arn=roles["exec"], task_arn=None)
    _migrate(ecs, api_def, _network(subnets, sgs[SG_API]))
    _upsert_service(ecs, API_SERVICE, api_def, _network(subnets, sgs[SG_API]), _find_tg(c["elbv2"], TG_API),
                    "api", API_PORT)
    _upsert_service(ecs, WEB_SERVICE, web_def, _network(subnets, sgs[SG_WEB]), _find_tg(c["elbv2"], TG_WEB),
                    "web", WEB_PORT)
    print("[platform] 서비스 안정화 대기")
    ecs.get_waiter("services_stable").wait(cluster=CLUSTER, services=[API_SERVICE, WEB_SERVICE],
                                           WaiterConfig={"Delay": 15, "MaxAttempts": 60})
    problems = deployment_problems(
        ecs.describe_services(cluster=CLUSTER, services=[API_SERVICE, WEB_SERVICE])["services"],
        {API_SERVICE: api_def, WEB_SERVICE: web_def},
    )
    if problems:
        sys.exit("배포 실패 — " + " · ".join(problems) + f" (로그: {LOG_GROUPS['api']})")
    print(f"[platform] 배포 완료 — {public_origins(dist)[0]} (AI 분석·조치 실행 디스패치 {'켬' if dispatch_enabled else '끔'})")
    if not dispatch_enabled:
        print("[platform] 주의: 디스패치가 꺼져 있어 화면에서 승인한 조치도 실행되지 않고 접수 상태로 멈춘다 — "
              "조치 실행을 실측하려면 deploy --enable-dispatch(OpenAI 과금도 함께 켜진다)")


# ------------------------------------------------------------------ 정리(down)
def _try(fn: Callable[[], Any], *ignore: str) -> None:
    try:
        fn()
    except ClientError as exc:
        if _code(exc) not in ignore:
            raise


def _delete_roles(iam, account: str) -> None:
    for role, managed, inline in (
        (ROLE_ACCESS, [_smoke_policy_arn(account)], []),
        (ROLE_TASK, [], ["assume-access-role", NOTIFY_POLICY_TASK]),
        (ROLE_EXEC, ["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"],
         ["read-platform-secrets", NOTIFY_POLICY_EXEC]),
    ):
        found = _get_role(iam, role)
        if not found:
            continue
        if not is_platform_tagged(found.get("Tags", [])):  # 안전 가드 ⑤ — 이름만 같은 남의 역할
            owner = customer_access_owner(found.get("Tags", []))
            if owner:  # 안전 가드 ⑧ — 다른 계정의 플랫폼이 넘겨받은 고객 접근 역할
                print(f"[platform] IAM 역할 {role}은 플랫폼 계정 {owner}이 쓰는 고객 접근 역할이라 건드리지 않았다")
            else:
                print(f"[platform] IAM 역할 {role}은 플랫폼 태그가 없어 건드리지 않았다 — 확인 뒤 직접 정리할 것")
            continue
        for arn in managed:
            _try(lambda arn=arn: iam.detach_role_policy(RoleName=role, PolicyArn=arn), "NoSuchEntity")
        for name in inline:
            _try(lambda name=name: iam.delete_role_policy(RoleName=role, PolicyName=name), "NoSuchEntity")
        iam.delete_role(RoleName=role)
        print(f"[platform] IAM 역할 {role} 삭제")


def ecs_remaining(services: list[dict], tasks: list[dict]) -> list[str]:
    """클러스터를 지우기 전에 끝나 있어야 하는 것 중 아직 남은 것. 빈 목록이면 지워도 된다.

    `services_inactive` 대기는 서비스 **하나만** INACTIVE여도 통과하고, INACTIVE 서비스에도 STOPPING
    태스크가 남을 수 있다. 그 상태에서 delete_cluster가 실패하면 뒤의 RDS·NAT 정리까지 멈춘다(PR #441
    리뷰) — 그래서 서비스 전부와 태스크 전부를 함께 본다. describe_services의 failures(MISSING)는
    이미 지워진 것이라 여기 들어오지 않는다.
    """
    left = [f"서비스 {svc.get('serviceName')}({svc.get('status')})"
            for svc in services if svc.get("status") != "INACTIVE"]
    left += [f"태스크 {task.get('taskArn', '').rsplit('/', 1)[-1]}({task.get('lastStatus')})"
             for task in tasks if task.get("lastStatus") != "STOPPED"]
    return left


def _ecs_drained(ecs, services: list[str], *, delay: float = 15, attempts: int = 40,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    """서비스 전부 INACTIVE + 태스크 전부 STOPPED까지 기다린다. 시간 안에 안 되면 False."""
    left: list[str] = []
    for _ in range(attempts):
        described = ecs.describe_services(cluster=CLUSTER, services=services)["services"] if services else []
        arns = [arn for status in ("RUNNING", "STOPPED")
                for arn in ecs.list_tasks(cluster=CLUSTER, desiredStatus=status)["taskArns"]]
        tasks = ecs.describe_tasks(cluster=CLUSTER, tasks=arns)["tasks"] if arns else []
        left = ecs_remaining(described, tasks)
        if not left:
            return True
        print(f"[platform] ECS 종료 대기 — 남음: {', '.join(left)}")
        sleep(delay)
    print(f"[platform] 경고: ECS 종료 대기 시간 초과 — 남음: {', '.join(left)}")
    return False


def down(c: dict, account: str) -> None:
    ec2, ecs, elbv2 = c["ec2"], c["ecs"], c["elbv2"]
    # 알람이 먼저다 — 서비스를 내리는 동안 down 알람이 울리지 않게 한다
    alarms = [a["AlarmName"] for a in c["cw"].describe_alarms(AlarmNames=list(ALARM_NAMES))["MetricAlarms"]]
    if alarms:  # 없는 이름을 넘기면 ResourceNotFound다
        c["cw"].delete_alarms(AlarmNames=alarms)
        print(f"[platform] 오류 알람 {len(alarms)}건 삭제")
    topic_arn = _find_topic(c["sns"])
    if topic_arn:
        c["sns"].delete_topic(TopicArn=topic_arn)
        print("[platform] 알림 주제 삭제")
    if _find_cluster(ecs):
        deleted = []
        for name in (API_SERVICE, WEB_SERVICE):
            if _find_service(ecs, name):
                ecs.update_service(cluster=CLUSTER, service=name, desiredCount=0)
                ecs.delete_service(cluster=CLUSTER, service=name, force=True)
                deleted.append(name)
                print(f"[platform] 서비스 {name} 삭제")
        if _ecs_drained(ecs, deleted):
            ecs.delete_cluster(cluster=CLUSTER)
        else:
            # 클러스터만 남기고 나머지(RDS·NAT 등 과금 자원)는 계속 정리한다 — 재실행하면 이어서 지운다
            print("[platform] 경고: 클러스터를 지우지 않고 넘어간다 — 정리가 끝나면 down을 다시 돌릴 것")
    for family in (API_TASK_FAMILY, WEB_TASK_FAMILY):
        for arn in ecs.list_task_definitions(familyPrefix=family, status="ACTIVE")["taskDefinitionArns"]:
            ecs.deregister_task_definition(taskDefinition=arn)

    dist = _find_distribution(c["cf"])
    if dist:
        # 배포를 지우면 별칭 레코드가 없는 곳을 가리킨다 — 이 배포를 가리키는 것만 먼저 지운다
        for alias in (dist.get("Aliases") or {}).get("Items") or []:
            zone_id = _find_zone(c["r53"], alias)
            if not zone_id:
                continue
            found = c["r53"].list_resource_record_sets(HostedZoneId=zone_id, StartRecordName=alias, MaxItems="10")
            records = alias_records_for(found["ResourceRecordSets"], alias, dist["DomainName"])
            if records:
                c["r53"].change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={"Changes": [
                    {"Action": "DELETE", "ResourceRecordSet": r} for r in records]})
                print(f"[platform] Route53 {alias} 별칭 레코드 {len(records)}건 삭제")
        cfg = c["cf"].get_distribution_config(Id=dist["Id"])
        if cfg["DistributionConfig"]["Enabled"]:
            cfg["DistributionConfig"]["Enabled"] = False
            c["cf"].update_distribution(Id=dist["Id"], IfMatch=cfg["ETag"], DistributionConfig=cfg["DistributionConfig"])
        print("[platform] CloudFront 비활성화 — 전파 대기(10–15분)")
        c["cf"].get_waiter("distribution_deployed").wait(Id=dist["Id"], WaiterConfig={"Delay": 30, "MaxAttempts": 60})
        etag = c["cf"].get_distribution_config(Id=dist["Id"])["ETag"]
        c["cf"].delete_distribution(Id=dist["Id"], IfMatch=etag)
        print("[platform] CloudFront 삭제")
    _delete_waf(c["waf"])

    lb = _find_lb(elbv2)
    if lb:
        elbv2.delete_load_balancer(LoadBalancerArn=lb["LoadBalancerArn"])
        elbv2.get_waiter("load_balancers_deleted").wait(LoadBalancerArns=[lb["LoadBalancerArn"]])
        print("[platform] ALB 삭제")
    for name in (TG_API, TG_WEB):
        arn = _find_tg(elbv2, name)
        if arn:
            _retry(lambda arn=arn: elbv2.delete_target_group(TargetGroupArn=arn), f"TG {name} 삭제", ("ResourceInUse",))

    if _find_db(c["rds"]):
        c["rds"].delete_db_instance(DBInstanceIdentifier=DB_INSTANCE, SkipFinalSnapshot=True,
                                    DeleteAutomatedBackups=True)
        print("[platform] RDS 삭제 — 완료 대기")
        c["rds"].get_waiter("db_instance_deleted").wait(DBInstanceIdentifier=DB_INSTANCE)
    _try(lambda: c["rds"].delete_db_subnet_group(DBSubnetGroupName=DB_SUBNET_GROUP), "DBSubnetGroupNotFoundFault")

    for name in ECR_REPOS.values():
        _try(lambda name=name: c["ecr"].delete_repository(repositoryName=name, force=True), "RepositoryNotFoundException")
    for name in (SECRET_DATABASE_URL, SECRET_OPENAI, SECRET_ORIGIN, SECRET_EXTERNAL_ID):
        _try(lambda name=name: c["sm"].delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True),
             "ResourceNotFoundException")
    for name in LOG_GROUPS.values():
        _try(lambda name=name: c["logs"].delete_log_group(logGroupName=name), "ResourceNotFoundException")

    _delete_roles(c["iam"], account)
    if c.get("customer"):
        print(f"[platform] 고객 계정 {c['customer']['account']}의 {ROLE_ACCESS}는 지우지 않았다 — 고객 계정의 자원이다"
              "(안전 가드 ⑧). 신뢰 대상인 태스크 역할이 지워져 이 플랫폼은 더 들어가지 못한다")

    vpc_id = _find_vpc(ec2)
    if vpc_id:
        nat = _find_nat(ec2, vpc_id)
        if nat:
            ec2.delete_nat_gateway(NatGatewayId=nat["NatGatewayId"])
            ec2.get_waiter("nat_gateway_deleted").wait(NatGatewayIds=[nat["NatGatewayId"]])
            print("[platform] NAT 삭제")
        for addr in _named(ec2.describe_addresses, "Addresses", f"{PREFIX}-nat"):
            ec2.release_address(AllocationId=addr["AllocationId"])
        for igw in ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])[
                "InternetGateways"]:
            ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
        for rt in ec2.describe_route_tables(Filters=[_vpc_filter(vpc_id)])["RouteTables"]:
            if any(a.get("Main") for a in rt["Associations"]):
                continue
            for a in rt["Associations"]:
                ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"])
            ec2.delete_route_table(RouteTableId=rt["RouteTableId"])
        groups = [g for g in ec2.describe_security_groups(Filters=[_vpc_filter(vpc_id)])["SecurityGroups"]
                  if g["GroupName"] != "default"]
        for g in groups:  # 서로 참조하는 규칙부터 걷어야 지워진다
            if g["IpPermissions"]:
                ec2.revoke_security_group_ingress(GroupId=g["GroupId"], IpPermissions=g["IpPermissions"])
        for g in groups:
            _retry(lambda g=g: ec2.delete_security_group(GroupId=g["GroupId"]), f"SG {g['GroupName']} 삭제",
                   ("DependencyViolation",))
        for s in ec2.describe_subnets(Filters=[_vpc_filter(vpc_id)])["Subnets"]:
            _retry(lambda s=s: ec2.delete_subnet(SubnetId=s["SubnetId"]), "Subnet 삭제", ("DependencyViolation",))
        _retry(lambda: ec2.delete_vpc(VpcId=vpc_id), "VPC 삭제", ("DependencyViolation",))
        print(f"[platform] VPC {vpc_id} 삭제")


# ------------------------------------------------------------------ 진입점
def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Vigilantis 플랫폼 배포 환경 (ADR-0010)")
    parser.add_argument("command", choices=("status", "up", "build", "deploy", "down",
                                            "notification-iam", "notification-revoke"))
    parser.add_argument("--account", required=True, help="대상 AWS 계정 ID — 호출 주체(sts)와 같아야 한다")
    parser.add_argument("--allow-cidr", action="append", default=[],
                        help=f"up: WAF 허용 IPv4 CIDR(/{MIN_ALLOW_PREFIX} 이상 좁게 · 여러 번 줄 수 있다)")
    parser.add_argument("--domain", help="up: CloudFront에 달 사용자 도메인(Route53 호스팅 영역 · "
                                         "us-east-1 ACM 인증서가 먼저 있어야 한다)")
    parser.add_argument("--alarm-email", help="up: 플랫폼 오류 알람을 받을 주소(SNS 메일 구독 — 확인 메일의 링크를 "
                                              "눌러야 알람이 온다)")
    parser.add_argument("--customer-account", help="고객(관제 대상) 계정 ID — 주면 접근 역할을 그 계정에 두고 스모크 "
                                                   "정책·VPC도 거기서 찾는다(T-8). 생략하면 --account 한 계정에 둔다")
    parser.add_argument("--customer-profile", help="--customer-account의 관리자 AWS 프로필")
    parser.add_argument("--tag", help="build·deploy: 이미지 태그")
    parser.add_argument("--enable-dispatch", action="store_true",
                        help="deploy: AI 분석과 조치 실행 디스패치를 함께 켠다(한 스위치 — 끄면 승인한 조치도 "
                             "실행되지 않는다. 켜면 OpenAI 과금 경로)")
    parser.add_argument("--notification-config", help="deploy·notification-iam: 비밀 아닌 알림 배포 입력 JSON "
                        "(enabled:true를 명시해야 켜진다 — 없으면 알림은 꺼진 채 배포된다)")
    parser.add_argument("--yes", action="store_true", help="up·deploy·down·notification-*: 실제로 바꾼다")
    args = parser.parse_args()
    if args.notification_config and args.command not in ("deploy", "notification-iam"):
        sys.exit("--notification-config는 deploy·notification-iam에서만 쓴다")
    if args.command == "notification-iam" and not args.notification_config:
        sys.exit("notification-iam에는 --notification-config가 필요하다")
    if bool(args.customer_account) != bool(args.customer_profile):
        sys.exit("--customer-account와 --customer-profile은 함께 준다")
    if args.customer_account and args.customer_account == args.account:
        sys.exit("--customer-account가 --account와 같다 — 한 계정에 둘 거면 고객 인자를 빼고 돌릴 것")

    account = _require_real_aws(args.account)["Account"]
    region = default_region()
    c = {
        "ec2": aws_client("ec2", region), "elbv2": aws_client("elbv2", region), "rds": aws_client("rds", region),
        "ecs": aws_client("ecs", region), "ecr": aws_client("ecr", region), "iam": aws_client("iam", region),
        "logs": aws_client("logs", region), "sm": aws_client("secretsmanager", region),
        "cw": aws_client("cloudwatch", region), "sns": aws_client("sns", region),
        # CloudFront와 그에 붙는 WAF(CLOUDFRONT 범위)는 us-east-1에서만 다룬다
        "cf": aws_client("cloudfront", "us-east-1"), "waf": aws_client("wafv2", "us-east-1"),
        "acm": aws_client("acm", "us-east-1"), "r53": aws_client("route53", "us-east-1"),
    }
    if args.customer_account:
        c["customer"] = _customer_clients(args.customer_profile, args.customer_account, region)
    if args.command == "status":
        status(c)
        return
    if args.command in ("build", "deploy") and not args.tag:
        sys.exit(f"{args.command}에는 --tag가 필요하다")
    if args.command == "build":
        build(c, region, args.tag)
        return
    if args.command == "notification-revoke":
        if not args.yes:
            print(f"[platform] 회수 예정(다른 정책은 그대로) — {', '.join(f'{r}/{p}' for r, p in NOTIFY_POLICIES)}"
                  " — 실제로 하려면 --yes. 먼저 --notification-config 없이 deploy해 알림을 끈 뒤 회수할 것")
            return
        revoke_notification_policies(c["iam"], c["ecs"])
        return
    notification = None
    if args.notification_config:  # 무엇이든 바꾸기 전에 — 입력·비밀 존재·역할 태그를 읽기만 하며 본다
        notification = notification_plan(c, account, region, args.notification_config)
        if args.command == "notification-iam" and notification is None:
            sys.exit("enabled가 false라 적용할 정책이 없다")
        print(f"[platform] 알림 {'켬' if notification else '끔'}"
              + (f" — 토픽 {len(notification['topic_arns'])}건 · Slack 비밀 {len(notification['secret_arns'])}건"
                 if notification else ""))
    if args.command == "up":
        if not args.allow_cidr:
            sys.exit("up에는 --allow-cidr가 필요하다 — 허용 IP 없이 공개 진입점을 세우지 않는다")
        allow_cidrs = validate_allow_cidrs(args.allow_cidr)
        # 미리 보기에서도 먼저 본다 — --yes로 돌렸을 때 중간에 멈출 일을 여기서 알린다
        customer = _customer(c, account)
        _require_smoke_env(customer["iam"], customer["ec2"], customer["account"])
        if args.domain:
            _require_custom_domain(c, args.domain)
    if not args.yes:
        if args.command == "up":
            missing = [row for row in inventory(c) if row[2] is None and row[0] != "EcsService"]
            print(f"[platform] 생성 예정 {len(missing)}건 — 실제로 만들려면 --yes")
            _print_rows(missing)
        elif args.command == "notification-iam":
            print(f"[platform] 적용 예정 — 인라인 정책 {NOTIFY_POLICY_TASK}(토픽 {notification['topic_arns']}) · "
                  f"{NOTIFY_POLICY_EXEC}(비밀 {len(notification['secret_arns'])}건) — 실제로 하려면 --yes")
        elif args.command == "deploy":
            if notification:
                verify_notification_policies(c["iam"], notification)
            print(f"[platform] 배포 예정 — 태그 {args.tag} · AI 분석·조치 실행 디스패치 "
                  f"{'켬' if args.enable_dispatch else '끔'} — 실제로 하려면 --yes")
        else:
            print("[platform] 삭제 대상(플랫폼 태그 자원 + 플랫폼 VPC 안 전부) — 실제로 지우려면 --yes")
            _print_rows([row for row in inventory(c) if row[2]])
        return
    if args.command == "up":
        up(c, account, region, allow_cidrs, args.domain, args.alarm_email)
    elif args.command == "notification-iam":
        apply_notification_policies(c["iam"], c["ecs"], notification)
    elif args.command == "deploy":
        deploy(c, account, region, args.tag, args.enable_dispatch, notification)
    else:
        down(c, account)


if __name__ == "__main__":
    main()
