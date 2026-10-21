"""Slack/SNS 전송 계약. HTTP·SDK는 프로토콜 경계의 테스트 더블이며 실호출은 없다."""

import dataclasses
import json
import logging
import socket
import ssl
from datetime import UTC, datetime

import pytest
from botocore.exceptions import (
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)
from botocore.exceptions import ClientError
from notifications.channels import MAX_RETRY_AFTER_SECONDS, SlackSender, SnsSender
from notifications.contracts import EventSnapshot, Route
from notifications.messages import SLACK_FALLBACK_TEXT, SNS_SUBJECT, slack_payload, sns_body
from services.aws.client import platform_sns_client

SECRET = "T0SECRET/B0SECRET/XXXXTOKENXXXX"
WEBHOOK = f"https://hooks.slack.com/services/{SECRET}"
TOPIC = "arn:aws:sns:ap-northeast-2:123456789012:secret-topic"
BASE = "https://app.example.com"


def make_event(**overrides) -> EventSnapshot:
    values = dict(
        event_id="evt-1", incident_id="inc-1", execution_id="exe-1",
        related_execution_id=None,
        target_arn="arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc",
        aws_account_id="123456789012", region="ap-northeast-2",
        kind="AUTO_ISOLATION", phase="RESULT", outcome="SUCCESS",
        trigger_source="PRE_MITIGATION_0_5S", runbook_id="RUNBOOK_EC2_ISOLATE",
        execution_status="SUCCESS", reason_code=None,
        occurred_at=datetime(2026, 10, 20, 1, 2, 3, tzinfo=UTC),
        created_at=datetime(2026, 10, 20, 1, 2, 4, tzinfo=UTC),
    )
    values.update(overrides)
    return EventSnapshot(**values)


TENANT = "11111111-1111-4111-8111-111111111111"


def make_route(channel="SLACK", destination=WEBHOOK, region=None) -> Route:
    return Route(
        tenant_id=TENANT, aws_account_id="123456789012", channel=channel,
        fingerprint="fp", destination=destination, region=region,
    )


# ---- messages -------------------------------------------------------------

_HIGH = dict(execution_id=None, trigger_source=None, runbook_id=None, execution_status=None)
_ISO = dict(trigger_source="PRE_MITIGATION_0_5S", runbook_id="RUNBOOK_EC2_ISOLATE")
# 롤백 이벤트는 원본 실행 기준(workflows.close_execution: execution=origin)이라 원본 런북이다.
_RB = dict(trigger_source="AUTO_ON_FAILURE", runbook_id="RUNBOOK_EC2_RIGHTSIZING")

# (kind, phase, outcome, 기대 제목, PR1 형태의 필드). 유효 조합 11개 전부.
PR1_CASES = [
    ("HIGH_INCIDENT_CREATED", "CREATED", None, "High 위험 인시던트 생성", _HIGH),
    ("AUTO_ISOLATION", "REQUESTED", None, "자동 격리 요청", dict(_ISO, execution_status="IN_PROGRESS")),
    ("AUTO_ISOLATION", "RESULT", "SUCCESS", "자동 격리 결과: 성공", dict(_ISO, execution_status="SUCCESS")),
    ("AUTO_ISOLATION", "RESULT", "FAILED", "자동 격리 결과: 실패", dict(_ISO, execution_status="FAILED")),
    ("AUTO_ISOLATION", "RESULT", "REJECTED", "자동 격리 결과: 거부", dict(_ISO, execution_status="FAILED")),
    ("AUTO_ISOLATION", "RESULT", "UNVERIFIED", "자동 격리 결과: 결과 미확인", dict(_ISO, execution_status="SUCCESS")),
    ("AUTO_ROLLBACK", "REQUESTED", None, "자동 원복 요청", dict(_RB, execution_status="ROLLBACK_INITIATED")),
    ("AUTO_ROLLBACK", "RESULT", "SUCCESS", "자동 원복 결과: 성공", dict(_RB, execution_status="ROLLED_BACK")),
    ("AUTO_ROLLBACK", "RESULT", "FAILED", "자동 원복 결과: 실패", dict(_RB, execution_status="ROLLBACK_FAILED")),
    ("AUTO_ROLLBACK", "RESULT", "REJECTED", "자동 원복 결과: 거부", dict(_RB, execution_status="ROLLBACK_FAILED")),
    ("AUTO_ROLLBACK", "RESULT", "UNVERIFIED", "자동 원복 결과: 결과 미확인", dict(_RB, execution_status="ROLLBACK_FAILED")),
]
PR1_EVENTS = [case[:3] for case in PR1_CASES]


