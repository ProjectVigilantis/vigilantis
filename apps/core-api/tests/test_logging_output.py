# ==============================================================================
# [파일 설명]
# 구조화 로깅 검증 — 접근 로그가 JSON 한 줄로, 약속된 필드만 남는지. (Issue #68)
# 파일 출력(상한·회전·기동 실패)과 실행 문맥(log_context)의 합류·해제. (Issue #424)
# ==============================================================================

from __future__ import annotations

import json
import logging
import logging.config
from logging.handlers import RotatingFileHandler
from pathlib import Path

import logging_config
import main
import pytest
import uvicorn.config
from config import Settings, get_settings
from logging_config import log_context, resolve_log_file_path, setup_logging
from pydantic import ValidationError


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


# ------------------------------------------------------------------------------
# 파일 출력·실행 문맥 (Issue #424)
# ------------------------------------------------------------------------------


@pytest.fixture
def restore_logging():
    """파일 핸들러를 붙인 테스트 뒤에 stdout 구성으로 되돌린다 — 임시 파일을 닫는다."""
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).level,
        )
        for name in names
    }
    yield
    setup_logging("INFO")
    for name, (handlers, propagate, level) in saved.items():
        target = logging.getLogger(name)
        target.handlers[:] = handlers
        target.propagate = propagate
        target.setLevel(level)


def _file_handlers():
    return [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]


def _json_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_test_app_never_writes_a_log_file(client):
    # conftest가 LOG_FILE_ENABLED를 끈다 — .env가 켜 두었어도 테스트 앱이 개발 스택의
    # 로그 파일을 함께 회전시키지 않는다
    assert get_settings().LOG_FILE_ENABLED is False
    assert _file_handlers() == []


def test_file_output_writes_the_same_json_lines(tmp_path, restore_logging, capsys):
    log_file = tmp_path / "nested" / "api.log"  # 부모 폴더가 없어도 만든다
    setup_logging("INFO", log_file_path=str(log_file))

    logging.getLogger("vigilantis.test").info("file_probe", extra={"value": 1})
    for handler in _file_handlers():
        handler.flush()

    lines = _json_lines(log_file)
    # 기동 로그가 어느 파일에 쓰는지 밝힌다 — 켜졌는지 로그로 진단할 수 있다
    assert lines[0]["event"] == "log_file_enabled"
    assert lines[0]["log_file"] == str(log_file)
    assert lines[-1]["event"] == "file_probe" and lines[-1]["value"] == 1
    # stdout에도 같은 줄이 나간다 — 파일은 추가 출력이다
    assert '"event": "file_probe"' in capsys.readouterr().out


def test_file_output_rotates_within_the_size_cap(tmp_path, restore_logging):
    log_file = tmp_path / "api.log"
    setup_logging("INFO", log_file_path=str(log_file), max_bytes=2_000, backup_count=2)

    probe = logging.getLogger("vigilantis.test")
    for index in range(200):
        probe.info("rotation_probe", extra={"index": index})

    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == ["api.log", "api.log.1", "api.log.2"]
    assert all(path.stat().st_size <= 2_000 for path in tmp_path.iterdir())


def test_backup_count_default_is_20_and_must_be_positive():
    assert Settings(DATABASE_URL="postgresql://x").LOG_FILE_BACKUP_COUNT == 20
    assert Settings(DATABASE_URL="postgresql://x", LOG_FILE_BACKUP_COUNT=3).LOG_FILE_BACKUP_COUNT == 3
    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            Settings(DATABASE_URL="postgresql://x", LOG_FILE_BACKUP_COUNT=invalid)


def test_create_app_passes_backup_count_to_setup_logging(monkeypatch, tmp_path):
    calls = []
    settings = Settings(
        DATABASE_URL="postgresql://x",
        LOG_FILE_ENABLED=True,
        LOG_FILE_PATH=str(tmp_path / "api.log"),
        LOG_FILE_BACKUP_COUNT=7,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **kw: calls.append((a, kw)))
    main.create_app()
    assert calls == [
        (
            ("INFO",),
            {"log_file_path": str(tmp_path / "api.log"), "backup_count": 7},
        )
    ]

    calls.clear()
    monkeypatch.setattr(main, "get_settings", lambda: settings.model_copy(update={"LOG_FILE_ENABLED": False}))
    main.create_app()
    assert calls[0][1]["log_file_path"] is None  # 꺼 두면 파일 경로가 넘어가지 않는다


def test_rotation_keeps_exactly_backup_count_uncompressed_files(tmp_path, restore_logging):
    setup_logging("INFO", log_file_path=str(tmp_path / "api.log"), max_bytes=500, backup_count=3)
    probe = logging.getLogger("vigilantis.test")
    for index in range(300):
        probe.info("retention_probe", extra={"index": index})

    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == ["api.log", "api.log.1", "api.log.2", "api.log.3"]  # 압축(.gz) 없음


