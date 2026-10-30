"""T-7 설정 자원 어댑터 — botocore Stubber(실제 boto3 직렬화 · 파라미터 검증)와 가짜 자격 fetcher.

네트워크 · 실제 AWS 호출 없음. 자원 이름 · 호출 파라미터 · 오류 분류 · 원문 비노출 · 플랫폼 전용 자격을 본다.
"""

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import botocore.credentials
import pytest
from botocore.exceptions import ConnectTimeoutError, NoCredentialsError
from botocore.stub import ANY, Stubber

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import AwsSettings  # noqa: E402
from schemas.settings_resources import (  # noqa: E402
    SETTINGS_SECRET_PREFIX,
    SETTINGS_SNS_TOPIC_PREFIX,
)
from services.aws import client as client_module  # noqa: E402
from services.aws import settings_notifications as sn  # noqa: E402

REGION = "ap-northeast-2"
ACCOUNT = "999988887777"
TENANT = "11111111-1111-4111-8111-111111111111"
GENERATION = "22222222-2222-4222-8222-222222222222"
WEBHOOK = "https://hooks.slack.com/services/TPRIV/BPRIV/PrivateSlackToken"
ADDRESS = "ops@example.com"
RAW = "raw provider text with arn:aws:secretsmanager:secret-detail"


def _secret_arn(name: str) -> str:
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbC123"


def _topic_arn(name: str) -> str:
    return f"arn:aws:sns:{REGION}:{ACCOUNT}:{name}"


class Stubs:
    """client_factory 대역 — 서비스별 실제 boto3 클라이언트 + Stubber. 만든 횟수를 센다."""

    def __init__(self):
        self.created = []
        self.stubbers = {}
        self.clients = {}
        for service in ("secretsmanager", "sns"):
            client = boto3.session.Session().client(
                service, region_name=REGION,
                aws_access_key_id="testing", aws_secret_access_key="testing",
            )
            stubber = Stubber(client)
            stubber.activate()
            self.clients[service], self.stubbers[service] = client, stubber

    def __call__(self, service, region, *, connect_timeout, read_timeout):
        self.created.append((service, region, connect_timeout, read_timeout))
        return self.clients[service]

    def verify(self):
        for stubber in self.stubbers.values():
            stubber.assert_no_pending_responses()


@pytest.fixture()
def stubs():
    s = Stubs()
    yield s
    s.verify()


@pytest.fixture()
def resources(stubs):
    return sn.SettingsResources(stubs, connect_timeout=1.5, read_timeout=2.5)


def test_names_use_shared_prefixes_and_are_deterministic():
    staging = str(uuid.uuid4())
    name = sn.secret_name(TENANT, staging)
    assert name == f"{SETTINGS_SECRET_PREFIX}{TENANT}/slack-{uuid.UUID(staging).hex}"
    topic = sn.topic_name(TENANT.upper(), GENERATION)
    assert topic == f"{SETTINGS_SNS_TOPIC_PREFIX}{uuid.UUID(TENANT).hex}-{uuid.UUID(GENERATION).hex}"
    assert len(topic) <= 256
    assert sn.secret_owner(_secret_arn(name)) == (REGION, TENANT)
    assert sn.secret_owner(_secret_arn("other/slack")) is None
    assert sn.topic_matches(_topic_arn(topic), TENANT, GENERATION) == REGION
    assert sn.topic_matches(_topic_arn(topic), TENANT, str(uuid.uuid4())) is None
    assert sn.topic_matches(_topic_arn("customer-a"), TENANT, GENERATION) is None


def test_clients_are_lazy_and_reused(stubs, resources):
    assert stubs.created == []
    topic = sn.topic_name(TENANT, GENERATION)
    for _ in range(2):
        stubs.stubbers["sns"].add_response(
            "create_topic", {"TopicArn": _topic_arn(topic)}, {"Name": topic},
        )
        resources.create_topic(region=REGION, name=topic)
    assert stubs.created == [("sns", REGION, 1.5, 2.5)]


