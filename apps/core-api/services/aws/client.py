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
#
# 이 모듈은 DB 설정에 의존하지 않는다(config.AwsSettings). 시드·실측 스크립트가
# DATABASE_URL 없이 도는 것이 그 이유다.
# ==============================================================================

from __future__ import annotations

import os
from typing import Any

import boto3
from botocore.config import Config

from config import get_aws_settings

# 스로틀링(RequestLimitExceeded) 대비. adaptive 모드가 자동으로 속도를 낮춘다.
_BOTO_CONFIG = Config(
    retries={"max_attempts": 5, "mode": "adaptive"},
    user_agent_extra="vigilantis/0.1",
)


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

    config = _BOTO_CONFIG.merge(Config(**config_overrides)) if config_overrides else _BOTO_CONFIG
    kwargs: dict[str, Any] = {
        "region_name": region or default_region(),
        "config": config,
    }
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client(service, **kwargs)


def account_id(region: str | None = None) -> str:
    """호출 주체의 계정 ID. ARN 조립에 쓰인다(가드레일 ③이 그 문자열을 대조한다)."""
    return aws_client("sts", region).get_caller_identity()["Account"]
