import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from notifications.contracts import EventSnapshot
from notifications.routing import (
    RouteConfigError,
    RouteRegistry,
    parse_sns_topic_arn,
    validate_public_base_url,
    validate_webhook_url,
)

HOOK_A = "https://hooks.slack.com/services/T000/B000/AAAA1111"
HOOK_B = "https://hooks.slack.com/services/T111/B111/BBBB2222"
ARN_A = "arn:aws:sns:ap-northeast-2:999999999999:customer-a"
ARN_B = "arn:aws:sns:ap-northeast-2:999999999999:customer-b"
ENV = {"NOTIFICATION_SLACK_A": HOOK_A, "NOTIFICATION_SLACK_B": HOOK_B}
NOW = datetime(2026, 1, 1, tzinfo=UTC)
TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
TENANT_C = "33333333-3333-4333-8333-333333333333"
TENANT_X = "44444444-4444-4444-8444-444444444444"
TENANT_MIXED = "AbCdEf01-2345-4789-8abc-DEF012345678"
TENANT_MIXED_CANONICAL = "abcdef01-2345-4789-8abc-def012345678"


def route_a(**over):
    return {
        "tenant_id": TENANT_A,
        "aws_account_id": "111111111111",
        "slack_webhook_env": "NOTIFICATION_SLACK_A",
        "sns_topic_arn": ARN_A,
    } | over


def route_b(**over):
    return {
        "tenant_id": TENANT_B,
        "aws_account_id": "222222222222",
        "slack_webhook_env": "NOTIFICATION_SLACK_B",
        "sns_topic_arn": ARN_B,
    } | over


def registry(routes, env=ENV):
    return RouteRegistry.from_mapping({"routes": routes}, env)


def event(account="111111111111", target_arn=None):
    return EventSnapshot(
        event_id="e1", incident_id="i1", execution_id=None, related_execution_id=None,
        target_arn=target_arn or f"arn:aws:ec2:ap-northeast-2:{account or '111111111111'}:instance/i-1",
        aws_account_id=account, region="ap-northeast-2", kind="K", phase="P", outcome=None,
        trigger_source=None, runbook_id=None, execution_status=None, reason_code=None,
        occurred_at=NOW, created_at=NOW,
    )  # fmt: skip


def test_two_tenants_route_to_own_destinations():
    reg = registry([route_a(), route_b()])
    a = reg.resolve(event("111111111111"), "SLACK").route
    b = reg.resolve(event("222222222222"), "SNS").route
    assert (a.tenant_id, a.destination) == (TENANT_A, HOOK_A)
    assert (b.tenant_id, b.destination, b.region) == (TENANT_B, ARN_B, "ap-northeast-2")
    assert len(reg.routes) == 4
    assert HOOK_A not in repr(a) and HOOK_A not in a.fingerprint


@pytest.mark.parametrize(
    ("ev", "reason"),
    [
        (event(None), "ACCOUNT_MISSING"),
        (event("12"), "ACCOUNT_INVALID"),
        (event("111111111111", "arn:aws:ec2:ap-northeast-2:333333333333:instance/i-1"), "ARN_ACCOUNT_MISMATCH"),
        (event("111111111111", "garbage"), "TARGET_ARN_INVALID"),
        (event("333333333333"), "OWNERSHIP_MISSING"),
    ],
)
def test_blocked_without_fallback(ev, reason):
    decision = registry([route_a(), route_b()]).resolve(ev, "SLACK")
    assert decision.route is None
    assert decision.blocked_reason == reason


def test_topic_account_is_not_used_to_infer_ownership():
    # 토픽 계정(999999999999)이 이벤트 계정이어도 소유권이 없으면 차단
    ev = event("999999999999")
    assert registry([route_a()]).resolve(ev, "SNS").blocked_reason == "OWNERSHIP_MISSING"


