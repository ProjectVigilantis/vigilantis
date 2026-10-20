# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 운영 기동 진입점(serve.py)의 접근 역할 프로필 회귀 테스트입니다. (ADR-0010)
#
# 프로필 파일이 Boto3가 실제로 읽는 모양인지까지 botocore로 확인한다 — 문자열만 맞고 키 이름이
# 틀리면 운영 태스크가 역할 전환 없이 태스크 역할(전환 권한만 있음)로 AWS를 불러 전부 거절된다.
# ==============================================================================

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

CORE_API = Path(__file__).resolve().parents[1]
if str(CORE_API) not in sys.path:
    sys.path.insert(0, str(CORE_API))

import serve  # noqa: E402

ROLE = "arn:aws:iam::111122223333:role/vigilantis-access"


def test_no_role_means_no_profile(tmp_path):
    environ = {"AWS_ACCESS_KEY_ID": "k"}
    assert serve.write_access_profile(environ, tmp_path) is False
    assert not (tmp_path / ".aws").exists()
    assert environ == {"AWS_ACCESS_KEY_ID": "k"}


def test_role_without_external_id_stops_startup(tmp_path):
    with pytest.raises(SystemExit, match="EXTERNAL_ID"):
        serve.write_access_profile({"VIGILANTIS_ACCESS_ROLE_ARN": ROLE}, tmp_path)


def test_profile_is_written_and_selected_and_static_keys_dropped(tmp_path):
    environ = {
        "VIGILANTIS_ACCESS_ROLE_ARN": ROLE,
        "VIGILANTIS_ACCESS_EXTERNAL_ID": "ext-123",
        "AWS_ACCESS_KEY_ID": "should-go",
        "AWS_SECRET_ACCESS_KEY": "should-go",
    }
    assert serve.write_access_profile(environ, tmp_path) is True
    assert environ["AWS_PROFILE"] == "vigilantis-access"
    assert Path(environ["AWS_CONFIG_FILE"]) == tmp_path / ".aws" / "config"
    assert "AWS_ACCESS_KEY_ID" not in environ and "AWS_SECRET_ACCESS_KEY" not in environ


def test_botocore_reads_the_profile_as_an_assume_role_profile(tmp_path, monkeypatch):
    import botocore.session

    environ = {"VIGILANTIS_ACCESS_ROLE_ARN": ROLE, "VIGILANTIS_ACCESS_EXTERNAL_ID": "ext-123"}
    serve.write_access_profile(environ, tmp_path)
    monkeypatch.setenv("AWS_CONFIG_FILE", environ["AWS_CONFIG_FILE"])
    profile = botocore.session.Session(profile="vigilantis-access").get_scoped_config()
    assert profile["role_arn"] == ROLE
    assert profile["external_id"] == "ext-123"
    assert profile["credential_source"] == "EcsContainer"
    assert profile["duration_seconds"] == "900"


def test_uvicorn_runs_one_worker_without_reload():
    argv = serve.uvicorn_argv("8000")
    assert argv[:2] == ["uvicorn", "main:app"]
    assert "--reload" not in argv
    assert argv[argv.index("--workers") + 1] == "1"


# ---- 알림 routes 파일화 (#493 PR3) -------------------------------------------
ROUTES = '{"routes":[{"tenant_id":"한글","aws_account_id":"111111111111","slack_webhook_env":"NOTIFICATION_SLACK_A"}]}'


def _env(**kw):
    return {"NOTIFICATION_ENABLED": "true", **kw}


def test_routes_json_becomes_a_file_the_runtime_registry_reads(tmp_path):
    from notifications.routing import RouteRegistry

    environ = _env(NOTIFICATION_ROUTES_JSON=ROUTES)
    assert serve.write_notification_routes(environ, tmp_path) is True
    path = Path(environ["NOTIFICATION_ROUTES_FILE"])
    assert path == tmp_path / ".vigilantis" / "notification-routes.json" and path.is_file()
    reg = RouteRegistry.from_file(str(path), {"NOTIFICATION_SLACK_A": "https://hooks.slack.com/services/T1/B1/X1"})
    assert [r.tenant_id for r in reg.routes] == ["한글"]
    assert "NOTIFICATION_ROUTES_JSON" not in environ


def test_disabled_or_absent_json_touches_nothing(tmp_path):
    for environ in ({"NOTIFICATION_ROUTES_JSON": ROUTES},
                    {"NOTIFICATION_ENABLED": "false", "NOTIFICATION_ROUTES_JSON": "{bad"}, _env()):
        before = dict(environ)
        assert serve.write_notification_routes(environ, tmp_path) is False
        assert environ == before
    assert not (tmp_path / ".vigilantis").exists()


def test_json_and_explicit_file_together_stop_startup(tmp_path):
    with pytest.raises(SystemExit, match="both set"):
        serve.write_notification_routes(_env(NOTIFICATION_ROUTES_JSON=ROUTES, NOTIFICATION_ROUTES_FILE="/x"), tmp_path)
    assert not (tmp_path / ".vigilantis").exists()


@pytest.mark.parametrize("raw", [
    "{not json SECRETVALUE",
    '{"routes":[{"tenant_id":"a","aws_account_id":"111111111111","slack_webhook":"https://hooks.slack.com/services/T/B/X"}]}',
    '{"routes":[],"routes":[]}',
    '{"routes":"x"}',
])
def test_invalid_json_fails_closed_without_persisting_or_echoing(tmp_path, raw):
    with pytest.raises(SystemExit) as exc:
        serve.write_notification_routes(_env(NOTIFICATION_ROUTES_JSON=raw), tmp_path)
    assert str(exc.value) == "NOTIFICATION_ROUTES_JSON is invalid"
    assert not (tmp_path / ".vigilantis").exists()


def test_main_wires_routes_before_exec(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_ROUTES_JSON", ROUTES)
    monkeypatch.delenv("NOTIFICATION_ROUTES_FILE", raising=False)
    monkeypatch.delenv("VIGILANTIS_ACCESS_ROLE_ARN", raising=False)
    monkeypatch.setattr(serve.os, "execvp", lambda *a: seen.update(file=os.environ["NOTIFICATION_ROUTES_FILE"]))
    serve.main()
    assert Path(seen["file"]).read_text(encoding="utf-8") == ROUTES
