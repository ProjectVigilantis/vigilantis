# Bedrock 고객 자격 경계 — 실제 SDK(서명 · 재시도 · 이벤트)를 통과시키고 HTTP 전송 계층만 가짜로 둔다.
# AWS · 네트워크 호출 없음.
from __future__ import annotations

import io
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.awsrequest import AWSResponse, HeadersDict
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import services.aws.client as K  # noqa: E402

ACCESS = K.TenantAccess(
    tenant_id="12345678-aaaa", access_role_arn="arn:aws:iam::111122223333:role/owner", external_id="ext-1"
)
SEOUL = "ap-northeast-2"

_STS_OK = b"""<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult>
<Credentials><AccessKeyId>OWNERAKID</AccessKeyId><SecretAccessKey>ownersecret</SecretAccessKey>
<SessionToken>owner-session-token</SessionToken><Expiration>2099-01-01T00:00:00Z</Expiration></Credentials>
</AssumeRoleResult><ResponseMetadata><RequestId>r</RequestId></ResponseMetadata></AssumeRoleResponse>"""


class _Raw(io.BytesIO):
    def stream(self, *_, **__):
        yield self.read()


class _Wire:
    """URLLib3Session.send 대체 — 서명이 끝난 요청을 기록하고 호스트별로 응답한다."""

    def __init__(self, sts_status: int = 200):
        self.sts_status = sts_status
        self.sts: list = []
        self.runtime: list = []

    def send(self, _session, request):
        host = urlparse(request.url).hostname
        if host.startswith("sts."):
            self.sts.append(request)
            body = _STS_OK if self.sts_status == 200 else b"<ErrorResponse/>"
            return self._response(request, self.sts_status, body, "text/xml")
        assert host.startswith("bedrock-runtime."), host
        self.runtime.append(request)
        return self._response(request, 200, b'{"ok": true}', "application/json")

    @staticmethod
    def _response(request, status, body, content_type):
        headers = HeadersDict({"Content-Type": content_type, "Content-Length": str(len(body))})
        return AWSResponse(request.url, status, headers, _Raw(body))


@pytest.fixture
def wire(monkeypatch):
    w = _Wire()
    monkeypatch.setattr("botocore.httpsession.URLLib3Session.send", lambda s, r: w.send(s, r))
    # 플랫폼 자격은 비공개 세션의 정적 자격으로 — 컨테이너 메타데이터 조회 없음.
    platform = Credentials("PLATFORMAKID", "platformsecret", "platform-token")
    monkeypatch.setattr(K, "_ecs_only_session", lambda provider=None: K._isolated_session(K._TenantProvider(platform)))
    # 섞인 주변 환경: 프로세스 키 · 다른 리전 · 엔드포인트 재정의가 있어도 영향이 없어야 한다.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AMBIENTAKID")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambientsecret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-token")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "http://localhost:4566")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setattr(K, "endpoint_url", lambda: None)
    monkeypatch.setattr(K, "default_region", lambda: "eu-west-1")
    return w


def _auth(request) -> str:
    value = request.headers["Authorization"]
    return value.decode() if isinstance(value, bytes) else value


def _client(region=SEOUL):
    return K.tenant_bedrock_runtime_client(ACCESS, region=region, connect_timeout=2.0, read_timeout=30.0)


def _invoke(client):
    return client.invoke_model(modelId="anthropic.claude-sonnet-5", body=b"{}")


def test_region_is_required_keyword_without_default():
    with pytest.raises(TypeError):
        K.tenant_bedrock_runtime_client(ACCESS, connect_timeout=2.0, read_timeout=30.0)


def test_invoke_is_signed_by_owner_role_in_seoul_despite_mixed_ambient_context(wire):
    _invoke(_client())

    (sts,) = wire.sts
    assert "Credential=PLATFORMAKID/" in _auth(sts) and "/ap-northeast-2/sts/aws4_request" in _auth(sts)
    form = parse_qs(sts.body.decode() if isinstance(sts.body, bytes) else sts.body)
    assert form["RoleArn"] == [ACCESS.access_role_arn] and form["ExternalId"] == ["ext-1"]

    (runtime,) = wire.runtime
    auth = _auth(runtime)
    assert "Credential=OWNERAKID/" in auth and "/ap-northeast-2/bedrock/aws4_request" in auth
    assert "AMBIENT" not in auth and "PLATFORM" not in auth
    assert runtime.headers["X-Amz-Security-Token"] in ("owner-session-token", b"owner-session-token")
    assert urlparse(runtime.url).hostname == "bedrock-runtime.ap-northeast-2.amazonaws.com"
    assert urlparse(runtime.url).path.endswith("/model/anthropic.claude-sonnet-5/invoke")