def test_missing_channel_and_secret_block_only_that_channel():
    reg = registry([route_a(sns_topic_arn=None), route_b(slack_webhook_env="MISSING_ENV")])
    assert reg.resolve(event(), "SNS").blocked_reason == "CHANNEL_NOT_CONFIGURED"
    assert reg.resolve(event(), "SLACK").route is not None
    assert reg.resolve(event("222222222222"), "SLACK").blocked_reason == "SECRET_MISSING"
    assert reg.resolve(event("222222222222"), "SNS").route is not None


def test_owned_account_without_channels_blocks_per_event():
    reg = registry([route_a(slack_webhook_env=None, sns_topic_arn=None), route_b()])
    assert reg.routes[0].tenant_id == TENANT_B and len(reg.routes) == 2
    for channel in ("SLACK", "SNS"):
        assert reg.resolve(event(), channel).blocked_reason == "CHANNEL_NOT_CONFIGURED"
        assert reg.resolve(event("222222222222"), channel).route is not None


def test_destination_shared_across_tenants_blocks_slack_only():
    env = {**ENV, "NOTIFICATION_SLACK_B": HOOK_A}
    reg = registry([route_a(), route_b()], env)
    for account in ("111111111111", "222222222222"):
        d = reg.resolve(event(account), "SLACK")
        assert d.route is None and d.blocked_reason == "DESTINATION_SHARED"
        assert reg.resolve(event(account), "SNS").route is not None
    assert {r.channel for r in reg.routes} == {"SNS"}


def test_sns_topic_shared_across_tenants_blocks_sns_only():
    reg = registry([route_a(), route_b(sns_topic_arn=ARN_A)])
    for account in ("111111111111", "222222222222"):
        assert reg.resolve(event(account), "SNS").blocked_reason == "DESTINATION_SHARED"
        assert reg.resolve(event(account), "SLACK").route is not None


def test_blocked_owner_declared_destination_blocks_healthy_sharer():
    # account111 소유 충돌(a+x 가 같은 웹훅/토픽), account222 의 b 는 같은 목적지로 해석 → b 도 차단
    env = {**ENV, "NOTIFICATION_SLACK_B": HOOK_A}
    reg = registry(
        [route_a(), route_a(tenant_id=TENANT_X), route_b(sns_topic_arn=ARN_A)],
        env,
    )
    assert reg.routes == ()
    assert reg.resolve(event("111111111111"), "SLACK").blocked_reason == "OWNERSHIP_CONFLICT"
    for channel in ("SLACK", "SNS"):
        assert reg.resolve(event("222222222222"), channel).blocked_reason == "DESTINATION_SHARED"


def test_destination_conflict_config_still_declares_healthy_tenants_destination():
    # a 가 두 웹훅 env(값 상이) → DESTINATION_CONFLICT, 그 중 하나가 b 의 웹훅과 동일 → b 차단
    env = {**ENV, "NOTIFICATION_SLACK_A2": HOOK_B}
    reg = registry([route_a(), route_a(slack_webhook_env="NOTIFICATION_SLACK_A2"), route_b()], env)
    assert reg.resolve(event("111111111111"), "SLACK").blocked_reason == "DESTINATION_CONFLICT"
    assert reg.resolve(event("222222222222"), "SLACK").blocked_reason == "DESTINATION_SHARED"
    assert reg.resolve(event("222222222222"), "SNS").route is not None


def test_destination_conflict_config_declares_sns_arn_of_healthy_tenant():
    reg = registry([route_a(), route_a(sns_topic_arn=ARN_B), route_b()])
    assert reg.resolve(event("111111111111"), "SNS").blocked_reason == "DESTINATION_CONFLICT"
    assert reg.resolve(event("222222222222"), "SNS").blocked_reason == "DESTINATION_SHARED"
    assert reg.resolve(event("222222222222"), "SLACK").route is not None


def test_same_tenant_may_share_destination_across_accounts():
    reg = registry([route_a(), route_a(aws_account_id="333333333333")])
    for account in ("111111111111", "333333333333"):
        assert reg.resolve(event(account), "SLACK").route is not None
        assert reg.resolve(event(account), "SNS").route is not None


