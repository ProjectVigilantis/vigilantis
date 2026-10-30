"""Slack Incoming Webhook 형식 — API 입력과 알림 라우팅이 함께 사용한다."""

import re

_WEBHOOK_RE = re.compile(
    r"https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+"
)


def validate_slack_webhook_url(value: object) -> str:
    """허용된 Slack URL만 반환한다. 예외 메시지에는 비밀값을 넣지 않는다."""
    if not isinstance(value, str) or _WEBHOOK_RE.fullmatch(value) is None:
        raise ValueError("invalid slack webhook url")
    return value
