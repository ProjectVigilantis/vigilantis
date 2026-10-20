"""알림 본문 렌더링. 원본 로그·예외·AI 요약·자유 사유는 입력에 없고 넣지도 않는다.

신뢰할 수 없는 문자열(저장된 ID·ARN·enum 값)은 제어문자 제거·길이 제한 후에만 쓴다.
Slack은 plain_text 블록만 쓰고 &,<,>를 한 번만 이스케이프한다(멘션·링크 구문 무력화).
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from urllib.parse import quote

from notifications.contracts import EventSnapshot, Route

SLACK_FALLBACK_TEXT = "Vigilantis notification"
SNS_SUBJECT = "Vigilantis notification"  # SNS Subject는 ASCII·<100자·개행 없음

_ID_MAX = 64
_ARN_MAX = 200
_ENUM_MAX = 40

_OUTCOME_LABELS = {
    "SUCCESS": "성공",
    "FAILED": "실패",
    "REJECTED": "거부",
    "UNVERIFIED": "결과 미확인",
}
_ACTION_LABELS = {"AUTO_ISOLATION": "자동 격리", "AUTO_ROLLBACK": "자동 원복"}


def clean(value: object, limit: int) -> str:
    """제어·서식 문자를 공백으로 바꾸고 공백을 접은 뒤 limit자로 자른다."""
    text = "".join(
        " " if unicodedata.category(ch)[0] in "CZ" else ch for ch in str(value)
    )
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def escape_slack(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _title(event: EventSnapshot) -> str:
    kind, phase, outcome = event.kind, event.phase, event.outcome
    if kind == "HIGH_INCIDENT_CREATED" and phase == "CREATED":
        return "High 위험 인시던트 생성"
    label = _ACTION_LABELS.get(kind)
    if label is not None and phase == "REQUESTED":
        return f"{label} 요청"
    if label is not None and phase == "RESULT" and outcome in _OUTCOME_LABELS:
        return f"{label} 결과: {_OUTCOME_LABELS[outcome]}"
    return "알림 이벤트 " + clean(f"{kind}/{phase}/{outcome}", 3 * _ENUM_MAX)


def _when(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.isoformat()


def incident_url(public_base_url: str, incident_id: str) -> str:
    return f"{public_base_url.rstrip('/')}/incidents/{quote(incident_id, safe='')}"


def build_lines(
    event: EventSnapshot, route: Route, public_base_url: str
) -> tuple[str, list[str]]:
    """(제목, 한 줄 필드 목록). 값은 정제됐지만 채널별 이스케이프 전이다."""
    rollback = event.kind == "AUTO_ROLLBACK"
    fields: list[tuple[str, str | None, int]] = [
        ("고객", route.tenant_id, _ID_MAX),
        ("AWS 계정", route.aws_account_id, _ID_MAX),
        ("리전", event.region, _ENUM_MAX),
        ("리소스", event.target_arn, _ARN_MAX),
        ("종류", event.kind, _ENUM_MAX),
        ("단계", event.phase, _ENUM_MAX),
        ("결과", event.outcome, _ENUM_MAX),
        ("발생 시각", _when(event.occurred_at), _ENUM_MAX),
        ("이벤트 ID", event.event_id, _ID_MAX),
        ("인시던트 ID", event.incident_id, _ID_MAX),
        ("원본 실행 ID" if rollback else "실행 ID", event.execution_id, _ID_MAX),
        ("원복 실행 ID", event.related_execution_id if rollback else None, _ID_MAX),
        ("실행 상태(실행 ID 기준)", event.execution_status, _ENUM_MAX),
        ("트리거", event.trigger_source, _ENUM_MAX),
        ("런북", event.runbook_id, _ENUM_MAX),
    ]
    lines = [
        f"{label}: {clean(value, limit)}"
        for label, value, limit in fields
        if value is not None
    ]
    if len(event.incident_id) <= _ID_MAX:  # 비정상적으로 긴 ID로 잘못된 링크를 만들지 않는다
        lines.append(f"상세: {incident_url(public_base_url, event.incident_id)}")
    return _title(event), lines


def slack_payload(event: EventSnapshot, route: Route, public_base_url: str) -> dict:
    title, lines = build_lines(event, route, public_base_url)

    def text(value: str) -> dict:
        return {"type": "plain_text", "text": escape_slack(value), "emoji": False}

    return {
        "text": SLACK_FALLBACK_TEXT,
        "mrkdwn": False,
        "unfurl_links": False,
        "unfurl_media": False,
        "blocks": [
            {"type": "header", "text": text(title)},
            *({"type": "section", "text": text(line)} for line in lines),
        ],
    }


def sns_body(event: EventSnapshot, route: Route, public_base_url: str) -> str:
    title, lines = build_lines(event, route, public_base_url)
    return "\n".join([title, "", *lines])