def test_empty_routes_starts_and_blocks_ownership_missing(tmp_path):
    reg = RouteRegistry.from_file(write(tmp_path, '{"routes": []}'), ENV)
    assert reg.routes == ()
    for channel in ("SLACK", "SNS"):
        assert reg.resolve(event(), channel).blocked_reason == "OWNERSHIP_MISSING"


def test_blank_and_malicious_secret_values_block():
    reg = registry([route_a(), route_b()], {"NOTIFICATION_SLACK_A": "", "NOTIFICATION_SLACK_B": "https://evil.test/x"})
    assert reg.resolve(event(), "SLACK").blocked_reason == "SECRET_MISSING"
    assert reg.resolve(event("222222222222"), "SLACK").blocked_reason == "DESTINATION_INVALID"


def test_ownership_conflict_blocks_both_channels():
    reg = registry([route_a(), route_a(tenant_id=TENANT_X)])
    for channel in ("SLACK", "SNS"):
        assert reg.resolve(event(), channel).blocked_reason == "OWNERSHIP_CONFLICT"
    assert reg.routes == ()


def test_equivalent_duplicates_dedupe_and_split_channels_merge():
    reg = registry([route_a(), route_a(), route_a(slack_webhook_env=None), route_a(sns_topic_arn=None)])
    assert len(reg.routes) == 2
    reg = registry([route_a(sns_topic_arn=None), route_a(slack_webhook_env=None)])
    assert {r.channel for r in reg.routes} == {"SLACK", "SNS"}
    # 다른 변수 이름이라도 같은 URL 이면 동등
    env = ENV | {"ALIAS": HOOK_A}
    assert len(registry([route_a(), route_a(slack_webhook_env="ALIAS")], env).routes) == 2


def test_differing_same_channel_destinations_conflict():
    reg = registry([route_a(), route_a(sns_topic_arn=ARN_B, slack_webhook_env="NOTIFICATION_SLACK_B")])
    assert reg.resolve(event(), "SLACK").blocked_reason == "DESTINATION_CONFLICT"
    assert reg.resolve(event(), "SNS").blocked_reason == "DESTINATION_CONFLICT"


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/services/T/B/X",
        "https://hooks.slack.com.evil.test/services/T/B/X",
        "https://evil.test/services/T/B/X",
        "https://user:pw@hooks.slack.com/services/T/B/X",
        "https://hooks.slack.com:444/services/T/B/X",
        "https://hooks.slack.com/services/T/B/X?x=1",
        "https://hooks.slack.com/services/T/B/X#f",
        "https://hooks.slack.com/services/T/B/X/",
        "https://hooks.slack.com/services/T/B",
        "https://hooks.slack.com/services/T/B/X\n",
        "https://hooks.slack.com/services/T/../X/Y",
        "https://hooks.slack.com@evil.test/services/T/B/X",
        "https://HOOKS.slack.com/services/T/B/X",
        "",
        None,
    ],
)
def test_malicious_webhook_rejected_without_leak(url):
    with pytest.raises(RouteConfigError) as exc:
        validate_webhook_url(url)
    assert str(url or "x") not in str(exc.value) or url in ("", None)


def test_valid_webhook():
    assert validate_webhook_url(HOOK_A) == HOOK_A


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:sns:ap-northeast-2:999999999999:t.fifo",
        "arn:aws:sns:ap-northeast-2:99:t",
        "arn:aws:sqs:ap-northeast-2:999999999999:t",
        "arn:aws:sns:ap-northeast-2:999999999999:t:subscription",
        "arn:aws-cn:sns:cn-north-1:999999999999:t",
    ],
)
def test_sns_arn_rejects_fifo_and_non_topic(arn):
    with pytest.raises(RouteConfigError):
        parse_sns_topic_arn(arn)
    with pytest.raises(RouteConfigError):
        registry([route_a(sns_topic_arn=arn)])
    with pytest.raises(RouteConfigError):
        parse_sns_topic_arn(None)


