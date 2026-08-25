"""Boto3 클라이언트 팩토리 단위 테스트 (Issue #128, ADR-0006 §3).

AWS/DB 불필요 — 클라이언트 생성 시점의 해석만 검증한다(호출은 하지 않는다).
설정은 .env 파일 영향을 받지 않도록 AwsSettings를 직접 구성해 주입한다.
"""

import os
import sys
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import AwsSettings  # noqa: E402
from services.aws import client as aws_client_module  # noqa: E402

LOCALSTACK = "http://localhost:4566"


@pytest.fixture
def settings(monkeypatch):
    """AwsSettings를 테스트가 지정한 값으로 갈아 끼운다."""

    def _apply(**values):
        # botocore도 AWS_ENDPOINT_URL을 자체적으로 읽는다 — 프로세스 환경을 비워
        # 팩토리가 넘긴 값만 반영되게 한다.
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        resolved = AwsSettings(_env_file=None, **values)
        monkeypatch.setattr(aws_client_module, "get_aws_settings", lambda: resolved)
        return resolved

    return _apply


# --- 설정 해석 -----------------------------------------------------------------


def test_regions_splits_comma_list_and_trims():
    resolved = AwsSettings(_env_file=None, AWS_REGIONS=" ap-northeast-2 , us-east-1 ")
    assert resolved.regions_list() == ["ap-northeast-2", "us-east-1"]


def test_regions_falls_back_to_single_region():
    resolved = AwsSettings(_env_file=None, AWS_REGION="eu-west-1", AWS_REGIONS="")
    assert resolved.regions_list() == ["eu-west-1"]


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_endpoint_means_real_aws(raw):
    assert AwsSettings(_env_file=None, AWS_ENDPOINT_URL=raw).endpoint_url() is None


def test_default_region_fails_loudly_when_unresolvable(settings):
    settings(AWS_REGION="", AWS_REGIONS="")
    with pytest.raises(RuntimeError, match="리전 해석 실패"):
        aws_client_module.default_region()


# --- 전환 스위치 (ADR-0006 §3) --------------------------------------------------


def test_endpoint_present_targets_localstack(settings):
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    assert aws_client_module.deployment_mode() == "localstack"
    client = aws_client_module.aws_client("ec2")
    assert client.meta.endpoint_url == LOCALSTACK


def test_endpoint_absent_targets_real_aws(settings):
    settings(AWS_ENDPOINT_URL="", AWS_REGION="ap-northeast-2")
    assert aws_client_module.deployment_mode() == "aws"
    client = aws_client_module.aws_client("ec2")
    assert client.meta.endpoint_url.endswith("amazonaws.com")


# --- 클라이언트 구성 ------------------------------------------------------------


def test_common_retry_config_is_applied(settings):
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    config = aws_client_module.aws_client("ec2").meta.config
    assert config.retries["mode"] == "adaptive"
    # botocore가 max_attempts(재시도 횟수)를 총 시도 횟수로 정규화한다 — 5 + 최초 1회
    assert config.retries["total_max_attempts"] == 6


def test_overrides_merge_without_dropping_retry_config(settings):
    """시드 스크립트의 cloudwatch 압축 해제처럼 한 축만 덧붙이는 경우."""
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    config = aws_client_module.aws_client(
        "cloudwatch", disable_request_compression=True
    ).meta.config
    assert config.disable_request_compression is True
    assert config.retries["mode"] == "adaptive"


def test_region_argument_overrides_default(settings):
    settings(AWS_REGIONS="ap-northeast-2,us-east-1", AWS_ENDPOINT_URL=LOCALSTACK)
    assert aws_client_module.aws_client("ec2").meta.region_name == "ap-northeast-2"
    assert aws_client_module.aws_client("ec2", "us-east-1").meta.region_name == "us-east-1"


# --- 자격증명 -------------------------------------------------------------------


def test_dummy_credentials_only_for_localstack(settings, monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    settings(AWS_ENDPOINT_URL="", AWS_REGION="ap-northeast-2")
    aws_client_module.aws_client("ec2")
    assert "AWS_ACCESS_KEY_ID" not in os.environ, "실 AWS에 더미 자격증명을 넣으면 안 됩니다"

    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    aws_client_module.aws_client("ec2")
    assert os.environ["AWS_ACCESS_KEY_ID"] == "test"


def test_existing_credentials_are_never_overwritten(settings, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAREAL")
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    aws_client_module.aws_client("ec2")
    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIAREAL"