def all_text(payload: dict) -> list[str]:
    return [b["text"]["text"] for b in payload["blocks"]]


def test_pr1_matrix_has_all_eleven_combinations():
    assert len(PR1_CASES) == len(set(PR1_EVENTS)) == 11


@pytest.mark.parametrize("kind,phase,outcome,title,fields", PR1_CASES)
def test_pr1_events_render_both_channels(kind, phase, outcome, title, fields):
    event = make_event(kind=kind, phase=phase, outcome=outcome, **fields)
    payload = slack_payload(event, make_route(), BASE)
    joined = "\n".join(all_text(payload))
    assert payload["blocks"][0]["text"]["text"] == title
    assert sns_body(event, make_route("SNS", TOPIC, "ap-northeast-2"), BASE).startswith(title + "\n")
    if outcome is not None:
        assert f"결과: {outcome}" in joined
    else:
        assert "결과: " not in joined.replace("발생", "")
    assert payload["text"] == SLACK_FALLBACK_TEXT
    assert payload["mrkdwn"] is False
    assert f"{BASE}/incidents/inc-1" in joined
    assert TENANT in joined and "123456789012" in joined and "evt-1" in joined
    assert kind in joined and phase in joined
    body = sns_body(event, make_route("SNS", TOPIC, "ap-northeast-2"), BASE)
    assert f"{BASE}/incidents/inc-1" in body and kind in body


def test_childless_rollback_failure_has_no_child_label_and_status_untouched():
    event = make_event(
        kind="AUTO_ROLLBACK", phase="RESULT", outcome="FAILED",
        related_execution_id=None, **dict(_RB, execution_status="ROLLBACK_FAILED"),
    )
    joined = "\n".join(all_text(slack_payload(event, make_route(), BASE)))
    assert "원본 실행 ID: exe-1" in joined
    assert "원복 실행 ID" not in joined
    assert "ROLLBACK_FAILED" in joined and "결과: FAILED" in joined


def test_rollback_child_labeled_separately_from_original():
    # 렌더러 계약: 저장된 execution_status 를 outcome 과 무관하게 그대로 표시한다
    # (재분류 금지). 실제 PR1 형태를 주장하는 픽스처가 아니다.
    event = make_event(
        kind="AUTO_ROLLBACK", phase="RESULT", outcome="SUCCESS",
        execution_id="orig-1", related_execution_id="child-9", execution_status="FAILED",
    )
    joined = "\n".join(all_text(slack_payload(event, make_route(), BASE)))
    assert "원본 실행 ID: orig-1" in joined
    assert "원복 실행 ID: child-9" in joined
    assert "실행 상태(실행 ID 기준): FAILED" in joined


def test_isolation_does_not_label_rollback_child():
    event = make_event(related_execution_id="child-9")
    joined = "\n".join(all_text(slack_payload(event, make_route(), BASE)))
    assert "child-9" not in joined and "실행 ID: exe-1" in joined


def test_malicious_strings_neutralised_and_bounded():
    evil = "<!channel> <@U1> <https://evil.example|click> & *bold*\r\n" + "A" * 5000
    event = make_event(
        incident_id=evil, event_id=evil, execution_id=evil, target_arn=evil,
        region=evil, kind=evil, phase=evil, outcome=evil, reason_code=evil,
    )
    route = dataclasses.replace(make_route(), tenant_id=evil)
    payload = slack_payload(event, route, BASE)
    texts = all_text(payload) + [payload["blocks"][0]["text"]["text"]]
    assert len(payload["blocks"]) <= 50
    for block in payload["blocks"]:
        assert block["text"]["type"] == "plain_text"
        assert len(block["text"]["text"]) <= 1500
        assert "\n" not in block["text"]["text"] and "\r" not in block["text"]["text"]
    for t in texts:
        assert "<!channel>" not in t and "<@U1>" not in t and "<https" not in t
    assert "&lt;!channel&gt;" in "\n".join(texts)
    assert "&amp;amp;" not in "\n".join(texts)  # 한 번만 이스케이프
    assert sum(len(t) for t in texts) < 8000
    assert len(json.dumps(payload).encode()) < 20000
    body = sns_body(event, route, BASE)
    assert "\r" not in body and len(body.encode()) < 8000
    assert "\n" not in SNS_SUBJECT and SNS_SUBJECT.isascii() and len(SNS_SUBJECT) < 100