def test_sns_region_derived():
    assert parse_sns_topic_arn(ARN_A) == ("ap-northeast-2", "999999999999")


def test_public_base_url():
    assert validate_public_base_url("https://Dash.Example.com/") == "https://dash.example.com"
    assert validate_public_base_url("https://dash.example.com:8443") == "https://dash.example.com:8443"
    for bad in [
        "http://dash.example.com", "https://dash.example.com/app", "https://dash.example.com?token=1",
        "https://dash.example.com/#x", "https://u:p@dash.example.com", "https://localhost",
        "https://10.0.0.1", "/incidents", "", None, "https://dash.example.com:99999",
    ]:  # fmt: skip
        with pytest.raises(RouteConfigError):
            validate_public_base_url(bad)


def write(tmp_path, content, mode="w"):
    path = tmp_path / "routes.json"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return str(path)


def test_from_file_ok(tmp_path):
    path = write(tmp_path, json.dumps({"routes": [route_a()]}))
    assert len(RouteRegistry.from_file(path, ENV).routes) == 2


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"routes": [], "extra": 1}',
        json.dumps({"routes": [route_a(tenant_id="   ")]}),
        json.dumps({"routes": [route_a(webhook_url=HOOK_A)]}),
        json.dumps({"routes": [route_a(slack_webhook_env=HOOK_A)]}),
        json.dumps({"routes": [route_a(tenant_id="")]}),
        json.dumps({"routes": [route_a(tenant_id="t" * 129)]}),
        json.dumps({"routes": [route_a(aws_account_id="1")]}),
        json.dumps({"routes": [route_a(tenant_id="customer-a")]}),
        json.dumps({"routes": [route_a(tenant_id=None)]}),
        json.dumps({"routes": [{k: v for k, v in route_a().items() if k != "tenant_id"}]}),
        '{"routes": [], "routes": []}',
    ],
)
def test_invalid_structure_fails_safely(tmp_path, content):
    with pytest.raises(RouteConfigError) as exc:
        RouteRegistry.from_file(write(tmp_path, content), ENV)
    assert exc.value.__cause__ is None and (exc.value.__suppress_context__ or exc.value.__context__ is None)
    assert HOOK_A not in str(exc.value) and "NOTIFICATION" not in str(exc.value)


def test_oversized_file_rejected(tmp_path):
    with pytest.raises(RouteConfigError, match="too large"):
        RouteRegistry.from_file(write(tmp_path, b" " * 70000), ENV)


def test_unreadable_file_is_sanitized(tmp_path):
    missing = str(tmp_path / "secret-dir-name" / "x.json")
    with pytest.raises(RouteConfigError) as exc:
        RouteRegistry.from_file(missing, ENV)
    assert "secret-dir-name" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert exc.value.__context__ is not None  # 원본은 숨겨진 채 보존만, 메시지에는 없음
    with pytest.raises(RouteConfigError):
        RouteRegistry.from_file(str(tmp_path), ENV)


