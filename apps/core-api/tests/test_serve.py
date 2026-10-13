# ==============================================================================
# [파일 설명]  담당: 김세혁 (Infra & DevSecOps)
# 운영 기동 진입점(serve.py)의 접근 역할 프로필 회귀 테스트입니다. (ADR-0010)
#
# 프로필 파일이 Boto3가 실제로 읽는 모양인지까지 botocore로 확인한다 — 문자열만 맞고 키 이름이
# 틀리면 운영 태스크가 역할 전환 없이 태스크 역할(전환 권한만 있음)로 AWS를 불러 전부 거절된다.
# ==============================================================================

from __future__ import annotations

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
