# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# Boto3 클라이언트 팩토리 — 리전·엔드포인트·자격증명 해석의 단일 원천입니다.
# (Issue #128, ADR-0006 §3)
#
# 소비자: services/collector.py(수집) · services/aws/executor.py(실행·precheck) ·
#         scripts/seed_localstack.py(시드) · scripts/probe_dryrun.py(#130)
#
# ADR-0006 §3(전환 스위치 규약)은 "AWS_ENDPOINT_URL 유무로만 전환하고 코드 분기를
# 두지 않는다"이다. 그 조항이 지켜지려면 값을 해석하는 자리가 하나여야 한다 —
# 같은 조건이 소비자마다 복사되면 조항이 있어도 어긋난 곳을 찾을 수 없다.
# **엔드포인트 유무를 보고 동작을 바꾸는 코드는 이 파일 밖에 두지 않는다.**
# 리포트용 표기가 필요하면 deployment_mode()를 쓴다.
#
# botocore 자신도 AWS_ENDPOINT_URL을 전역 엔드포인트 설정으로 읽는다 — 팩토리를
# 거치지 않고 만든 클라이언트도 같은 스위치를 따른다는 뜻이라 §3과 어긋나지 않는다.
# 다만 반대 방향은 성립하지 않는다. botocore는 서비스별 환경변수
# (AWS_ENDPOINT_URL_EC2 등)와 ~/.aws/config의 endpoint_url·services 섹션도 엔드포인트
# 설정으로 읽지만 AwsSettings는 이들을 읽지 않는다. 스위치를 껐는데(= 실 AWS) 그런
# 설정이 남아 있으면 요청이 조용히 테스트 서버로 흘러간다 — 실 AWS 모드에서는
# 무시하게 한다(_IGNORE_CONFIGURED_ENDPOINTS).
#
# 이 모듈은 DB 설정에 의존하지 않는다(config.AwsSettings). 시드·실측 스크립트가
# DATABASE_URL 없이 도는 것이 그 이유다.
#
# 세션은 **스레드마다 하나**다(Issue #434 2-1). 수집·실행 디스패처·AI 디스패처는 각자의
# 스케줄러가 동기 잡을 이벤트 루프의 기본 스레드 풀에서 돌려 동시에 클라이언트를 만들고,
# boto3 문서는 기본 세션(boto3.client)을 여러 스레드에서 함께 쓰지 말라고 한다 — 세션은
# 스레드 안전하지 않고 클라이언트만 안전하다. 그래서 클라이언트는 호출마다 만들되 그
# 재료인 세션은 스레드별로 한 번만 만든다.
#
# 재시도 정책(같은 2-1). 조회와 변경을 한 정책으로 둔다 — standard 모드, 최대 5회 시도.
#   - adaptive가 아닌 이유: adaptive의 속도 조절 상태는 **클라이언트 인스턴스** 안에 산다.
#     클라이언트를 호출마다 만드는 이 구조에서는 함수 하나 안에서만 유효해 사실상 standard와
#     같고, AWS SDK 문서도 여러 자원을 다루는 클라이언트에 adaptive를 권하지 않는다.
#   - 변경 호출의 재전송을 끄지 않는 이유: botocore는 연결 오류·읽기 제한 시간·5xx에 요청을
#     다시 보내며 멱등성을 보지 않는다. 첫 요청이 적용된 뒤 응답이 유실되면 재전송이 받는
#     4xx(NotFound·AlreadyExists)가 마지막 오류가 된다. 그 가능성은 전송을 막는 대신 **결과
#     분류**가 흡수한다 — 재시도가 있었던 오류는 "변경 없음"으로 적지 않고(executor._effect_for)
#     실자산을 보는 종료 판정으로 보낸다. 재전송을 끄면 스로틀링 한 번이 조치 실패가 된다.
# ==============================================================================

from __future__ import annotations

import os
import threading
from typing import Any

import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import ContainerProvider, CredentialResolver

from config import get_aws_settings

# 스로틀링(RequestLimitExceeded)·일시 오류 대비 — 첫 요청 포함 최대 5회 시도(파일 헤더).
_BOTO_CONFIG = Config(
    retries={"total_max_attempts": 5, "mode": "standard"},
    user_agent_extra="vigilantis/0.1",
)

# 스레드별 boto3 세션(파일 헤더). 기본 세션을 쓰지 않는다.
_thread_local = threading.local()


