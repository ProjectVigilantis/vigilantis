"""실제 ASGI lifespan 배선과 opt-in/자원 정리. 외부 발송은 하지 않는다."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from config import Settings, get_settings
from notifications.runtime import NotificationRuntime, build_notification_runtime


@pytest.fixture(autouse=True)
def isolated_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_disabled_lifespan_never_builds_notification_clients(monkeypatch):
    import main

    monkeypatch.delenv("NOTIFICATION_ENABLED", raising=False)
    settings = Settings(_env_file=None, DATABASE_URL="postgresql+psycopg://unused/unused",
                        SCAN_ENABLED=False, DISPATCH_ENABLED=False)
    assert settings.NOTIFICATION_ENABLED is False
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    def forbidden(_):
        pytest.fail("비활성 앱이 수신처/비밀/외부 클라이언트를 읽었다")

    monkeypatch.setattr(main, "build_notification_runtime", forbidden)
    with TestClient(main.create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_real_lifespan_starts_and_closes_notification_runtime(monkeypatch):
    import main

    calls = []

    class Dispatcher:
        async def start(self):
            calls.append("started")

        async def stop(self):
            calls.append("stopped")

    class Sender:
        def close(self):
            calls.append("closed")

    runtime = NotificationRuntime(Dispatcher(), {"SLACK": Sender()})
    monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
    monkeypatch.setattr(main, "build_notification_runtime", lambda _: runtime)
    with TestClient(main.create_app()) as client:
        assert calls == ["started"]
        assert client.get("/health").status_code == 200
    assert calls == ["started", "stopped", "closed"]


def test_start_failure_closes_started_resources_and_existing_schedulers(monkeypatch, capsys):
    import main

    calls = []
    secret = "https://hooks.slack.com/services/FAKE/FAKE/SECRET"

    class Dispatcher:
        async def start(self):
            calls.append("notification-start")
            raise RuntimeError(secret)

        async def stop(self):
            calls.append("notification-stop")

    class Sender:
        def close(self):
            calls.append("sender-close")

    class Scheduler:
        def shutdown(self, wait=False):
            calls.append("scheduler-stop")

    runtime = NotificationRuntime(Dispatcher(), {"SNS": Sender()})
    monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
    monkeypatch.setattr(main, "build_notification_runtime", lambda _: runtime)
    monkeypatch.setattr(main, "start_scan_scheduler", lambda _: Scheduler())
    with pytest.raises(RuntimeError, match="NOTIFICATION_START_FAILED") as exc:
        with TestClient(main.create_app()):
            pass
    assert calls == ["notification-start", "notification-stop", "sender-close", "scheduler-stop"]
    assert secret not in str(exc.value)
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_stop_failure_still_closes_each_sender_and_other_app_resources(monkeypatch):
    import main

    calls = []

    class Dispatcher:
        async def start(self):
            pass

        async def stop(self):
            calls.append("stop")
            raise RuntimeError("controlled-stop-failure")

    class Sender:
        def __init__(self, name):
            self.name = name

        def close(self):
            calls.append(self.name)
            if self.name == "slack":
                raise RuntimeError("controlled-close-failure")

    class Scheduler:
        def shutdown(self, wait=False):
            calls.append("scheduler-stop")

    runtime = NotificationRuntime(Dispatcher(), {"SLACK": Sender("slack"), "SNS": Sender("sns")})
    monkeypatch.setenv("NOTIFICATION_ENABLED", "true")
    monkeypatch.setattr(main, "build_notification_runtime", lambda _: runtime)
    monkeypatch.setattr(main, "start_scan_scheduler", lambda _: Scheduler())
    with pytest.raises(RuntimeError, match="controlled-stop-failure"):
        with TestClient(main.create_app()):
            pass
    assert calls == ["stop", "slack", "sns", "scheduler-stop"]
    asyncio.run(runtime.stop())
    assert calls == ["stop", "slack", "sns", "scheduler-stop"]


@pytest.mark.parametrize(("changes", "code"), [
    ({"NOTIFICATION_ENABLED": False}, "NOTIFICATION_DISABLED"),
    ({"NOTIFICATION_ROUTES_FILE": ""}, "NOTIFICATION_ROUTES_REQUIRED"),
    ({"NOTIFICATION_MAX_BACKOFF_SECONDS": 1}, "NOTIFICATION_BACKOFF_INVALID"),
    ({"NOTIFICATION_LEASE_SECONDS": 1}, "NOTIFICATION_LEASE_TOO_SHORT"),
])
def test_invalid_runtime_configuration_fails_before_loading_routes(changes, code):
    values = {
        "NOTIFICATION_ENABLED": True,
        "NOTIFICATION_ROUTES_FILE": "not-opened.json",
        "NOTIFICATION_MAX_BACKOFF_SECONDS": 300,
        "NOTIFICATION_BACKOFF_SECONDS": 2,
        "NOTIFICATION_LEASE_SECONDS": 120,
        "NOTIFICATION_CONNECT_TIMEOUT_SECONDS": 3,
        "NOTIFICATION_READ_TIMEOUT_SECONDS": 5,
    }
    values.update(changes)
    with pytest.raises(ValueError, match=code):
        build_notification_runtime(SimpleNamespace(**values))


def test_test_bootstrap_overrides_external_opt_in_and_dotenv(tmp_path):
    """별도 프로세스에서 외부 env/.env가 실제 테스트 앱의 발송을 켜지 못함을 확인한다."""
    core = Path(__file__).resolve().parents[1]
    (tmp_path / ".env").write_text("NOTIFICATION_ENABLED=true\n", encoding="utf-8")
    env = {
        **os.environ,
        "NOTIFICATION_ENABLED": "true",
        "DISPATCH_ENABLED": "false",
        "SCAN_ENABLED": "false",
        "DATABASE_URL": "postgresql+psycopg://unused:unused@localhost:1/unused",
    }
    code = (
        "import runpy; "
        f"runpy.run_path({str(core / 'tests' / 'conftest.py')!r}); "
        "from config import get_settings; "
        "assert get_settings().NOTIFICATION_ENABLED is False; "
        "import main; "
        "from fastapi.testclient import TestClient; "
        "main.build_notification_runtime=lambda _: (_ for _ in ()).throw(AssertionError('send')); "
        "client=TestClient(main.create_app()); "
        "client.__enter__(); "
        "assert client.get('/health').status_code==200; "
        "client.__exit__(None,None,None)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
