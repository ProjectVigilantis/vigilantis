"""T-7 DTO의 부분 수정·상태 일관성·비밀값 경계를 검증한다."""

import pytest
from pydantic import SecretStr, ValidationError
from schemas.api.settings import (
    EmailNotificationStatus,
    NotificationSettings,
    NotificationSettingsUpdateRequest,
    ProtectedTagRule,
    TenantSettings,
    TenantSettingsUpdateRequest,
)
from schemas.slack import validate_slack_webhook_url

TENANT_ID = "730070dd-d418-4fea-9280-cf545aa1b783"
WEBHOOK = "https://hooks.slack.com/services/TTEAM/BCHANNEL/PrivateToken"
EMAIL = "observer@example.com"


def settings(**changes):
    data = {
        "tenant_id": TENANT_ID,
        "protected_tags": [{"key": "env", "values": ["prod", "production"]}],
        "notifications": {
            "slack_configured": False,
            "legacy_sns_configured": False,
            "email_address": None,
            "email_status": "UNSET",
        },
        "updated_at": "2026-10-30T03:00:00Z",
    }
    data.update(changes)
    return data


def test_response_round_trip_retains_the_effective_settings_and_utc_time():
    payload = settings()
    assert TenantSettings.model_validate(payload).model_dump(mode="json") == payload


def test_empty_protection_is_distinct_from_omission():
    clear = TenantSettingsUpdateRequest(protected_tags=[])
    assert clear.model_dump(exclude_unset=True) == {"protected_tags": []}
    channel = TenantSettingsUpdateRequest(notifications={"email_address": EMAIL})
    assert channel.model_dump(exclude_unset=True) == {
        "notifications": {"email_address": EMAIL},
    }


def test_json_schema_only_advertises_null_for_channel_clearing():
    schema = TenantSettingsUpdateRequest.model_json_schema()
    assert schema.get("required", []) == []
    assert schema["properties"]["protected_tags"]["type"] == "array"
    assert schema["properties"]["notifications"]["$ref"].endswith(
        "/NotificationSettingsUpdateRequest",
    )
    channels = schema["$defs"]["NotificationSettingsUpdateRequest"]["properties"]
    for name in ("slack_webhook_url", "email_address"):
        assert {"type": "null"} in channels[name]["anyOf"]


@pytest.mark.parametrize("field", ["email_address", "slack_webhook_url"])
def test_channel_null_clears_only_the_named_channel(field):
    request = TenantSettingsUpdateRequest(notifications={field: None})
    assert request.model_dump(exclude_unset=True) == {"notifications": {field: None}}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"protected_tags": None},
        {"notifications": None},
        {"notifications": {}},
        {"tenant_id": TENANT_ID},
        {"notifications": {"sns_topic_arn": "arn:aws:sns:ap-northeast-2:123456789012:x"}},
        {"notifications": {"slack_configured": True}},
        {"notifications": {"legacy_sns_configured": True}},
        {"notifications": {"email_status": "READY"}},
        {"protected_tags": [{"key": "env", "values": ["prod"], "threshold": 1}]},
        {"protected_tags": [{"key": "", "values": ["prod"]}]},
        {"protected_tags": [{"key": "env", "values": []}]},
        {"protected_tags": [{"key": "env", "values": [False]}]},
    ],
)
def test_invalid_or_server_owned_updates_are_rejected(payload):
    with pytest.raises(ValidationError):
        TenantSettingsUpdateRequest.model_validate(payload)


def test_tag_rules_preserve_strings_but_reject_equivalent_duplicates():
    rule = ProtectedTagRule(key="ENV", values=[" Prod ", "staging"])
    assert rule.model_dump() == {"key": "ENV", "values": [" Prod ", "staging"]}
    with pytest.raises(ValidationError):
        ProtectedTagRule(key="env", values=["prod", " PROD "])
    for model, payload in [
        (TenantSettingsUpdateRequest, {}),
        (TenantSettings, settings()),
    ]:
        with pytest.raises(ValidationError):
            model.model_validate({
                **payload,
                "protected_tags": [
                    {"key": "env", "values": ["prod"]},
                    {"key": "ENV", "values": ["staging"]},
                ],
            })


def test_empty_tag_value_can_match_an_empty_resource_tag():
    assert ProtectedTagRule(key="protected", values=[""]).values == [""]