def test_non_seoul_region_signs_and_sends_in_that_region_with_independent_clients(wire):
    tokyo, virginia = _client("ap-northeast-1"), _client("us-east-1")
    _invoke(tokyo)
    _invoke(virginia)
    _invoke(tokyo)

    assert [urlparse(r.url).hostname for r in wire.sts] == [
        "sts.ap-northeast-1.amazonaws.com", "sts.us-east-1.amazonaws.com"
    ]
    assert [urlparse(r.url).hostname for r in wire.runtime] == [
        "bedrock-runtime.ap-northeast-1.amazonaws.com",
        "bedrock-runtime.us-east-1.amazonaws.com",
        "bedrock-runtime.ap-northeast-1.amazonaws.com",
    ]
    for request, region in zip(wire.runtime, ("ap-northeast-1", "us-east-1", "ap-northeast-1")):
        assert f"/{region}/bedrock/aws4_request" in _auth(request)


@pytest.mark.parametrize(
    "region",
    [
        None, "", " ", 7, "ap-northeast-2 ", "AP-NORTHEAST-2", "ap-northeast-2.evil.com/x?",
        "evil.com#", "us-gov-west-1", "cn-north-1", "xx-nowhere-9", "ap-northeast-2:443", "../x",
    ],
)
def test_malformed_or_unsupported_region_is_rejected_before_network(wire, region):
    with pytest.raises(K.BedrockBoundaryError) as caught:
        _client(region)
    assert str(caught.value) == K._BOUNDARY_MESSAGE
    assert wire.sts == [] and wire.runtime == []


def test_credentials_are_not_refreshed_during_calls(wire):
    client = _client()
    _invoke(client)
    _invoke(client)
    assert len(wire.sts) == 1 and len(wire.runtime) == 2


def test_sts_failure_is_single_attempt_raw_and_never_builds_runtime(wire):
    wire.sts_status = 500
    with pytest.raises(ClientError):
        _client()
    assert len(wire.sts) == 1 and wire.runtime == []


def test_runtime_error_is_single_attempt(wire, monkeypatch):
    client = _client()
    monkeypatch.setattr(
        _Wire, "_response", staticmethod(lambda r, s, b, c: AWSResponse(
            r.url, 503, HeadersDict({"Content-Type": "application/json"}), _Raw(b'{"message": "x"}')))
    )
    with pytest.raises(ClientError):
        _invoke(client)
    assert len(wire.runtime) == 1


def test_absent_owner_or_missing_role_material_is_rejected_before_network(wire):
    for bad in (None, K.TenantAccess("t", "", "e"), K.TenantAccess("t", "arn", "")):
        with pytest.raises(K.BedrockBoundaryError):
            K.tenant_bedrock_runtime_client(bad, region=SEOUL, connect_timeout=2.0, read_timeout=30.0)
    assert wire.sts == [] and wire.runtime == []


def test_custom_endpoint_is_rejected_before_credentials(wire, monkeypatch):
    monkeypatch.setattr(K, "endpoint_url", lambda: "http://localhost:4566")
    with pytest.raises(K.BedrockBoundaryError):
        _client()
    assert wire.sts == [] and wire.runtime == []


def test_bearer_token_is_rejected_before_credentials(wire, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer")
    with pytest.raises(K.BedrockBoundaryError):
        _client()
    assert wire.sts == [] and wire.runtime == []


def _converse(client):
    return client.converse(modelId="anthropic.claude-sonnet-5", messages=[{"role": "user", "content": [{"text": "x"}]}])


@pytest.mark.parametrize("call", [_invoke, _converse])
def test_bearer_token_set_after_construction_blocks_the_send(wire, monkeypatch, call):
    client = _client()
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bearer")
    with pytest.raises(K.BedrockBoundaryError):
        call(client)
    assert wire.runtime == []


def test_guard_is_bound_per_client_to_region_and_issued_credentials():
    def request(region="ap-northeast-1", key="AKID", token="tok", host=None):
        host = host or f"bedrock-runtime.{region}.amazonaws.com"
        return type("Req", (), {
            "url": f"https://{host}/model/m/invoke",
            "headers": {
                "Authorization": f"AWS4-HMAC-SHA256 Credential={key}/20260101/{region}/bedrock/aws4_request",
                "X-Amz-Security-Token": token,
            },
        })()

    guard = K._bedrock_send_guard("ap-northeast-1", "AKID", "tok")
    guard(request())
    # 다른 리전을 묶은 두 번째 가드가 첫 가드를 바꾸지 않는다.
    K._bedrock_send_guard("us-east-1", "AKID", "tok")(request("us-east-1"))
    guard(request())

    bad_headers = {"Authorization": "Bearer x", "X-Amz-Security-Token": "tok"}
    cases = [
        request("us-east-1"),
        request(host="bedrock-runtime.ap-northeast-1.amazonaws.com.evil.com"),
        request(key="OTHER"),
        request(token="other"),
        request(token=""),
        type("Req", (), {"url": request().url.replace("https:", "http:"), "headers": request().headers})(),
        type("Req", (), {"url": request().url, "headers": bad_headers})(),
        type("Req", (), {"url": request().url, "headers": {"Authorization": request().headers["Authorization"]}})(),
    ]
    for bad in cases:
        with pytest.raises(K.BedrockBoundaryError) as caught:
            guard(bad)
        assert "tok" not in str(caught.value) and "AKID" not in str(caught.value)