def test_secret_is_created_new_with_value_and_returns_immutable_reference(stubs, resources):
    staging = str(uuid.uuid4())
    name = sn.secret_name(TENANT, staging)
    version = str(uuid.uuid4())
    stubs.stubbers["secretsmanager"].add_response(
        "create_secret",
        {"ARN": _secret_arn(name), "Name": name, "VersionId": version},
        {"Name": name, "SecretString": WEBHOOK, "ClientRequestToken": staging, "Description": ANY},
    )
    ref = resources.create_slack_secret(region=REGION, name=name, url=WEBHOOK, request_token=staging)
    assert (ref.arn, ref.version_id) == (_secret_arn(name), version)
    assert ACCOUNT not in repr(ref) and name not in repr(ref)


def test_secret_creation_refuses_unowned_names_and_bad_responses(stubs, resources):
    with pytest.raises(sn.SettingsProviderError) as refused:
        resources.create_slack_secret(region=REGION, name="prod/slack", url=WEBHOOK, request_token="t" * 32)
    assert refused.value.code == "secretsmanager_name_not_owned"
    name = sn.secret_name(TENANT, str(uuid.uuid4()))
    stubs.stubbers["secretsmanager"].add_response(
        "create_secret", {"ARN": _secret_arn("elsewhere/x"), "VersionId": str(uuid.uuid4())},
    )
    with pytest.raises(sn.SettingsProviderError) as invalid:
        resources.create_slack_secret(region=REGION, name=name, url=WEBHOOK, request_token="t" * 32)
    assert (invalid.value.category, invalid.value.code) == ("UNKNOWN", "secretsmanager_invalid_response")


def test_secret_value_is_read_by_exact_version_and_only_for_owned_references(stubs, resources):
    arn = _secret_arn(sn.secret_name(TENANT, str(uuid.uuid4())))
    version = str(uuid.uuid4())
    stubs.stubbers["secretsmanager"].add_response(
        "get_secret_value", {"SecretString": WEBHOOK}, {"SecretId": arn, "VersionId": version},
    )
    assert resources.read_slack_secret(arn, version) == WEBHOOK
    with pytest.raises(sn.SettingsProviderError) as refused:
        resources.read_slack_secret(_secret_arn("legacy/prod-webhook"), version)
    assert refused.value.code == "secretsmanager_ref_not_owned"


def test_missing_secret_delete_is_done_and_other_errors_carry_only_codes(stubs, resources):
    name = sn.secret_name(TENANT, str(uuid.uuid4()))
    sm = stubs.stubbers["secretsmanager"]
    sm.add_client_error("delete_secret", "ResourceNotFoundException", RAW, expected_params={
        "SecretId": name, "ForceDeleteWithoutRecovery": True,
    })
    resources.delete_secret(region=REGION, name=name)
    sm.add_client_error("delete_secret", "AccessDeniedException", RAW)
    with pytest.raises(sn.SettingsProviderError) as denied:
        resources.delete_secret(region=REGION, name=name)
    assert (denied.value.category, denied.value.code) == (
        "PERMANENT", "secretsmanager_AccessDeniedException",
    )
    assert RAW not in str(denied.value) and denied.value.__cause__ is None
    with pytest.raises(sn.SettingsProviderError) as foreign:
        resources.delete_secret(region=REGION, name="prod/slack-webhook")
    assert foreign.value.code == "secretsmanager_name_not_owned"