@pytest.mark.parametrize(
    "status",
    ["SETUP_PENDING", "SETUP_FAILED", "CONFIRMATION_PENDING", "READY"],
)
def test_each_configured_email_state_requires_the_current_address(status):
    state = NotificationSettings(
        slack_configured=False, legacy_sns_configured=False,
        email_address=EMAIL, email_status=status,
    )
    assert state.model_dump(mode="json")["email_status"] == status
    with pytest.raises(ValidationError):
        NotificationSettings(
            slack_configured=False, legacy_sns_configured=False,
            email_address=None, email_status=status,
        )


def test_unset_requires_no_address_and_rejects_unknown_status():
    with pytest.raises(ValidationError):
        NotificationSettings(
            slack_configured=False, legacy_sns_configured=False,
            email_address=EMAIL, email_status="UNSET",
        )
    with pytest.raises(ValidationError):
        NotificationSettings(
            slack_configured=False, legacy_sns_configured=False,
            email_address=EMAIL, email_status="DELIVERED",
        )
    assert {item.value for item in EmailNotificationStatus} == {
        "UNSET", "SETUP_PENDING", "SETUP_FAILED", "CONFIRMATION_PENDING", "READY",
    }


def test_legacy_sns_configuration_does_not_infer_an_email_address_or_confirmation():
    payload = settings()
    payload["notifications"]["legacy_sns_configured"] = True
    assert TenantSettings.model_validate(payload).model_dump(mode="json") == payload
    for status in EmailNotificationStatus:
        if status is EmailNotificationStatus.UNSET:
            continue
        with pytest.raises(ValidationError):
            NotificationSettings(
                slack_configured=False, legacy_sns_configured=True,
                email_address=EMAIL, email_status=status,
            )


def test_response_requires_an_explicit_legacy_sns_configuration_flag():
    payload = settings()
    del payload["notifications"]["legacy_sns_configured"]
    with pytest.raises(ValidationError):
        TenantSettings.model_validate(payload)


@pytest.mark.parametrize("email", ["", "not-an-address", "a b@example.com", "a@@b", 12])
def test_email_inputs_reuse_the_existing_email_contract(email):
    with pytest.raises(ValidationError):
        NotificationSettingsUpdateRequest(email_address=email)


def test_webhook_remains_secret_in_nested_repr_and_json():
    request = TenantSettingsUpdateRequest(
        notifications={"slack_webhook_url": WEBHOOK},
    )
    secret = request.notifications.slack_webhook_url
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == WEBHOOK
    assert WEBHOOK not in repr(request)
    assert WEBHOOK not in request.model_dump_json()
    assert request.model_dump(mode="json", exclude_unset=True) == {
        "notifications": {"slack_webhook_url": "**********"},
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/services/T/B/secret",
        "https://example.com/services/T/B/secret",
        "https://hooks.slack.com.evil.test/services/T/B/secret",
        "https://hooks.slack.com@evil.test/services/T/B/secret",
        "https://hooks.slack.com/services/T/B/secret?query=private",
        "https://hooks.slack.com/services/T/B/secret#private",
        "https://hooks.slack.com/services/T/B/secret\n",
        "",
    ],
)
def test_invalid_webhook_never_appears_in_validation_messages(url):
    with pytest.raises(ValidationError) as error:
        TenantSettingsUpdateRequest(notifications={"slack_webhook_url": url})
    if url:
        assert url not in str(error.value)
    with pytest.raises(ValueError, match="^invalid slack webhook url$"):
        validate_slack_webhook_url(url)


def test_nested_errors_do_not_print_a_valid_webhook_in_the_same_payload():
    with pytest.raises(ValidationError) as error:
        TenantSettingsUpdateRequest(
            notifications={"slack_webhook_url": WEBHOOK},
            protected_tags=None,
        )
    assert WEBHOOK not in str(error.value)


@pytest.mark.parametrize("field", ["slack_webhook_url", "sns_topic_arn", "subscription_arn"])
def test_response_rejects_private_provider_fields(field):
    payload = settings()
    payload["notifications"][field] = WEBHOOK
    with pytest.raises(ValidationError) as error:
        TenantSettings.model_validate(payload)
    assert WEBHOOK not in str(error.value)


@pytest.mark.parametrize("field,value", [("tenant_id", "other"), ("updated_at", "yesterday")])
def test_response_rejects_invalid_identity_or_timestamp(field, value):
    with pytest.raises(ValidationError):
        TenantSettings.model_validate(settings(**{field: value}))
