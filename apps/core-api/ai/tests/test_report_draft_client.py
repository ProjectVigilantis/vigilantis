"""신고 초안 모델 연결(#648) — 전용 설정이 있을 때만 연결하고, 분석 설정과 섞이지 않는다."""

import pytest
from ai import report_draft
from ai.model_client import AIModelError, AIModelRequest
from ai.openai_client import build_openai_model_client
from config import Settings
from pydantic import ValidationError
from schemas.api.report_draft import ReportNarrative

BASE = {"_env_file": None, "DATABASE_URL": "postgresql+psycopg://t:t@localhost:5432/t"}


def _use(monkeypatch, **values) -> Settings:
    # 명시 인자가 환경변수보다 우선하므로 None 주입이 "미설정"을 재현한다
    settings = Settings(**BASE, **values)
    monkeypatch.setattr(report_draft, "get_settings", lambda: settings)
    return settings


def test_not_connected_without_report_draft_model(monkeypatch):
    _use(monkeypatch, OPENAI_API_KEY="sk-test", REPORT_DRAFT_OPENAI_MODEL=None)

    assert report_draft.get_report_model_client() is None


def test_not_connected_when_key_is_missing(monkeypatch, caplog):
    _use(monkeypatch, OPENAI_API_KEY=None, REPORT_DRAFT_OPENAI_MODEL="gpt-6-luna")

    assert report_draft.get_report_model_client() is None
    assert "report_draft_model_key_missing" in caplog.text


def test_connected_client_uses_report_draft_knobs_not_analysis_ones(monkeypatch):
    settings = _use(
        monkeypatch,
        OPENAI_API_KEY="sk-test",
        OPENAI_MODEL="gpt-4o",
        OPENAI_TEMPERATURE=0,
        OPENAI_REASONING_EFFORT="unset",
        OPENAI_TIMEOUT_SECONDS=30,
        OPENAI_MAX_ATTEMPTS=3,
        REPORT_DRAFT_OPENAI_MODEL="gpt-6-luna",
    )

    client = report_draft.get_report_model_client()

    assert client._model == "gpt-6-luna"
    # 분석용 temperature가 새어 들지 않고 effort는 신고 초안 기본값(high)이다
    assert client._model_params == {"reasoning_effort": "high"}
    assert client._policy.max_attempts == 1
    # 분석 경로가 읽는 설정은 그대로다
    assert (settings.OPENAI_MODEL, settings.OPENAI_TEMPERATURE, settings.OPENAI_MAX_ATTEMPTS) == ("gpt-4o", 0, 3)


class _RecordingCompletions:
    def __init__(self):
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("no network in tests")


def _sdk_timeout_of_one_call(client):
    completions = _RecordingCompletions()
    client._client = type("SDK", (), {"chat": type("Chat", (), {"completions": completions})()})()
    with pytest.raises(AIModelError):
        client.complete(AIModelRequest(system_prompt="s", user_payload={"facts": []}), ReportNarrative)
    assert len(completions.calls) == 1  # 재시도 없음
    return completions.calls[0]["timeout"]


def test_sdk_phase_budget_stays_under_the_cloudfront_wait(monkeypatch):
    _use(
        monkeypatch,
        OPENAI_API_KEY="sk-test",
        REPORT_DRAFT_OPENAI_MODEL="gpt-6-luna",
        REPORT_DRAFT_OPENAI_TIMEOUT_SECONDS=50,
    )

    timeout = _sdk_timeout_of_one_call(report_draft.get_report_model_client())

    # SDK 제한 시간은 단계마다 따로 걸린다 — 단계 상한의 합을 CloudFront 응답 대기 60초보다 작게
    # 잡아 연결이 늦은 경우를 막는다. 응답 대기 상한은 읽기마다 걸리므로, 서버가 조금씩 나눠 보내는
    # 응답까지 60초 안을 보장하지는 않는다(받아들인 한계)
    assert timeout.read == 50
    assert timeout.connect + timeout.write + timeout.pool + timeout.read < 60


def test_analysis_client_keeps_one_timeout_for_every_phase():
    settings = Settings(**BASE, OPENAI_API_KEY="sk-test", OPENAI_TIMEOUT_SECONDS=30)

    assert _sdk_timeout_of_one_call(build_openai_model_client(settings)) == 30


def test_response_wait_must_leave_room_under_the_cloudfront_wait():
    with pytest.raises(ValidationError):
        Settings(**BASE, REPORT_DRAFT_OPENAI_TIMEOUT_SECONDS=51)