def test_incident_id_in_url_is_percent_encoded():
    event = make_event(incident_id="a/b?c#d")
    joined = "\n".join(all_text(slack_payload(event, make_route(), BASE)))
    assert f"{BASE}/incidents/a%2Fb%3Fc%23d" in joined


def test_no_free_text_fields_reach_message():
    names = {f.name for f in dataclasses.fields(EventSnapshot)}
    assert not names & {"raw_log", "exception", "ai_summary", "reason"}


# ---- Slack transport ------------------------------------------------------


class FakeSock:
    def __init__(self, conn):
        self.conn = conn

    def settimeout(self, value):
        self.conn.read_timeout = value


class FakeResponse:
    def __init__(self, status, body=b"ok", headers=None, read_exc=None):
        self.status, self._body, self._headers = status, body, headers or {}
        self.read_sizes, self.read_exc = [], read_exc

    def getheader(self, name):
        return self._headers.get(name)

    def read(self, amt=None):
        self.read_sizes.append(amt)
        if self.read_exc:
            raise self.read_exc
        return self._body[:amt]


class FakeConn:
    instances = []

    def __init__(self, host, port=None, timeout=None, *, response=None,
                 connect_exc=None, request_exc=None, response_exc=None):
        self.host, self.port, self.connect_timeout = host, port, timeout
        self.response, self.connect_exc = response, connect_exc
        self.request_exc, self.response_exc = request_exc, response_exc
        self.requests, self.closed, self.read_timeout = [], False, None
        self.sock = None
        FakeConn.instances.append(self)

    def connect(self):
        if self.connect_exc:
            raise self.connect_exc
        self.sock = FakeSock(self)

    def request(self, method, url, body, headers):
        self.requests.append((method, url, body, headers))
        if self.request_exc:
            raise self.request_exc

    def getresponse(self):
        if self.response_exc:
            raise self.response_exc
        return self.response

    def close(self):
        self.closed = True


def slack(**conn_kwargs):
    FakeConn.instances = []
    return SlackSender(
        connection_factory=lambda host, port, timeout: FakeConn(
            host, port, timeout, **conn_kwargs
        )
    )


def assert_no_secret(result, caplog):
    blob = repr(result) + json.dumps(dataclasses.asdict(result), default=str)
    assert SECRET not in blob and "hooks.slack.com" not in blob and TOPIC not in blob
    assert SECRET not in caplog.text and TOPIC not in caplog.text


def test_slack_success_request_shape():
    sender = slack(response=FakeResponse(200, b"ok"))
    result = sender.send(make_event(), make_route(), BASE)
    conn = FakeConn.instances[0]
    assert result.succeeded and result.receipt_kind == "SLACK_POSTED"
    assert result.receipt_ref is None
    assert (conn.host, conn.port) == ("hooks.slack.com", None)
    assert conn.connect_timeout == 3.0 and conn.read_timeout == 5.0
    method, url, body, headers = conn.requests[0]
    assert (method, url) == ("POST", f"/services/{SECRET}")
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == slack_payload(make_event(), make_route(), BASE)
    assert conn.response.read_sizes == [257] and conn.closed
    assert not any(k.lower() in {"authorization", "cookie"} for k in headers)


def test_slack_custom_timeouts_applied():
    FakeConn.instances = []
    sender = SlackSender(
        connect_timeout=1.5, read_timeout=2.5,
        connection_factory=lambda h, p, timeout: FakeConn(
            h, p, timeout, response=FakeResponse(200)
        ),
    )
    sender.send(make_event(), make_route(), BASE)
    assert FakeConn.instances[0].connect_timeout == 1.5
    assert FakeConn.instances[0].read_timeout == 2.5


@pytest.mark.parametrize("body", [b"", b"OK", b"ok\n", b"no_service", b"ok" + b"x" * 500])
def test_slack_200_requires_exact_ok(body):
    result = slack(response=FakeResponse(200, body)).send(make_event(), make_route(), BASE)
    assert not result.succeeded
    assert (result.failure_category, result.error_code) == ("UNKNOWN", "slack_unexpected_body")