def test_email_subscription_uses_real_subscription_arn_and_email_protocol(stubs, resources):
    topic = _topic_arn(sn.topic_name(TENANT, GENERATION))
    subscription = f"{topic}:{uuid.uuid4()}"
    stubs.stubbers["sns"].add_response(
        "subscribe", {"SubscriptionArn": subscription},
        {"TopicArn": topic, "Protocol": "email", "Endpoint": ADDRESS, "ReturnSubscriptionArn": True},
    )
    assert resources.subscribe_email(topic_arn=topic, address=ADDRESS) == subscription
    stubs.stubbers["sns"].add_response("subscribe", {"SubscriptionArn": "pending confirmation"})
    with pytest.raises(sn.SettingsProviderError) as invalid:
        resources.subscribe_email(topic_arn=topic, address=ADDRESS)
    assert invalid.value.code == "sns_invalid_response"


TOPIC = _topic_arn(sn.topic_name(TENANT, GENERATION))
SUBSCRIPTION = f"{TOPIC}:0f0e0d0c-0b0a-4908-8706-050403020100"


def _attributes(**over):
    """AWS CLI get-subscription-attributes 의 확인된 이메일 구독 예시 모양 — PendingConfirmation 이 없다."""
    values = {
        "SubscriptionArn": SUBSCRIPTION, "TopicArn": TOPIC, "Owner": ACCOUNT,
        "ConfirmationWasAuthenticated": "true", "Protocol": "email", "Endpoint": ADDRESS,
        "RawMessageDelivery": "false",
    }
    values.update(over)
    return {"Attributes": {k: v for k, v in values.items() if v is not None}}


def _entry(arn=SUBSCRIPTION, endpoint=ADDRESS, protocol="email", topic=TOPIC):
    return {"SubscriptionArn": arn, "Owner": ACCOUNT, "Protocol": protocol, "Endpoint": endpoint, "TopicArn": topic}


def _state(stubs, resources, attributes, *pages):
    sns = stubs.stubbers["sns"]
    sns.add_response("get_subscription_attributes", attributes, {"SubscriptionArn": SUBSCRIPTION})
    token = None
    for index, page in enumerate(pages):
        response = {"Subscriptions": page}
        if index < len(pages) - 1:
            response["NextToken"] = f"page-{index + 1}"
        sns.add_response(
            "list_subscriptions_by_topic", response,
            {"TopicArn": TOPIC} if token is None else {"TopicArn": TOPIC, "NextToken": token},
        )
        token = response.get("NextToken")
    return resources.subscription_state(subscription_arn=SUBSCRIPTION, topic_arn=TOPIC, address=ADDRESS)


def test_confirmed_subscription_without_pending_flag_is_confirmed_only_by_the_topic_listing(stubs, resources):
    # 공식 예시처럼 PendingConfirmation 이 없으면 목록에서 저장한 ARN 을 찾은 뒤에만 확인이다
    assert _state(stubs, resources, _attributes(), [_entry()]) == "CONFIRMED"
    assert _state(stubs, resources, _attributes(PendingConfirmation="false"), [_entry()]) == "CONFIRMED"


def test_pending_flag_and_pending_listing_are_pending(stubs, resources):
    assert _state(stubs, resources, _attributes(PendingConfirmation="true")) == "PENDING"
    assert _state(stubs, resources, _attributes(), [_entry(arn="PendingConfirmation")]) == "PENDING"


def test_confirmation_found_on_a_later_page(stubs, resources):
    other = _entry(arn=f"{TOPIC}:other", endpoint="someone@example.com")
    assert _state(stubs, resources, _attributes(), [], [other], [_entry()]) == "CONFIRMED"


@pytest.mark.parametrize("pages", [
    ([],),  # 속성은 있는데 목록 어디에도 없다 — 없음(MISSING)으로 단정하지 않는다
    ([_entry(arn=f"{TOPIC}:another-confirmed")],),  # 같은 주소의 다른 구독
    ([_entry(), _entry(arn="PendingConfirmation")],),  # 확인 · 대기가 함께 보인다
    ([{"SubscriptionArn": SUBSCRIPTION, "TopicArn": TOPIC, "Protocol": "email"}],),  # Endpoint 없음
    ([_entry(topic="arn:aws:sns:ap-northeast-2:999988887777:other-topic")],),  # 다른 토픽이 섞였다
])
def test_ambiguous_or_incomplete_listing_never_confirms(stubs, resources, pages):
    with pytest.raises(sn.SettingsProviderError) as unknown:
        _state(stubs, resources, _attributes(), *pages)
    assert unknown.value.category == "UNKNOWN"