def _session() -> boto3.session.Session:
    """이 스레드의 boto3 세션 — 없으면 만든다. 세션은 스레드 간에 공유하지 않는다."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = boto3.session.Session()
        _thread_local.session = session
    return session

# 실 AWS 모드 전용. 스위치가 꺼졌는데도 남아 있는 다른 엔드포인트 설정(서비스별
# 환경변수·~/.aws/config)을 botocore가 집지 못하게 한다. LocalStack 모드에서는
# 붙이지 않는다 — 그쪽은 endpoint_url을 명시로 넘기므로 무시할 대상이 없다.
_IGNORE_CONFIGURED_ENDPOINTS = Config(ignore_configured_endpoint_urls=True)


def endpoint_url() -> str | None:
    """LocalStack 엔드포인트. 실 AWS면 None."""
    return get_aws_settings().endpoint_url()


def deployment_mode() -> str:
    """리포트·저장 표기용 문자열. 동작 분기에는 쓰지 않는다(ADR-0006 §3)."""
    return "localstack" if endpoint_url() else "aws"


def regions() -> list[str]:
    """수집·실행 대상 리전 목록. AWS_REGIONS(콤마) 우선, 없으면 AWS_REGION."""
    return get_aws_settings().regions_list()


def default_region() -> str:
    """리전을 지정하지 않은 호출이 쓰는 리전 — 목록의 첫 값."""
    resolved = regions()
    if not resolved:
        raise RuntimeError("리전 해석 실패 — AWS_REGION / AWS_REGIONS 값을 확인할 것")
    return resolved[0]


def _ensure_dummy_credentials(endpoint: str | None) -> None:
    """LocalStack은 자격증명을 검사하지 않지만 boto3는 키가 아예 없으면 예외를 낸다.
    실 AWS(엔드포인트 없음)에서는 절대 더미 키를 넣지 않는다."""
    if not endpoint or os.getenv("AWS_ACCESS_KEY_ID"):
        return
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")


def aws_client(service: str, region: str | None = None, **config_overrides: Any):
    """Boto3 클라이언트 1개. 재시도 설정은 공통이고, 필요한 경우만 덧붙인다.

    config_overrides는 공통 Config 위에 병합된다(재시도 설정은 유지된다).
    예: aws_client("cloudwatch", region, disable_request_compression=True)
    """
    endpoint = endpoint_url()
    _ensure_dummy_credentials(endpoint)
    return _session().client(
        service,
        **_client_kwargs(_BOTO_CONFIG, endpoint, region or default_region(), config_overrides),
    )


def _client_kwargs(
    base: Config, endpoint: str | None, region: str, config_overrides: dict[str, Any]
) -> dict[str, Any]:
    """region·endpoint·Config 조립의 단일 지점 — 일반/플랫폼 클라이언트가 공유한다."""
    config = base if endpoint else base.merge(_IGNORE_CONFIGURED_ENDPOINTS)
    if config_overrides:
        config = config.merge(Config(**config_overrides))
    kwargs: dict[str, Any] = {"region_name": region, "config": config}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return kwargs


def _ecs_only_session(provider: ContainerProvider | None = None) -> botocore.session.Session:
    """고객 프로파일·정적 키·공유 자격증명 파일을 보지 않는 비공개 botocore 세션.

    자격증명 해석기는 ContainerProvider 하나뿐이다(ECS 태스크 역할, 실패 시 닫힘).
    provider 주입은 테스트용(가짜 메타데이터 fetcher). 전역 환경은 수정하지 않는다."""
    session = botocore.session.Session()
    # AWS_PROFILE을 이기려면 인스턴스 프로파일을 명시해야 하고, 그 프로파일은 맵에 있어야
    # 한다 — 빈 default 프로파일만 가진 맵을 직접 심는다(botocore 비공개 속성, 테스트가 고정).
    session.set_config_variable("profile", "default")
    session._profile_map = {"default": {}}
    session.set_config_variable("config_file", os.devnull)
    session.set_config_variable("credentials_file", os.devnull)
    session.register_component(
        "credential_provider",
        CredentialResolver([provider or ContainerProvider()]),
    )
    return session


def platform_sns_client(
    region: str,
    *,
    connect_timeout: float = 3.0,
    read_timeout: float = 5.0,
    _provider: ContainerProvider | None = None,
):
    """플랫폼(ECS 태스크 역할) 자격증명으로 만든 SNS 클라이언트. SDK 재시도 없음(총 시도 1번)."""
    # SDK 로그 보호는 앱 setup_logging의 WARNING 설정이 소유한다.
    # 이 팩토리는 다른 AWS 호출의 경고·오류를 끄거나 로거 수준을 바꾸지 않는다.
    config = Config(
        retries={"total_max_attempts": 1, "mode": "standard"},
        user_agent_extra="vigilantis/0.1",
    )
    kwargs = _client_kwargs(
        config,
        endpoint_url(),
        region,
        {"connect_timeout": connect_timeout, "read_timeout": read_timeout},
    )
    return _ecs_only_session(_provider).create_client("sns", **kwargs)


def account_id(region: str | None = None) -> str:
    """호출 주체의 계정 ID. ARN 조립에 쓰인다(가드레일 ③이 그 문자열을 대조한다)."""
    return aws_client("sts", region).get_caller_identity()["Account"]