def test_slack_429_retry_after_passed_unclamped(caplog):
    result = slack(response=FakeResponse(429, b"ratelimited", {"Retry-After": "86400"})).send(
        make_event(), make_route(), BASE
    )
    assert result.failure_category == "TRANSIENT" and result.error_code == "slack_http_429"
    assert result.retry_after_seconds == 86400.0
    assert_no_secret(result, caplog)


def test_slack_429_known_retry_after_survives_body_read_timeout(caplog):
    response = FakeResponse(429, b"x", {"Retry-After": "120"}, read_exc=socket.timeout(SECRET))
    result = slack(response=response).send(make_event(), make_route(), BASE)
    assert result.failure_category == "TRANSIENT" and result.retry_after_seconds == 120.0
    assert response.read_sizes == [] and FakeConn.instances[0].closed
    assert_no_secret(result, caplog)


def test_slack_retry_after_beyond_nine_digits_honored():
    result = slack(response=FakeResponse(429, b"", {"Retry-After": "0000086400"})).send(
        make_event(), make_route(), BASE
    )
    assert result.retry_after_seconds == 86400.0
    result = slack(response=FakeResponse(429, b"", {"Retry-After": str(MAX_RETRY_AFTER_SECONDS)})).send(
        make_event(), make_route(), BASE
    )
    assert result.retry_after_seconds == float(MAX_RETRY_AFTER_SECONDS)


@pytest.mark.parametrize("value", [str(MAX_RETRY_AFTER_SECONDS + 1), "9" * 20, "9" * 6000])
def test_slack_retry_after_out_of_range_is_terminal_not_early(value):
    result = slack(response=FakeResponse(429, b"", {"Retry-After": value})).send(
        make_event(), make_route(), BASE
    )
    assert (result.failure_category, result.error_code) == (
        "PERMANENT", "slack_retry_after_out_of_range",
    )
    assert result.retry_after_seconds is None


@pytest.mark.parametrize("header", [None, "", "soon", "-5", "1.5"])
def test_slack_429_bad_retry_after_ignored(header):
    headers = {} if header is None else {"Retry-After": header}
    result = slack(response=FakeResponse(429, b"", headers)).send(
        make_event(), make_route(), BASE
    )
    assert result.failure_category == "TRANSIENT" and result.retry_after_seconds is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_slack_permanent_4xx(status):
    result = slack(response=FakeResponse(status, b"invalid_payload")).send(
        make_event(), make_route(), BASE
    )
    assert (result.failure_category, result.error_code) == ("PERMANENT", f"slack_http_{status}")


@pytest.mark.parametrize("status", [500, 502, 503])
def test_slack_5xx_is_ambiguous(status):
    result = slack(response=FakeResponse(status, b"boom")).send(make_event(), make_route(), BASE)
    assert (result.failure_category, result.error_code) == ("UNKNOWN", f"slack_http_{status}")


def test_slack_redirect_refused_and_not_followed():
    sender = slack(response=FakeResponse(302, b"", {"Location": "https://evil.example/x"}))
    result = sender.send(make_event(), make_route(), BASE)
    assert (result.failure_category, result.error_code) == ("PERMANENT", "slack_redirect_refused")
    assert len(FakeConn.instances) == 1 and len(FakeConn.instances[0].requests) == 1
    assert "evil" not in repr(result)


def test_slack_response_read_is_bounded():
    response = FakeResponse(200, b"x" * 10_000)
    slack(response=response).send(make_event(), make_route(), BASE)
    assert response.read_sizes == [257]


@pytest.mark.parametrize(
    "exc,code",
    [
        (socket.timeout(SECRET), "slack_connect_timeout"),
        (ssl.SSLError(SECRET), "slack_tls_error"),
        (socket.gaierror(SECRET), "slack_connect_failed"),
        (ConnectionRefusedError(SECRET), "slack_connect_failed"),
    ],
)
def test_slack_connect_failures_are_transient(exc, code, caplog):
    caplog.set_level(logging.DEBUG)
    result = slack(connect_exc=exc).send(make_event(), make_route(), BASE)
    assert (result.failure_category, result.error_code) == ("TRANSIENT", code)
    assert FakeConn.instances[0].requests == [] and FakeConn.instances[0].closed
    assert_no_secret(result, caplog)


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"response_exc": socket.timeout(SECRET)}, "slack_read_timeout"),
        ({"request_exc": socket.timeout(SECRET)}, "slack_read_timeout"),
        ({"response_exc": ConnectionResetError(SECRET)}, "slack_io_error"),
        ({"response_exc": __import__("http.client").client.BadStatusLine(SECRET)}, "slack_io_error"),
    ],
)
def test_slack_failure_after_submission_is_unknown(kwargs, code, caplog):
    caplog.set_level(logging.DEBUG)
    result = slack(**kwargs).send(make_event(), make_route(), BASE)
    assert (result.failure_category, result.error_code) == ("UNKNOWN", code)
    assert FakeConn.instances[0].closed
    assert_no_secret(result, caplog)