def test_repeated_page_token_is_not_treated_as_complete(stubs, resources):
    sns = stubs.stubbers["sns"]
    sns.add_response("get_subscription_attributes", _attributes(), {"SubscriptionArn": SUBSCRIPTION})
    sns.add_response("list_subscriptions_by_topic", {"Subscriptions": [], "NextToken": "same"}, {"TopicArn": TOPIC})
    sns.add_response(
        "list_subscriptions_by_topic", {"Subscriptions": [], "NextToken": "same"},
        {"TopicArn": TOPIC, "NextToken": "same"},
    )
    with pytest.raises(sn.SettingsProviderError) as unknown:
        resources.subscription_state(subscription_arn=SUBSCRIPTION, topic_arn=TOPIC, address=ADDRESS)
    assert (unknown.value.category, unknown.value.code) == ("UNKNOWN", "sns_invalid_response")


@pytest.mark.parametrize(("attributes", "pages", "expected"), [
    (_attributes(Endpoint="other@example.com"), (), "MISMATCH"),
    (_attributes(Protocol="https"), (), "MISMATCH"),
    (_attributes(TopicArn="arn:aws:sns:ap-northeast-2:999988887777:other"), (), "MISMATCH"),
    (_attributes(), ([_entry(endpoint="other@example.com")],), "MISMATCH"),  # 저장한 ARN 이 다른 주소다
])
def test_nonmatching_subscription_is_mismatch(stubs, resources, attributes, pages, expected):
    assert _state(stubs, resources, attributes, *pages) == expected


@pytest.mark.parametrize("attributes", [
    _attributes(Endpoint=None),
    _attributes(PendingConfirmation="maybe"),
    {},  # Attributes 없음
])
def test_malformed_attributes_never_confirm(stubs, resources, attributes):
    with pytest.raises(sn.SettingsProviderError) as unknown:
        _state(stubs, resources, attributes)
    assert unknown.value.category == "UNKNOWN"


def test_removed_subscription_is_missing(stubs, resources):
    topic = _topic_arn(sn.topic_name(TENANT, GENERATION))
    stubs.stubbers["sns"].add_client_error("get_subscription_attributes", "NotFound", RAW)
    assert resources.subscription_state(
        subscription_arn=f"{topic}:x", topic_arn=topic, address=ADDRESS,
    ) == "MISSING"


def test_topic_delete_resolves_unrecorded_arn_and_never_touches_unmanaged_topics(stubs, resources):
    name = sn.topic_name(TENANT, GENERATION)
    sns = stubs.stubbers["sns"]
    sns.add_response("create_topic", {"TopicArn": _topic_arn(name)}, {"Name": name})
    sns.add_response("delete_topic", {}, {"TopicArn": _topic_arn(name)})
    resources.delete_topic(region=REGION, name=name)
    sns.add_client_error("delete_topic", "NotFound", RAW, expected_params={"TopicArn": _topic_arn(name)})
    resources.delete_topic(region=REGION, name=_topic_arn(name))
    for foreign in (_topic_arn("customer-a"), "customer-a"):
        with pytest.raises(sn.SettingsProviderError) as refused:
            resources.delete_topic(region=REGION, name=foreign)
        assert refused.value.code == "sns_name_not_owned"


