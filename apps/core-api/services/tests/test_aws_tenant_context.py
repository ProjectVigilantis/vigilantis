"""테넌트 자격 · 문맥 테스트 (Issue #533 A, ADR-0012 §2).

실 메타데이터 · 네트워크 · AWS 호출 없음. 플랫폼 자격은 가짜 ECS fetcher, STS · SNS 는 HTTP 층
(URLLib3Session.send) 대역이다 — 팩토리 · 갱신 자격 · 서명은 실제 botocore 경로를 탄다.
대역은 서명에 쓰인 액세스 키를 보고 답하므로 「어느 자격으로 나갔는가」를 그대로 본다.
"""

import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import botocore.credentials
import botocore.httpsession
import pytest
from botocore.awsrequest import AWSResponse

CORE_API = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (str(CORE_API), str(REPO_ROOT / "packages")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import AwsSettings  # noqa: E402
from services.aws import client as client_module  # noqa: E402
from services.aws.client import TenantAccess, TenantContextRequired, tenant_scope  # noqa: E402

PLATFORM_KEY = "ECSPLATFORMKEY"
TENANT_A = TenantAccess(
    tenant_id="aaaaaaaa-1111-4000-8000-000000000001",
    access_role_arn="arn:aws:iam::111111111111:role/vigilantis-access",
    external_id="ext-a",
)
TENANT_B = TenantAccess(
    tenant_id="bbbbbbbb-2222-4000-8000-000000000002",
    access_role_arn="arn:aws:iam::222222222222:role/vigilantis-access",
    external_id="ext-b",
)
_SIGNED_KEY = re.compile(r"Credential=([^/]+)/")
_SIGNED_REGION = re.compile(r"Credential=[^/]+/[^/]+/([^/]+)/")


class FakeFetcher:
    """ContainerMetadataFetcher 대역 — 플랫폼 태스크 역할 자격(1시간)."""

    def full_url(self, relative_uri):
        return "http://169.254.170.2" + relative_uri

    def retrieve_full_uri(self, full_uri, headers=None):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        return {
            "AccessKeyId": PLATFORM_KEY,
            "SecretAccessKey": "s",
            "Token": "tok",
            "Expiration": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


class _Raw:
    def __init__(self, body: str):
        self._body = body.encode()

    def stream(self, *a, **k):
        yield self._body


class FakeAws:
    """HTTP 층 대역. AssumeRole 은 역할 ARN 의 계정으로 키를 내주고(TENANT<계정>-<회차>),
    GetCallerIdentity 는 서명한 키의 계정을 답한다. 보낸 요청을 (동작, 서명 키, 인자)로 적는다."""

    def __init__(self):
        self.sent: list[tuple[str, str, dict]] = []
        self.regions: list[tuple[str, str]] = []  # (동작, 서명 리전)
        self.issued = 0
        self.lifetime = timedelta(minutes=15)  # 900초 세션
        self.deny: dict[str, str] = {}  # 동작 → 403 으로 돌려줄 오류 코드
        self.caller_account: str | None = (
            None  # GetCallerIdentity 가 답할 계정(없으면 서명 키의 계정)
        )
        self._lock = threading.Lock()

    def assume_calls(self) -> list[dict]:
        return [params for action, _key, params in self.sent if action == "AssumeRole"]

    def send(self, request):
        body = (
            request.body.decode()
            if isinstance(request.body, bytes)
            else (request.body or "")
        )
        params = {k: v[0] for k, v in parse_qs(body).items()}
        action = params.get("Action", "")
        auth = request.headers["Authorization"]
        auth_text = auth.decode() if isinstance(auth, bytes) else auth
        key = _SIGNED_KEY.search(auth_text).group(1)
        with self._lock:
            self.sent.append((action, key, params))
            self.regions.append((action, _SIGNED_REGION.search(auth_text).group(1)))
            if action == "AssumeRole":
                self.issued += 1
                issued = self.issued
        if action in self.deny:
            error = (
                f"<Error><Code>{self.deny[action]}</Code><Message>m</Message></Error>"
            )
            xml = (
                f"<Response><Errors>{error}</Errors><RequestID>r</RequestID></Response>"
                if action == "DescribeInstances"  # EC2 의 오류 모양
                else f"<ErrorResponse>{error}</ErrorResponse>"
            )
            return AWSResponse(request.url, 403, {}, _Raw(xml))
        if action == "AssumeRole":
            account = params["RoleArn"].split(":")[4]
            expiry = (datetime.now(timezone.utc) + self.lifetime).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            xml = (
                "<AssumeRoleResponse><AssumeRoleResult><Credentials>"
                f"<AccessKeyId>TENANT{account}-{issued}</AccessKeyId>"
                "<SecretAccessKey>s</SecretAccessKey><SessionToken>t</SessionToken>"
                f"<Expiration>{expiry}</Expiration></Credentials>"
                "<AssumedRoleUser><AssumedRoleId>AROA:x</AssumedRoleId>"
                f"<Arn>{params['RoleArn']}</Arn></AssumedRoleUser>"
                "</AssumeRoleResult></AssumeRoleResponse>"
            )
        elif action == "GetCallerIdentity":
            account = self.caller_account or (
                key[len("TENANT") :].split("-")[0]
                if key.startswith("TENANT")
                else "999999999999"
            )
            xml = (
                "<GetCallerIdentityResponse><GetCallerIdentityResult>"
                f"<Arn>arn:aws:sts::{account}:assumed-role/x/y</Arn><UserId>u</UserId>"
                f"<Account>{account}</Account></GetCallerIdentityResult></GetCallerIdentityResponse>"
            )
        elif action == "DescribeInstances":
            xml = "<DescribeInstancesResponse><reservationSet/></DescribeInstancesResponse>"
        elif action == "Publish":
            xml = "<PublishResponse><PublishResult><MessageId>m</MessageId></PublishResult></PublishResponse>"
        else:
            raise AssertionError(f"예상하지 않은 요청: {action or request.url}")
        return AWSResponse(request.url, 200, {}, _Raw(xml))


@pytest.fixture
def aws(monkeypatch):
    """실 AWS 모드 · ECS 플랫폼 자격 · HTTP 대역. 테넌트 캐시는 테스트마다 비운다."""
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/fake")
    for name in (
        "AWS_ENDPOINT_URL",
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        botocore.credentials, "ContainerMetadataFetcher", lambda *a, **k: FakeFetcher()
    )
    fake = FakeAws()
    monkeypatch.setattr(
        botocore.httpsession.URLLib3Session,
        "send",
        lambda self, request: fake.send(request),
    )
    monkeypatch.setattr(client_module, "_tenant_credentials", {})
    monkeypatch.setattr(client_module, "_thread_local", threading.local())
    return fake


@pytest.fixture
def settings(monkeypatch):
    def _apply(**values):
        resolved = AwsSettings(_env_file=None, **values)
        monkeypatch.setattr(client_module, "get_aws_settings", lambda: resolved)
        return resolved

    _apply()
    return _apply


# --- 문맥 안의 자격 ---------------------------------------------------------------


def test_scope_assumes_the_tenant_role_from_platform_credentials(aws, settings):
    """플랫폼 자격으로 AssumeRole(역할 ARN · ExternalId · 900초 · 테넌트 세션 이름)."""
    with tenant_scope(TENANT_A):
        assert client_module.account_id() == "111111111111"

    assume = [
        (key, params) for action, key, params in aws.sent if action == "AssumeRole"
    ]
    assert len(assume) == 1
    key, params = assume[0]
    assert key == PLATFORM_KEY
    assert params["RoleArn"] == TENANT_A.access_role_arn
    assert params["ExternalId"] == "ext-a"
    assert params["DurationSeconds"] == "900"
    assert params["RoleSessionName"] == "vigilantis-aaaaaaaa"


def test_aws_client_in_scope_signs_with_the_tenant_credentials(aws, settings):
    with tenant_scope(TENANT_A):
        client_module.aws_client("sts").get_caller_identity()

    ((_action, key, _params),) = [s for s in aws.sent if s[0] == "GetCallerIdentity"]
    assert key == "TENANT111111111111-1"


def test_each_tenant_gets_its_own_credentials_and_inner_scope_wins(aws, settings):
    with tenant_scope(TENANT_A):
        assert client_module.account_id() == "111111111111"
        with tenant_scope(TENANT_B):
            assert client_module.account_id() == "222222222222"
        assert client_module.account_id() == "111111111111"
    assert client_module.current_tenant() is None
    assert [p["RoleArn"] for p in aws.assume_calls()] == [
        TENANT_A.access_role_arn,
        TENANT_B.access_role_arn,
    ]


def test_changed_role_or_external_id_gets_new_credentials(aws, settings):
    """역할을 다시 만들어 ARN · ExternalId 가 바뀐 행은 옛 자격을 물려받지 않는다."""
    moved = TenantAccess(TENANT_A.tenant_id, TENANT_B.access_role_arn, "ext-new")
    with tenant_scope(TENANT_A):
        client_module.account_id()
    with tenant_scope(moved):
        assert client_module.account_id() == "222222222222"
    assert [p["ExternalId"] for p in aws.assume_calls()] == ["ext-a", "ext-new"]


# --- 이행 스위치 ---------------------------------------------------------------


def test_switch_on_without_scope_raises_and_sends_nothing(aws, settings):
    settings(TENANT_CONTEXT_REQUIRED=True)
    with pytest.raises(TenantContextRequired):
        client_module.aws_client("ec2")
    with pytest.raises(TenantContextRequired):
        client_module.account_id()
    assert aws.sent == []


def test_switch_on_inside_scope_uses_the_tenant(aws, settings):
    settings(TENANT_CONTEXT_REQUIRED=True)
    with tenant_scope(TENANT_A):
        assert client_module.account_id() == "111111111111"


def test_switch_off_without_scope_keeps_the_process_credentials(
    aws, settings, monkeypatch
):
    """기본(꺼짐)은 지금과 같다 — 프로세스 자격으로 나가고 AssumeRole 을 부르지 않는다."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "PROCESSKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    client_module.aws_client("sts").get_caller_identity()
    assert [(a, k) for a, k, _p in aws.sent] == [("GetCallerIdentity", "PROCESSKEY")]


def test_tenant_session_ignores_process_keys_and_profile(
    aws, settings, monkeypatch, tmp_path
):
    """문맥 안에서는 프로세스 키 · 프로필이 남아 있어도 테넌트 자격만 쓴다."""
    cred = tmp_path / "credentials"
    cred.write_text(
        "[customer]\naws_access_key_id = FAKEPROFILEKEY\naws_secret_access_key = f\n"
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred))
    monkeypatch.setenv("AWS_PROFILE", "customer")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "PROCESSKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    with tenant_scope(TENANT_A):
        client_module.account_id()
    assert {k for _a, k, _p in aws.sent} == {PLATFORM_KEY, "TENANT111111111111-1"}


# --- 갱신 ---------------------------------------------------------------------


def test_refresh_does_not_call_sts_on_every_call(aws, settings):
    """900초 세션에서 호출마다 STS 를 부르지 않는다(botocore 기본 창이면 매번 갱신 대상)."""
    with tenant_scope(TENANT_A):
        for _ in range(5):
            client_module.account_id()
    assert len(aws.assume_calls()) == 1


def test_refreshes_once_inside_the_advisory_window(aws, settings):
    """만료 5분 전 창에 들어오면 다시 받는다 — 4분 남은 자격은 다음 호출에서 갱신된다."""
    aws.lifetime = timedelta(minutes=4)
    with tenant_scope(TENANT_A):
        client_module.account_id()
        aws.lifetime = timedelta(minutes=15)
        client_module.account_id()
        client_module.account_id()
    keys = [k for a, k, _p in aws.sent if a == "GetCallerIdentity"]
    assert len(aws.assume_calls()) == 2
    assert keys == [
        "TENANT111111111111-1",
        "TENANT111111111111-2",
        "TENANT111111111111-2",
    ]


def test_failed_refresh_keeps_credentials_outside_the_mandatory_window(aws, settings):
    """권고 창(5분)의 갱신이 실패해도 필수 창(2분) 밖이면 남은 자격으로 계속 나간다."""
    aws.lifetime = timedelta(minutes=4)
    with tenant_scope(TENANT_A):
        client_module.account_id()
        aws.deny["AssumeRole"] = "AccessDenied"
        assert client_module.account_id() == "111111111111"
    keys = [k for a, k, _p in aws.sent if a == "GetCallerIdentity"]
    assert keys == ["TENANT111111111111-1", "TENANT111111111111-1"]


def test_threads_get_their_own_session_but_share_the_tenant_credentials(aws, settings):
    sessions = {}

    def worker(name):
        with tenant_scope(TENANT_A):
            sessions[name] = client_module._tenant_session(TENANT_A)
            client_module.account_id()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("t1", "t2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sessions["t1"] is not sessions["t2"]
    assert len(aws.assume_calls()) == 1


# --- 규칙 밖 · 기록 ------------------------------------------------------------


def test_platform_sns_client_keeps_platform_credentials_inside_scope(aws, settings):
    """플랫폼 자체 자원 호출은 문맥 안에서도 고객 자격으로 나가지 않는다(ADR-0012 §2)."""
    with tenant_scope(TENANT_A):
        client_module.platform_sns_client("ap-northeast-2").publish(
            TopicArn="arn:aws:sns:ap-northeast-2:999999999999:t", Message="m"
        )
    assert [(a, k) for a, k, _p in aws.sent] == [("Publish", PLATFORM_KEY)]


def test_external_id_stays_out_of_repr():
    assert "ext-a" not in repr(TENANT_A)


# --- 연결 확인 어댑터 · 캐시 비우기(#533 B) ---------------------------------------
# 결과 5갈래의 분류 표는 tests/test_tenant_verification.py 가 대역 함수로 고정한다. 여기서는 어댑터가
# 실제 세션 · 단계 순서 · 캐시를 맞게 잇는지 본다.

REGION = (
    "us-west-2"  # 테넌트 regions[0] — 플랫폼 기본 리전(ap-northeast-2)과 일부러 다르게
)


def _verify(access=TENANT_A, expected="111111111111"):
    from tenant_verification import verify_connection

    return verify_connection(access, expected_account_id=expected, region=REGION)


def _actions(aws):
    return [(action, key) for action, key, _p in aws.sent]


def test_verify_ok_walks_the_three_steps_with_the_assumed_session(aws, settings):
    from schemas.api.tenants import TenantVerificationResult

    outcome = _verify()

    assert (outcome.result, outcome.reason_code) == (TenantVerificationResult.OK, None)
    assert _actions(aws) == [
        ("AssumeRole", PLATFORM_KEY),
        ("GetCallerIdentity", "TENANT111111111111-1"),
        ("DescribeInstances", "TENANT111111111111-1"),
    ]
    (params,) = [p for a, _k, p in aws.sent if a == "DescribeInstances"]
    assert params["MaxResults"] == "5"
    assert (
        params.get("Filter.1.Name") is None
    )  # 범위를 좁히지 않는다 — 읽기 권한만 본다
    # AssumeRole 은 플랫폼 리전, 고객 계정 쪽 두 단계는 테넌트의 첫 리전에서 묻는다
    assert aws.regions == [
        ("AssumeRole", "ap-northeast-2"),
        ("GetCallerIdentity", REGION),
        ("DescribeInstances", REGION),
    ]


@pytest.mark.parametrize(
    ("deny", "caller", "expected"),
    [
        ({"AssumeRole": "AccessDenied"}, None, ("ASSUME_DENIED", "AccessDenied")),
        ({}, "333333333333", ("ACCOUNT_MISMATCH", "CallerAccountMismatch")),
        (
            {"DescribeInstances": "UnauthorizedOperation"},
            None,
            ("READ_DENIED", "UnauthorizedOperation"),
        ),
    ],
    ids=["assume", "account", "read"],
)
def test_verify_stops_at_the_first_failed_step(aws, settings, deny, caller, expected):
    aws.deny.update(deny)
    aws.caller_account = caller

    outcome = _verify()

    assert (outcome.result.value, outcome.reason_code) == expected
    sent = [a for a, _k in _actions(aws)]
    stop = {"ASSUME_DENIED": 1, "ACCOUNT_MISMATCH": 2, "READ_DENIED": 3}[expected[0]]
    assert sent == ["AssumeRole", "GetCallerIdentity", "DescribeInstances"][:stop]


def test_verify_without_platform_credentials_is_unreachable(aws, settings, monkeypatch):
    """플랫폼 자신의 자격이 없으면 고객 계정의 거절이 아니다 — 아무 요청도 나가지 않는다."""
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")

    outcome = _verify()

    assert (outcome.result.value, outcome.reason_code) == (
        "UNREACHABLE",
        "NoCredentialsError",
    )
    assert aws.sent == []


def test_verify_does_not_use_or_fill_the_session_cache(aws, settings):
    """확인은 캐시된 자격으로 묻지 않고, 실패한 확인은 캐시를 건드리지 않는다."""
    with tenant_scope(TENANT_A):
        client_module.account_id()  # 캐시: TENANT…-1
    aws.deny["DescribeInstances"] = "UnauthorizedOperation"

    _verify()

    assert [k for a, k in _actions(aws) if a == "GetCallerIdentity"][
        -1
    ] == "TENANT111111111111-2"
    with tenant_scope(TENANT_A):
        client_module.account_id()
    assert [k for a, k in _actions(aws) if a == "GetCallerIdentity"][
        -1
    ] == "TENANT111111111111-1"


def test_verify_ok_drops_the_tenant_cache_for_every_thread(aws, settings):
    """통과하면 그 테넌트의 캐시를 버린다 — 이미 세션을 쥔 스레드도 다음 호출에서 다시 받는다."""
    ready, verified, done = threading.Event(), threading.Event(), threading.Event()
    keys = []

    def worker():
        with tenant_scope(TENANT_A):
            client_module.account_id()  # 이 스레드가 세션을 쥔다(TENANT…-1)
            ready.set()
            verified.wait(timeout=10)
            client_module.account_id()
        keys.append([k for a, k in _actions(aws) if a == "GetCallerIdentity"][-1])
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    ready.wait(timeout=10)
    with tenant_scope(TENANT_B):
        client_module.account_id()  # 다른 테넌트 캐시는 남는다
    assert _verify().result.value == "OK"  # 확인 세션: TENANT…-2
    verified.set()
    done.wait(timeout=10)
    thread.join()

    # 발급 순서: 스레드 1 · B 2 · 확인 3 · 버린 뒤 스레드의 새 자격 4
    assert keys == ["TENANT111111111111-4"]
    assert (
        len([p for p in aws.assume_calls() if p["RoleArn"] == TENANT_B.access_role_arn])
        == 1
    )
    with tenant_scope(TENANT_B):
        client_module.account_id()
    assert (
        len([p for p in aws.assume_calls() if p["RoleArn"] == TENANT_B.access_role_arn])
        == 1
    )