def test_fingerprint_binds_route_not_global_config():
    base = registry([route_a(), route_b()])
    fp = {(r.tenant_id, r.channel): r.fingerprint for r in base.routes}
    assert len(set(fp.values())) == 4
    a_slack = (TENANT_A, "SLACK")

    # 한 고객의 웹훅만 회전 → 그 라우트만 변경, 전역 설정이 달라져도(순서/추가 필드 없는 라우트 추가) 나머지 동일
    rotated = registry([route_a(), route_b()], ENV | {"NOTIFICATION_SLACK_A": "https://hooks.slack.com/services/T999/B999/CCCC3333"})
    new = {(r.tenant_id, r.channel): r.fingerprint for r in rotated.routes}
    assert new[a_slack] != fp[a_slack]
    assert {k: v for k, v in new.items() if k != a_slack} == {k: v for k, v in fp.items() if k != a_slack}

    third = registry(
        [
            route_b(),
            route_a(),
            route_a(
                tenant_id=TENANT_C,
                aws_account_id="333333333333",
                slack_webhook_env="NOTIFICATION_SLACK_C",
                sns_topic_arn="arn:aws:sns:ap-northeast-2:999999999999:customer-c",
            ),
        ],
        ENV | {"NOTIFICATION_SLACK_C": "https://hooks.slack.com/services/T888/B888/DDDD4444"},
    )
    assert {(r.tenant_id, r.channel): r.fingerprint for r in third.routes if r.tenant_id != TENANT_C} == fp

    # 계정 변경도 fingerprint 변경
    moved = registry([route_a(aws_account_id="444444444444"), route_b()])
    assert {r.fingerprint for r in moved.routes if r.tenant_id == TENANT_A}.isdisjoint(fp.values())


def _encodings(canonical):
    plain = canonical.replace("-", "")
    return [canonical.upper(), plain, plain.upper(), "{" + canonical + "}", "urn:uuid:" + canonical]


def test_tenant_uuid_is_normalized_to_canonical_string():
    for raw in [TENANT_MIXED, *_encodings(TENANT_MIXED_CANONICAL)]:
        reg = registry([route_a(tenant_id=raw)])
        assert {r.tenant_id for r in reg.routes} == {TENANT_MIXED_CANONICAL}
        assert all(type(r.tenant_id) is str for r in reg.routes)


def test_equivalent_uuid_encodings_share_owner_and_fingerprint():
    base = registry([route_a()])
    fp = {r.channel: r.fingerprint for r in base.routes}
    for raw in _encodings(TENANT_A):
        reg = registry([route_a(), route_a(tenant_id=raw)])
        assert reg.blocked == {}  # 같은 계정의 동치 표기는 소유권 충돌이 아니다
        assert {r.channel: r.fingerprint for r in reg.routes} == fp
        assert len(reg.routes) == 2
        assert reg.resolve(event(), "SLACK").route.tenant_id == TENANT_A
    # 다른 계정이어도 동치 표기는 같은 tenant 라 목적지 공유로 차단되지 않는다
    reg = registry([route_a(), route_a(tenant_id=TENANT_A.upper(), aws_account_id="333333333333")])
    assert reg.blocked == {} and len(reg.routes) == 4


def test_distinct_uuids_still_conflict_on_same_account():
    reg = registry([route_a(), route_a(tenant_id=TENANT_B)])
    assert reg.blocked[("111111111111", "")] == "OWNERSHIP_CONFLICT"


@pytest.mark.parametrize(
    "bad",
    [
        "", " ", "customer-a", TENANT_A[:-1], TENANT_A + "0", " " + TENANT_A, TENANT_A + " ", TENANT_A + "\n",
        "\t" + TENANT_A, TENANT_A.replace("-", "_"), TENANT_A[:-1] + "g", TENANT_A.replace("1", "１", 1),
        "{" + TENANT_A, TENANT_A + "}", "urn:uuid:", "uuid:" + TENANT_A, TENANT_A.replace("-", " "),
        TENANT_A.replace("-", "", 1), "1_" + TENANT_A[2:], "0x" + TENANT_A.replace("-", "")[2:],
        None, 1, 1.5, True, [], {}, TENANT_A.encode(),
    ],
)  # fmt: skip
def test_invalid_tenant_id_rejected_without_echo(bad):
    with pytest.raises(RouteConfigError) as exc:
        registry([route_a(tenant_id=bad)])
    assert str(exc.value) == "invalid route settings"
    assert exc.value.__cause__ is None
    if isinstance(bad, str) and bad.strip():
        assert bad not in str(exc.value)


def test_event_replace_roundtrip_unused():
    assert replace(event(), event_id="x").event_id == "x"
