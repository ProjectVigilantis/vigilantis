# ==============================================================================
# [파일 설명]
# 구조화 로깅 검증 — 접근 로그가 JSON 한 줄로, 약속된 필드만 남는지. (Issue #68)
# ==============================================================================

from __future__ import annotations

import json


def _access_log_lines(captured: str) -> list[dict]:
    lines = []
    for line in captured.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if parsed.get("event") == "http_request":
            lines.append(parsed)
    return lines


def test_access_log_is_json_with_expected_fields(client, capsys):
    response = client.get("/health")
    captured = capsys.readouterr().out
    logs = _access_log_lines(captured)
    assert logs, "접근 로그가 stdout에 남아야 한다"
    entry = logs[-1]
    assert entry["method"] == "GET"
    assert entry["path"] == "/health"
    assert entry["status_code"] == 200
    assert entry["request_id"] == response.headers["X-Request-ID"]
    assert isinstance(entry["duration_ms"], (int, float))


def test_access_log_excludes_request_payload(client, capsys):
    # 존재하지 않는 경로에 본문을 실어 보내도 로그에는 경로·상태만 남는다
    client.post("/api/v1/no-such-path", json={"secret_credential": "sk-do-not-log"})
    captured = capsys.readouterr().out
    assert "sk-do-not-log" not in captured
    logs = _access_log_lines(captured)
    assert logs and logs[-1]["path"] == "/api/v1/no-such-path"