def test_created_topic_must_be_a_standard_topic_arn_in_the_requested_region_and_name(stubs, resources):
    name = sn.topic_name(TENANT, GENERATION)
    sns = stubs.stubbers["sns"]
    sns.add_response("create_topic", {"TopicArn": _topic_arn(name)}, {"Name": name})
    assert resources.create_topic(region=REGION, name=name) == _topic_arn(name)
    for returned in (
        f"arn:aws:sns:us-east-1:{ACCOUNT}:{name}",  # 다른 리전
        _topic_arn(sn.topic_name(TENANT, str(uuid.uuid4()))),  # 다른 세대의 이름
        f"arn:aws:sns:{REGION}:12345:{name}",  # 계정 칸 형식 아님
        f"arn:aws:sns:{REGION}:{ACCOUNT}:extra:{name}",  # 칸이 더 있다
        _topic_arn(name) + ".fifo",  # 표준 토픽 아님
    ):
        sns.add_response("create_topic", {"TopicArn": returned}, {"Name": name})
        with pytest.raises(sn.SettingsProviderError) as invalid:
            resources.create_topic(region=REGION, name=name)
        assert (invalid.value.category, invalid.value.code) == ("UNKNOWN", "sns_invalid_response")


def test_errors_are_classified(stubs, resources):
    name = sn.topic_name(TENANT, GENERATION)
    sns = stubs.stubbers["sns"]
    sns.add_client_error("create_topic", "Throttling", RAW)
    with pytest.raises(sn.SettingsProviderError) as throttled:
        resources.create_topic(region=REGION, name=name)
    assert (throttled.value.category, throttled.value.code) == ("TRANSIENT", "sns_Throttling")

    class Unreachable:
        def create_topic(self, **_):
            raise ConnectTimeoutError(endpoint_url="https://sns.example/" + RAW)

    offline = sn.SettingsResources(lambda *_a, **_k: Unreachable())
    with pytest.raises(sn.SettingsProviderError) as connect:
        offline.create_topic(region=REGION, name=name)
    assert (connect.value.category, connect.value.code) == ("TRANSIENT", "sns_connect_failed")
    assert RAW not in str(connect.value)


# --- 플랫폼 전용 자격(services.aws.client 규칙) -------------------------------------------


class FakeFetcher:
    def __init__(self, response):
        self.response = response

    def full_url(self, relative_uri):
        return "http://169.254.170.2" + relative_uri

    def retrieve_full_uri(self, full_uri, headers=None):
        return self.response


def _ecs_credentials():
    expiry = datetime.now(UTC) + timedelta(minutes=60)
    return {
        "AccessKeyId": "ECSKEY", "SecretAccessKey": "ecs-secret", "Token": "tok",
        "Expiration": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@pytest.fixture()
def platform_env(monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "CUSTOMERKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "customer-secret")
    resolved = AwsSettings(_env_file=None)
    monkeypatch.setattr(client_module, "get_aws_settings", lambda: resolved)


def test_secrets_manager_client_uses_only_platform_task_credentials(platform_env, monkeypatch):
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/fake")
    monkeypatch.setattr(
        botocore.credentials, "ContainerMetadataFetcher", lambda *a, **k: FakeFetcher(_ecs_credentials()),
    )
    client = sn.platform_client("secretsmanager", REGION, connect_timeout=1.5, read_timeout=2.5)
    assert client.meta.service_model.service_name == "secretsmanager"
    assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert (client.meta.config.connect_timeout, client.meta.config.read_timeout) == (1.5, 2.5)
    credentials = client._request_signer._credentials
    assert credentials.method == "container-role"
    assert credentials.get_frozen_credentials().access_key == "ECSKEY"


def test_platform_client_fails_closed_without_task_credentials(platform_env, monkeypatch):
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", raising=False)
    resources = sn.SettingsResources()
    with pytest.raises(sn.SettingsProviderError) as missing:
        resources.read_slack_secret(_secret_arn(sn.secret_name(TENANT, str(uuid.uuid4()))), "v" * 32)
    assert (missing.value.category, missing.value.code) == (
        "PERMANENT", "secretsmanager_credentials_unavailable",
    )
    assert not isinstance(missing.value.__cause__, NoCredentialsError)
