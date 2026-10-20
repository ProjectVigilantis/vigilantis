"""플랫폼 SNS 자격증명 팩토리 테스트 (#493, ADR-0006 §3).

실 메타데이터·네트워크·AWS 호출 없음. 가짜 fetcher + 실제 ContainerProvider/갱신 경로를
쓰고, 오염된 프로파일/키는 임시 가짜 값만 쓴다.
"""

import copy
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import botocore.credentials
import pytest
from botocore.awsrequest import AWSResponse
from botocore.credentials import ContainerProvider
from botocore.exceptions import ClientError, NoCredentialsError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import AwsSettings  # noqa: E402
from services.aws import client as client_module  # noqa: E402

LOCALSTACK = "http://localhost:4566"
RELATIVE_URI = "/v2/credentials/fake"


class FakeFetcher:
    """ContainerMetadataFetcher 대역. 호출 때마다 다음 자격증명을 돌려준다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def full_url(self, relative_uri):
        return "http://169.254.170.2" + relative_uri

    def retrieve_full_uri(self, full_uri, headers=None):
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


def _resp(key, secret, minutes):
    expiry = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return {
        "AccessKeyId": key,
        "SecretAccessKey": secret,
        "Token": "tok-" + key,
        "Expiration": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@pytest.fixture
def ecs_env(monkeypatch):
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", RELATIVE_URI)
    for name in ("AWS_ENDPOINT_URL", "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(monkeypatch):
    def _apply(**values):
        resolved = AwsSettings(_env_file=None, **values)
        monkeypatch.setattr(client_module, "get_aws_settings", lambda: resolved)

    return _apply


@pytest.fixture
def real_provider(monkeypatch):
    """운영 경로(_provider 미주입)가 만드는 실제 ContainerProvider에 가짜 fetcher만 꽂는다."""

    def _install(fetcher):
        monkeypatch.setattr(botocore.credentials, "ContainerMetadataFetcher", lambda *a, **k: fetcher)

    return _install


def _poison(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cred = tmp_path / "credentials"
    cfg.write_text("[profile customer]\nregion = eu-west-1\nendpoint_url = http://evil.invalid\n")
    cred.write_text("[customer]\naws_access_key_id = FAKEPROFILEKEY\naws_secret_access_key = fake\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred))
    monkeypatch.setenv("AWS_PROFILE", "customer")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKEENVKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-env-secret")


def test_returns_sns_client_with_one_sdk_attempt_and_timeouts(ecs_env, settings, real_provider):
    settings()
    real_provider(FakeFetcher([_resp("ECSKEY", "s", 60)]))
    client = client_module.platform_sns_client("ap-northeast-2", connect_timeout=1.5, read_timeout=2.5)
    assert client.meta.service_model.service_name == "sns"
    assert client.meta.region_name == "ap-northeast-2"
    assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert client.meta.config.connect_timeout == 1.5
    assert client.meta.config.read_timeout == 2.5


def test_defaults_timeouts(ecs_env, settings, real_provider):
    settings()
    real_provider(FakeFetcher([_resp("ECSKEY", "s", 60)]))
    config = client_module.platform_sns_client("us-east-1").meta.config
    assert (config.connect_timeout, config.read_timeout) == (3.0, 5.0)


def test_exactly_one_attempt_on_server_error(ecs_env, settings, real_provider):
    settings()
    real_provider(FakeFetcher([_resp("ECSKEY", "s", 60)]))
    client = client_module.platform_sns_client("us-east-1")
    sent = []

    class Raw:
        def stream(self, *a, **k):
            yield b"<ErrorResponse><Error><Code>InternalError</Code></Error></ErrorResponse>"

    def before_send(request, **kwargs):
        sent.append(request)
        return AWSResponse(request.url, 500, {}, Raw())

    client.meta.events.register("before-send.sns.Publish", before_send)
    with pytest.raises(ClientError):
        client.publish(TopicArn="arn:aws:sns:us-east-1:123456789012:t", Message="m")
    assert len(sent) == 1


def test_uses_container_credentials_ignoring_customer_profile_and_keys(
    ecs_env, settings, real_provider, monkeypatch, tmp_path
):
    settings()
    _poison(monkeypatch, tmp_path)
    real_provider(FakeFetcher([_resp("ECSKEY", "ecs-secret", 60)]))
    client = client_module.platform_sns_client("us-east-1")
    frozen = client._request_signer._credentials.get_frozen_credentials()
    assert frozen.access_key == "ECSKEY"
    assert frozen.secret_key == "ecs-secret"
    assert client._request_signer._credentials.method == "container-role"
    # 오염된 설정의 endpoint_url / region도 따라가지 않는다.
    assert "evil.invalid" not in client.meta.endpoint_url
    assert client.meta.region_name == "us-east-1"


def test_credentials_are_refreshable_not_frozen_copies(ecs_env, settings, real_provider):
    settings()
    fetcher = FakeFetcher([_resp("KEY1", "s1", 1), _resp("KEY2", "s2", 60)])
    real_provider(fetcher)
    client = client_module.platform_sns_client("us-east-1")
    creds = client._request_signer._credentials
    assert isinstance(creds, botocore.credentials.RefreshableCredentials)
    assert fetcher.calls == 1
    assert creds.get_frozen_credentials().access_key == "KEY2"
    assert fetcher.calls == 2


def test_fails_closed_without_ecs_credentials(settings, monkeypatch, tmp_path):
    settings()
    _poison(monkeypatch, tmp_path)
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", raising=False)
    with pytest.raises(NoCredentialsError):
        client_module.platform_sns_client("us-east-1").publish(
            TopicArn="arn:aws:sns:us-east-1:123456789012:t", Message="m"
        )


def test_environment_is_not_mutated(ecs_env, settings, real_provider, monkeypatch, tmp_path):
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    _poison(monkeypatch, tmp_path)
    real_provider(FakeFetcher([_resp("ECSKEY", "s", 60)]))
    before = copy.deepcopy(dict(os.environ))
    client_module.platform_sns_client("us-east-1")
    assert dict(os.environ) == before


def test_localstack_endpoint_with_injected_provider(ecs_env, settings):
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    fetcher = FakeFetcher([_resp("LSKEY", "s", 60)])
    provider = ContainerProvider(fetcher=fetcher)
    client = client_module.platform_sns_client("us-east-1", _provider=provider)
    assert client.meta.endpoint_url == LOCALSTACK


def test_real_aws_ignores_configured_endpoints(ecs_env, settings, real_provider, monkeypatch):
    settings()
    monkeypatch.setenv("AWS_ENDPOINT_URL_SNS", "http://evil.invalid")
    real_provider(FakeFetcher([_resp("ECSKEY", "s", 60)]))
    client = client_module.platform_sns_client("us-east-1")
    assert client.meta.endpoint_url == "https://sns.us-east-1.amazonaws.com"


@pytest.fixture
def sdk_logger_state():
    names = client_module._SDK_LOGGERS
    saved = {n: logging.getLogger(n).level for n in names}
    root = logging.getLogger()
    saved_root = root.level
    root.setLevel(logging.DEBUG)  # 앱 LOG_LEVEL=DEBUG 재현
    for n in names:
        logging.getLogger(n).setLevel(logging.DEBUG)  # SDK 로거가 열려 있던 상태에서 시작
    yield
    root.setLevel(saved_root)
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


def test_debug_logging_does_not_leak_body_token_or_provider_error(
    ecs_env, settings, real_provider, sdk_logger_state, caplog
):
    from botocore.exceptions import MetadataRetrievalError

    settings()
    body_sentinel, token_sentinel, meta_sentinel = "BODY-SENTINEL", "TOKEN-SENTINEL", "META-ERR-SENTINEL"
    caplog.set_level(logging.DEBUG)

    # 1) 발송 경로: 본문과 보안 토큰이 요청 단계 DEBUG 로그에 남으면 안 된다.
    ok = FakeFetcher([_resp(token_sentinel, "s", 60)])
    real_provider(ok)
    client = client_module.platform_sns_client("us-east-1")

    class Raw:
        def stream(self, *a, **k):
            yield b"<ErrorResponse><Error><Code>InternalError</Code></Error></ErrorResponse>"

    client.meta.events.register(
        "before-send.sns.Publish", lambda request, **kw: AWSResponse(request.url, 500, {}, Raw())
    )
    with pytest.raises(ClientError):
        client.publish(TopicArn="arn:aws:sns:us-east-1:123456789012:t", Message=body_sentinel)

    # 2) 자격증명 갱신 실패: 원본 오류 문자열이 WARNING/traceback으로 남으면 안 된다.
    class Broken(FakeFetcher):
        def retrieve_full_uri(self, full_uri, headers=None):
            raise MetadataRetrievalError(error_msg=meta_sentinel)

    real_provider(Broken([]))
    with pytest.raises(Exception):
        broken = client_module.platform_sns_client("us-east-1")
        broken.publish(TopicArn="arn:aws:sns:us-east-1:123456789012:t", Message="m")

    # 앱 로거의 안전한 이벤트는 여전히 기록된다.
    logging.getLogger("vigilantis.notifications").warning("sns_publish_failed reason=%s", "transport_error")

    text = caplog.text
    for sentinel in (body_sentinel, token_sentinel, meta_sentinel):
        assert sentinel not in text
    assert "sns_publish_failed reason=transport_error" in text
    assert not [r for r in caplog.records if r.name.split(".")[0] in ("botocore", "urllib3")]


def test_normal_aws_client_keeps_default_path(settings, monkeypatch):
    settings(AWS_ENDPOINT_URL=LOCALSTACK)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "x")  # 복원 지점 기록 후 제거
        monkeypatch.delenv(name)
    client = client_module.aws_client("sns", "us-east-1")
    assert client.meta.endpoint_url == LOCALSTACK
    assert client.meta.config.retries["mode"] == "adaptive"
    assert os.environ["AWS_ACCESS_KEY_ID"] == "test"  # 기존 동작 유지