def test_uvicorn_errors_reach_root_json_handlers_once(tmp_path, restore_logging, capsys):
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    log_file = tmp_path / "api.log"
    setup_logging("INFO", log_file_path=str(log_file))

    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.info("uvicorn_probe")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        uvicorn_error.exception("uvicorn_exception_probe")
    for handler in _file_handlers():
        handler.flush()

    captured = capsys.readouterr()
    stdout_events = [json.loads(line) for line in captured.out.splitlines()]
    assert [e["event"] for e in stdout_events].count("uvicorn_probe") == 1
    exception_lines = [e for e in stdout_events if e["event"] == "uvicorn_exception_probe"]
    assert len(exception_lines) == 1
    assert exception_lines[0]["logger"] == "uvicorn.error"
    assert "RuntimeError: boom" in exception_lines[0]["exc_info"]
    assert captured.err == ""  # uvicorn 자체 평문 핸들러로 새지 않는다
    file_events = [e["event"] for e in _json_lines(log_file)]
    assert file_events.count("uvicorn_probe") == 1
    assert file_events.count("uvicorn_exception_probe") == 1


def test_uvicorn_access_log_stays_suppressed(restore_logging, capsys):
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    setup_logging("INFO")
    access = logging.getLogger("uvicorn.access")
    access.info('127.0.0.1 - "GET /health HTTP/1.1" 200')
    captured = capsys.readouterr()
    assert "GET /health" not in captured.out + captured.err


def test_repeated_configuration_neither_duplicates_lines_nor_leaks_handlers(
    tmp_path, restore_logging, capsys
):
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    log_file = tmp_path / "api.log"
    previous = []
    for _ in range(3):
        setup_logging("INFO", log_file_path=str(log_file))
        previous.extend(_file_handlers())
    capsys.readouterr()

    logging.getLogger("uvicorn.error").info("repeat_probe")
    for handler in _file_handlers():
        handler.flush()

    root_handlers = logging.getLogger().handlers
    assert sum(isinstance(h, logging_config._StdoutHandler) for h in root_handlers) == 1
    assert len(_file_handlers()) == 1
    assert logging.getLogger("uvicorn").handlers == []
    assert [h.stream for h in previous[:-1]] == [None, None]  # 이전 파일 핸들은 닫혔다
    assert capsys.readouterr().out.count('"event": "repeat_probe"') == 1
    assert [e["event"] for e in _json_lines(log_file)].count("repeat_probe") == 1


def test_file_disabled_adds_no_file_handler_and_keeps_uvicorn_routing(restore_logging, capsys):
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    setup_logging("INFO", log_file_path=None)
    logging.getLogger("uvicorn.error").warning("stdout_only_probe")
    assert _file_handlers() == []
    assert capsys.readouterr().out.count('"event": "stdout_only_probe"') == 1


def test_reconfiguring_closes_the_previous_log_file(tmp_path, restore_logging):
    # create_app이 반복 호출돼도 파일 핸들이 쌓이지 않는다
    setup_logging("INFO", log_file_path=str(tmp_path / "api.log"))
    (previous,) = _file_handlers()

    setup_logging("INFO")

    assert _file_handlers() == []
    assert previous.stream is None  # FileHandler.close()가 스트림을 놓는다


def test_unopenable_log_file_stops_startup(tmp_path, restore_logging):
    # 켜 둔 파일 출력이 조용히 빠지지 않는다 — 폴더를 가리키면 기동 단계에서 실패한다
    with pytest.raises(OSError):
        setup_logging("INFO", log_file_path=str(tmp_path))


def test_relative_log_path_resolves_under_core_api():
    # 실행 위치(컨테이너 WORKDIR·저장소 루트)와 무관하게 compose가 공유하는 폴더를 가리킨다
    core_api = Path(logging_config.__file__).resolve().parent
    assert resolve_log_file_path(".logs/api.log") == core_api / ".logs" / "api.log"
    assert resolve_log_file_path(str(core_api / "x.log")) == core_api / "x.log"


def test_log_context_fields_join_lines_until_the_block_ends(capsys, restore_logging):
    setup_logging("INFO")
    probe = logging.getLogger("vigilantis.test")
    with log_context(incident_id="inc-1", analysis_step=None):
        with log_context(analysis_step="summarize_evidence"):
            probe.warning("inner")
        probe.warning("outer", extra={"incident_id": "explicit"})
    probe.warning("after")

    lines = {line["event"]: line for line in map(json.loads, capsys.readouterr().out.splitlines())}
    assert lines["inner"]["incident_id"] == "inc-1"
    assert lines["inner"]["analysis_step"] == "summarize_evidence"
    # None은 싣지 않는다 — 안쪽 블록이 끝나면 바깥 문맥만 남는다
    assert "analysis_step" not in lines["outer"]
    # 같은 키면 레코드의 extra가 문맥보다 우선한다
    assert lines["outer"]["incident_id"] == "explicit"
    assert "incident_id" not in lines["after"]