@pytest.mark.parametrize("destination", ["http://hooks.slack.com/x", "not a url", "https:///x", ""])
def test_slack_invalid_destination_permanent_without_connecting(destination):
    sender = slack(response=FakeResponse(200))
    result = sender.send(make_event(), make_route(destination=destination), BASE)
    assert (result.failure_category, result.error_code) == ("PERMANENT", "slack_invalid_destination")
    assert FakeConn.instances == []


def test_slack_connection_factory_failure_is_safe(caplog):
    def factory(host, port, timeout):
        raise RuntimeError(SECRET)

    result = SlackSender(connection_factory=factory).send(make_event(), make_route(), BASE)
    assert (result.failure_category, result.error_code) == ("TRANSIENT", "slack_connect_failed")
    assert_no_secret(result, caplog)


class CloseFailConn(FakeConn):
    def close(self):
        raise RuntimeError(SECRET)


@pytest.mark.parametrize(
    "response,expected",
    [
        (FakeResponse(200, b"ok"), ("SLACK_POSTED", None)),
        (FakeResponse(429, b"", {"Retry-After": "7"}), (None, "slack_http_429")),
    ],
)
def test_slack_close_failure_does_not_replace_result(response, expected, caplog):
    sender = SlackSender(
        connection_factory=lambda h, p, timeout: CloseFailConn(h, p, timeout, response=response)
    )
    result = sender.send(make_event(), make_route(), BASE)
    assert (result.receipt_kind, result.error_code) == expected
    if expected[1]:
        assert result.retry_after_seconds == 7.0
    assert_no_secret(result, caplog)


def test_reason_code_never_rendered():
    event = make_event(reason_code="LEAK-<!channel>-reasoncode")
    route = make_route()
    assert "reasoncode" not in "\n".join(all_text(slack_payload(event, route, BASE)))
    assert "reasoncode" not in sns_body(event, route, BASE)


def test_slack_destination_not_in_route_repr():
    assert SECRET not in repr(make_route())


def test_slack_close_is_noop():
    assert SlackSender().close() is None


def test_slack_default_connection_has_no_proxy_or_redirect_machinery(monkeypatch):
    import http.client

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    assert SlackSender()._connection_factory is http.client.HTTPSConnection


# ---- SNS transport --------------------------------------------------------


class FakeSnsClient:
    def __init__(self, response=None, exc=None):
        self.response, self.exc = response, exc
        self.calls, self.closed = [], False

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.response

    def close(self):
        self.closed = True


def make_sns(client):
    factory_calls = []

    def factory(region, *, connect_timeout, read_timeout):
        factory_calls.append((region, connect_timeout, read_timeout))
        return client

    return SnsSender(client_factory=factory), factory_calls


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": SECRET + TOPIC}}, "Publish")


def sns_route():
    return make_route("SNS", TOPIC, "ap-northeast-2")


def test_sns_default_factory_is_platform_client():
    assert SnsSender()._client_factory is platform_sns_client


def test_sns_accepts_and_publishes_topic_only():
    client = FakeSnsClient({"MessageId": "abc-123", "ResponseMetadata": {}})
    sender, factory_calls = make_sns(client)
    result = sender.send(make_event(), sns_route(), BASE)
    assert result.receipt_kind == "SNS_ACCEPTED" and result.receipt_ref == "abc-123"
    assert result.failure_category is None
    assert factory_calls == [("ap-northeast-2", 3.0, 5.0)]
    (call,) = client.calls
    assert set(call) == {"TopicArn", "Subject", "Message"}
    assert call["TopicArn"] == TOPIC and call["Subject"] == SNS_SUBJECT
    assert call["Message"] == sns_body(make_event(), sns_route(), BASE)
    assert len(call["Message"].encode()) <= 262144


def test_sns_client_cached_per_region_and_closed():
    client = FakeSnsClient({"MessageId": "m"})
    sender, factory_calls = make_sns(client)
    sender.send(make_event(), sns_route(), BASE)
    sender.send(make_event(), sns_route(), BASE)
    assert len(factory_calls) == 1
    sender.close()
    assert client.closed
    sender.send(make_event(), sns_route(), BASE)
    assert len(factory_calls) == 2


def test_sns_custom_timeouts_passed_to_factory():
    calls = []

    def factory(region, *, connect_timeout, read_timeout):
        calls.append((connect_timeout, read_timeout))
        return FakeSnsClient({"MessageId": "m"})

    SnsSender(factory, connect_timeout=1.0, read_timeout=2.0).send(
        make_event(), sns_route(), BASE
    )
    assert calls == [(1.0, 2.0)]


def test_sns_missing_region_is_permanent_without_factory_call():
    sender, factory_calls = make_sns(FakeSnsClient({"MessageId": "m"}))
    result = sender.send(make_event(), make_route("SNS", TOPIC, None), BASE)
    assert (result.failure_category, result.error_code) == ("PERMANENT", "sns_region_missing")
    assert factory_calls == []


@pytest.mark.parametrize(
    "response",
    [{}, {"MessageId": ""}, {"MessageId": 5}, {"MessageId": "x" * 101},
     {"MessageId": "a b"}, {"MessageId": "a\nb"}, {"MessageId": "<https://x|y>"},
     {"MessageId": "a/b"}, None],
)
def test_sns_invalid_message_id_is_unknown(response):
    sender, _ = make_sns(FakeSnsClient(response))
    result = sender.send(make_event(), sns_route(), BASE)
    assert not result.succeeded
    assert (result.failure_category, result.error_code) == ("UNKNOWN", "sns_invalid_message_id")


def test_sns_message_id_100_chars_ok():
    sender, _ = make_sns(FakeSnsClient({"MessageId": "x" * 100}))
    assert sender.send(make_event(), sns_route(), BASE).receipt_ref == "x" * 100


@pytest.mark.parametrize(
    "code,category",
    [
        ("InvalidParameter", "PERMANENT"), ("NotFound", "PERMANENT"),
        ("AuthorizationError", "PERMANENT"), ("KMSAccessDenied", "PERMANENT"),
        ("EndpointDisabled", "PERMANENT"),
        ("Throttled", "TRANSIENT"), ("KMSThrottling", "TRANSIENT"),
        ("InternalError", "UNKNOWN"), ("ServiceUnavailable", "UNKNOWN"),
    ],
)
def test_sns_client_error_whitelist(code, category, caplog):
    caplog.set_level(logging.DEBUG)
    sender, _ = make_sns(FakeSnsClient(exc=client_error(code)))
    result = sender.send(make_event(), sns_route(), BASE)
    assert (result.failure_category, result.error_code) == (category, f"sns_{code}")
    assert_no_secret(result, caplog)


def test_sns_unlisted_error_code_is_sanitized():
    sender, _ = make_sns(FakeSnsClient(exc=client_error(f"Weird{SECRET}")))
    result = sender.send(make_event(), sns_route(), BASE)
    assert (result.failure_category, result.error_code) == ("UNKNOWN", "sns_unrecognized_error")
    assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "exc,category,code",
    [
        (ConnectTimeoutError(endpoint_url=TOPIC), "TRANSIENT", "sns_connect_failed"),
        (EndpointConnectionError(endpoint_url=TOPIC), "TRANSIENT", "sns_connect_failed"),
        (ReadTimeoutError(endpoint_url=TOPIC), "UNKNOWN", "sns_read_timeout"),
        (NoCredentialsError(), "PERMANENT", "sns_credentials_unavailable"),
        (RuntimeError(SECRET + TOPIC), "UNKNOWN", "sns_unexpected_error"),
    ],
)
def test_sns_sdk_errors_map_to_safe_codes(exc, category, code, caplog):
    caplog.set_level(logging.DEBUG)
    sender, _ = make_sns(FakeSnsClient(exc=exc))
    result = sender.send(make_event(), sns_route(), BASE)
    assert (result.failure_category, result.error_code) == (category, code)
    assert_no_secret(result, caplog)


def test_sns_factory_failure_is_safe(caplog):
    def factory(region, *, connect_timeout, read_timeout):
        raise RuntimeError(SECRET)

    result = SnsSender(factory).send(make_event(), sns_route(), BASE)
    assert (result.failure_category, result.error_code) == ("UNKNOWN", "sns_unexpected_error")
    assert_no_secret(result, caplog)
