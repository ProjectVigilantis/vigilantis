"""FE 관찰용 테스트 서버 리모콘(scripts/stepper/cli.py·server.py)의 판정·순서 테스트.

Docker·DB·네트워크 없이 돈다. 바깥 세계(subprocess·HTTP·포트 탐지)는 대역으로 바꾸고,
안전장치가 지켜야 하는 것을 본다 — compose 명령의 프로젝트 고정, 렌더링된 설정의 loopback
검사, 남의 스택이 포트를 잡으면 시드·주입기·HTTP를 하나도 부르지 않는 것, compose 실패 뒤
다음 단계로 가지 않는 것, 토큰이 출력·기록에 남지 않는 것. 실제 compose 병합·기동은 로컬
스모크 몫이다(scripts/stepper/README.md).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STEPPER = REPO_ROOT / "scripts" / "stepper"


def _load(name: str, path: Path):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


cli = _load("stepper_cli", STEPPER / "cli.py")
server = cli.server

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 23, 20, 40, 34, 987654, tzinfo=KST)
TOKEN = "f" * 48
ACCOUNT_ARN = "arn:aws:ec2:ap-northeast-2:000000000000:"
IDLE_DEV = ACCOUNT_ARN + "instance/i-0000000000000dev1"
IDLE = ACCOUNT_ARN + "instance/i-00000000000000id1"
UNUSED_SG = ACCOUNT_ARN + "security-group/sg-0000000000000uu1"

SAFE_CONFIG = {
    "name": "vigilantis-test",
    "services": {
        "db": {"ports": [{"host_ip": "127.0.0.1", "published": "5432", "target": 5432}]},
        "localstack": {"ports": [{"host_ip": "127.0.0.1", "published": "4566", "target": 4566}]},
        "migrate": {},
        "api": {"ports": [{"host_ip": "127.0.0.1", "published": "8000", "target": 8000}]},
    },
    "volumes": {"pgdata": {"name": "vigilantis-test_pgdata"}},
}


def _ps_line(name: str, project: str | None, service: str, ports: str, working_dir: str = "") -> str:
    labels = [f"com.docker.compose.service={service}"]
    if project:
        labels += [
            f"com.docker.compose.project={project}",
            f"com.docker.compose.project.working_dir={working_dir}",
            # 값에 쉼표가 든 라벨 — 조각나도 읽는 라벨에는 영향이 없어야 한다
            "com.docker.compose.project.config_files=C:\\repo\\docker-compose.yml,C:\\repo\\override.yml",
        ]
    return json.dumps({"Names": name, "Ports": ports, "Labels": ",".join(labels), "State": "running"})


DEV_STACK = "\n".join([
    _ps_line("vigilantis-db-1", "vigilantis", "db", "0.0.0.0:5432->5432/tcp, [::]:5432->5432/tcp"),
    _ps_line("vigilantis-localstack-1", "vigilantis", "localstack",
             "0.0.0.0:4566->4566/tcp, [::]:4566->4566/tcp, 4510-4559/tcp"),
])


def _ours(repo_root: Path) -> str:
    return "\n".join([
        _ps_line("vigilantis-test-db-1", "vigilantis-test", "db", "127.0.0.1:5432->5432/tcp", str(repo_root)),
        _ps_line("vigilantis-test-localstack-1", "vigilantis-test", "localstack",
                 "127.0.0.1:4566->4566/tcp, 4510-4559/tcp", str(repo_root)),
        _ps_line("vigilantis-test-api-1", "vigilantis-test", "api", "127.0.0.1:8000->8000/tcp", str(repo_root)),
    ])


class FakeRunner:
    """subprocess.run 대역 — 부른 명령을 모으고 docker ps·compose config에 정해 둔 출력을 준다."""

    def __init__(self, *, ps: str = "", failing: tuple[str, ...] = ()) -> None:
        self.ps = ps
        self.failing = failing
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append((args, kwargs))
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, stdout=self.ps, stderr="")
        if "config" in args:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(SAFE_CONFIG), stderr="")
        if any(marker in " ".join(args) for marker in self.failing):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="실패 재현")
        return subprocess.CompletedProcess(args, 0, stdout="[seed] 완료\n", stderr="")

    def commands(self) -> list[str]:
        return [" ".join(args) for args, _ in self.calls]

    def beyond_inspection(self) -> list[str]:
        """docker ps·compose config(읽기) 말고 부른 것 — 시드·주입기·compose 변경."""
        return [
            command for command in self.commands()
            if not command.startswith("docker ps") and " config --format json" not in command
        ]


class FakeRemote:
    """테스트 서버 HTTP 대역. 만들어진 것 자체를 기록해 '소유 확인 전에 HTTP 없음'을 본다."""

    created: ClassVar[list[FakeRemote]] = []

    def __init__(self, base_url: str, token: str, *, snap: dict | None = None) -> None:
        self.base_url = base_url
        self.token = token
        self.snap = snap or {"incidents": [], "pending_analysis": [], "assets": []}
        self.calls: list = []
        FakeRemote.created.append(self)

    def healthy(self) -> bool:
        return True

    def ping(self) -> dict:
        self.calls.append("ping")
        return {"server": server.SERVER_ID}

    def get(self, path: str, *, auth: bool = False):
        self.calls.append(path)
        if path == "/api/v1/incidents":
            return {"items": self.snap["incidents"]}
        if path.startswith("/api/v1/incidents/"):
            return next(i for i in self.snap["incidents"] if path.endswith(i["incident_id"]))
        if path == "/_stepper/pending-analysis":
            return {"incidents": self.snap["pending_analysis"]}
        if path == "/api/v1/assets":
            return {"items": self.snap["assets"]}
        raise AssertionError(path)

    def press(self, button: str, *, timeout: float) -> dict:
        self.calls.append(("press", button))
        return {"button": button, "ok": True, "elapsed_seconds": 0.1,
                "report": {"scanned": 0, "errored": 0}}


@pytest.fixture(autouse=True)
def _fresh_remotes():
    FakeRemote.created.clear()


@pytest.fixture()
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "stepper").mkdir(parents=True)
    return root


def _ctx(tmp_path: Path, repo: Path, runner: FakeRunner, *, snap: dict | None = None, answers=False):
    output: list[str] = []
    popen_calls: list = []

    def popen(*args, **kwargs):
        popen_calls.append(args)
        raise AssertionError("실패 주입기를 띄웠다")

    ctx = server.Context(
        data_root=tmp_path / "data",
        repo_root=repo,
        run=runner,
        popen=popen,
        confirm=lambda prompt: True,
        remote_factory=lambda url, token: FakeRemote(url, token, snap=snap),
        port_answers=lambda port: answers,
        out=output.append,
        now=lambda: NOW,
        sleep=lambda seconds: None,
    )
    return ctx, output, popen_calls


def _running_round(ctx, repo: Path) -> None:
    """떠 있는 테스트 서버의 데이터 루트 — 토큰·회차·포트."""
    state = server.State(ctx.data_root)
    ctx.data_root.mkdir(parents=True)
    (ctx.data_root / "token").write_text(TOKEN, encoding="ascii")
    state.save(round="20260923-200000", ports=dict(server.DEFAULT_PORTS), repo_root=str(repo))


# ------------------------------------------------------------------------------
# compose 명령과 렌더링된 설정
# ------------------------------------------------------------------------------


def test_compose_commands_pin_project_and_override_and_pass_token_only_by_env(repo):
    runner = FakeRunner()
    server.Docker(runner, repo).compose("down", "-v", token=TOKEN)
    (args, kwargs), = runner.calls
    assert args[:4] == ["docker", "compose", "-p", "vigilantis-test"]
    files = [args[i + 1] for i, arg in enumerate(args) if arg == "-f"]
    assert files == [str(repo / "docker-compose.yml"),
                     str(repo / "scripts" / "stepper" / "compose.override.yml")]
    assert args[-2:] == ["down", "-v"]
    assert kwargs["env"]["STEPPER_TOKEN"] == TOKEN
    assert TOKEN not in " ".join(args)


def test_safe_rendered_config_gives_the_published_ports():
    ports, problems = server.check_rendered_config(SAFE_CONFIG)
    assert problems == []
    assert ports == {"db": 5432, "localstack": 4566, "api": 8000}


@pytest.mark.parametrize("mutate", [
    # !override를 모르는 Compose — 기본 파일의 포트가 덧붙어 모든 인터페이스에 열린다
    lambda c: c["services"]["api"]["ports"].insert(0, {"host_ip": "", "published": "8000"}),
    lambda c: c["services"]["db"]["ports"].__setitem__(0, {"host_ip": "0.0.0.0", "published": "5432"}),
    lambda c: c.__setitem__("name", "vigilantis"),
    lambda c: c["volumes"].__setitem__("pgdata", {"name": "vigilantis_pgdata"}),
    lambda c: c["volumes"]["pgdata"].__setitem__("external", True),
    lambda c: c["services"]["localstack"].pop("ports"),
])
def test_unsafe_rendered_config_is_refused(mutate):
    config = json.loads(json.dumps(SAFE_CONFIG))
    mutate(config)
    _, problems = server.check_rendered_config(config)
    assert problems


def test_override_file_binds_every_port_to_loopback_and_disables_timers():
    yaml = pytest.importorskip("yaml")

    class Loader(yaml.SafeLoader):
        pass

    # !override·!reset은 Compose 병합 태그다 — 표시만 남기고 값은 그대로 읽는다
    Loader.add_constructor("!override", lambda loader, node: {"!override": loader.construct_sequence(node)})
    override = yaml.load((STEPPER / "compose.override.yml").read_text(encoding="utf-8"), Loader=Loader)
    for name, service in override["services"].items():
        if "ports" in service:
            ports = service["ports"]["!override"]
            assert ports and all(str(port).startswith("127.0.0.1:") for port in ports), name
    assert {"db", "localstack", "api"} <= set(override["services"])
    env = override["services"]["api"]["environment"]
    assert env["SCAN_ENABLED"] == "false" and env["DISPATCH_ENABLED"] == "false"
    assert env["MOCK_THREAT_INBOX_DIR"] == ""
    assert env["STEPPER_TOKEN"].startswith("${STEPPER_TOKEN:?")
    assert env["AWS_ENDPOINT_URL"] == "http://localstack:4566"


# ------------------------------------------------------------------------------
# 포트 소유 — 남의 스택이 잡고 있으면 아무것도 하지 않는다
# ------------------------------------------------------------------------------


def test_ps_parsing_reads_host_ports_and_compose_labels():
    containers = server.parse_ps(DEV_STACK + "\n" + _ps_line("x", None, "", "0.0.0.0:8000-8001->80-81/tcp"))
    db, localstack, other = containers
    assert (db.project, db.service, db.host_ports) == ("vigilantis", "db", {5432})
    assert localstack.host_ports == {4566}  # 호스트에 열지 않은 4510-4559는 세지 않는다
    assert other.project is None and other.host_ports == {8000, 8001}


def test_foreign_holders_name_the_other_project_and_non_compose_containers(repo):
    containers = server.parse_ps(
        DEV_STACK + "\n" + _ps_line("proxy", None, "", "127.0.0.1:8000->80/tcp") + "\n" + _ours(repo)
    )
    held = server.foreign_holders(containers, server.DEFAULT_PORTS.values())
    assert held == [
        "5432번 포트 — compose 프로젝트 'vigilantis'의 vigilantis-db-1",
        "4566번 포트 — compose 프로젝트 'vigilantis'의 vigilantis-localstack-1",
        "8000번 포트 — compose 밖의 컨테이너의 proxy",
    ]
    assert server.foreign_holders(server.parse_ps(_ours(repo)), server.DEFAULT_PORTS.values()) == []


@pytest.mark.parametrize("argv", [
    ["status"],
    ["collect"],
    ["scan"],
    ["inject", "ssh", "--case", "C01", "--target", "vigilantis-seed-idle"],
    ["inject", "golden", "evt_open_ip_001", "--target", "vigilantis-seed-open-ssh"],
    ["analyze", "-y"],
    ["dispatch"],
    ["dispatch", "--fail-status-check", "vigilantis-seed-idle-dev"],
    ["up"],
    ["reset"],
])
def test_every_command_refuses_before_touching_a_foreign_stack(tmp_path, repo, argv):
    runner = FakeRunner(ps=DEV_STACK)
    ctx, output, popen_calls = _ctx(tmp_path, repo, runner)
    _running_round(ctx, repo)

    assert cli.main(argv, ctx) == 2
    assert "compose 프로젝트 'vigilantis'의 vigilantis-db-1" in "\n".join(output)
    # 읽기(docker ps·compose config)만 했다 — 시드·주입 준비·compose 변경·HTTP·주입기 없음
    assert runner.beyond_inspection() == []
    assert FakeRemote.created == [] and popen_calls == []


def test_process_outside_docker_on_a_test_port_blocks_start(tmp_path, repo):
    runner = FakeRunner(ps="")
    ctx, output, _ = _ctx(tmp_path, repo, runner, answers=True)
    assert cli.main(["reset"], ctx) == 2
    assert "compose 밖의 프로세스" in "\n".join(output)
    assert runner.beyond_inspection() == []


def test_server_started_from_another_checkout_is_refused(tmp_path, repo):
    other = tmp_path / "other-checkout"
    runner = FakeRunner(ps=_ours(other))
    ctx, output, _ = _ctx(tmp_path, repo, runner)
    _running_round(ctx, repo)
    assert cli.main(["scan"], ctx) == 2
    assert "다른 체크아웃" in "\n".join(output)
    assert FakeRemote.created == []


# ------------------------------------------------------------------------------
# 초기화 순서
# ------------------------------------------------------------------------------


def test_reset_stops_at_the_first_failed_compose_step(tmp_path, repo):
    runner = FakeRunner(failing=("--wait db localstack",))
    ctx, _output, _ = _ctx(tmp_path, repo, runner)
    assert cli.main(["reset"], ctx) == 2
    commands = runner.commands()
    assert any(" down -v " in f"{command} " for command in commands)
    assert not any("seed_localstack.py" in command for command in commands)
    assert not any(command.endswith("--wait api") for command in commands)
    assert FakeRemote.created == []


def test_reset_runs_the_documented_order_and_keeps_the_token_out_of_output(tmp_path, repo):
    inbox = server.host_inbox(repo)
    (inbox / "done").mkdir(parents=True)
    (inbox / "done" / "old.json").write_text("{}", encoding="utf-8")
    ctx, output, _ = _ctx(tmp_path, repo, FakeRunner())
    state = server.State(ctx.data_root)
    state.save(round="20260923-190000")

    assert cli.main(["reset"], ctx) == 0
    commands = ctx.run.commands()
    order = [
        next(i for i, c in enumerate(commands) if c.endswith("down -v --remove-orphans")),
        next(i for i, c in enumerate(commands) if c.endswith("--wait db localstack")),
        next(i for i, c in enumerate(commands) if "seed_localstack.py" in c),
        next(i for i, c in enumerate(commands) if c.endswith("--wait api")),
    ]
    assert order == sorted(order)
    seed_env = next(kwargs["env"] for args, kwargs in ctx.run.calls if "seed_localstack.py" in " ".join(args))
    assert seed_env["AWS_ENDPOINT_URL"] == "http://127.0.0.1:4566"
    # 이전 회차의 inbox는 지우지 않고 그 회차 기록으로 옮긴다
    assert (state.round_dir("20260923-190000") / "inbox" / "done" / "old.json").exists()
    assert inbox.is_dir() and not any(inbox.iterdir())
    token = state.token()
    assert token and token != TOKEN
    records = list(ctx.data_root.rglob("*.jsonl")) + list(ctx.data_root.rglob("state.json"))
    assert records
    assert all(token not in path.read_text(encoding="utf-8") for path in records)
    assert token not in "\n".join(output)


def test_archive_refuses_anything_but_the_stepper_inbox(tmp_path):
    with pytest.raises(server.Refused):
        server.archive_inbox(tmp_path / "apps" / "core-api" / ".mock-threat-inbox", tmp_path / "out")


# ------------------------------------------------------------------------------
# 버튼 판정 — 대상 이름·관측 시각·예상 호출 수·다음 동작
# ------------------------------------------------------------------------------

ASSETS = [
    {"arn": IDLE, "name": "vigilantis-seed-idle"},
    {"arn": IDLE_DEV, "name": "vigilantis-seed-idle-dev"},
    {"arn": UNUSED_SG, "name": "vigilantis-seed-unused"},
]


def test_target_name_resolves_only_when_exactly_one_asset_matches():
    assert cli.resolve_target(ASSETS, "vigilantis-seed-idle") == IDLE
    for assets, name in (
        ([], "vigilantis-seed-idle"),
        (ASSETS, "vigilantis-seed"),
        (ASSETS + [{"arn": IDLE + "x", "name": "vigilantis-seed-idle"}], "vigilantis-seed-idle"),
    ):
        with pytest.raises(server.Refused):
            cli.resolve_target(assets, name)


def test_observation_time_is_the_press_time_in_utc_whole_seconds():
    assert cli.utc_seconds(NOW) == "2026-09-23T11:40:34Z"


def test_expected_model_calls_follow_the_measured_per_card_counts():
    pending = [
        {"category": "FINOPS", "subject_arn": IDLE_DEV},   # 근거 요약 · 추천 · 절감 단가
        {"category": "FINOPS", "subject_arn": UNUSED_SG},  # 근거 요약 · 추천
        {"category": "FINOPS", "subject_arn": ACCOUNT_ARN + "volume/vol-1"},
        {"category": "SECOPS", "subject_arn": IDLE},
    ]
    assert cli.estimate_calls(pending[:3]) == 7
    assert cli.estimate_calls(pending) == 10


def _incident(incident_id: str, category: str, status: str, subject: str, executions=()):
    return {
        "incident_id": incident_id, "category": category, "status": status, "subject_arn": subject,
        "executions": list(executions), "created_at": "2026-09-23T11:00:00Z",
    }


def _snap(incidents=(), pending=(), names=None):
    return {
        "incidents": list(incidents),
        "pending_analysis": list(pending),
        "asset_names": {asset["arn"]: asset["name"] for asset in ASSETS} if names is None else names,
    }


def test_next_actions_follow_the_current_state():
    assert cli.next_actions(_snap(names={}))[0].startswith("collect 또는 scan")

    analyzing = cli.next_actions(_snap(
        [_incident("a" * 8, "FINOPS", "ANALYZING", IDLE_DEV)],
        pending=[{"category": "FINOPS", "subject_arn": IDLE_DEV}],
    ))
    assert analyzing[0] == "analyze — 분석 대기 1건 · 예상 모델 호출 약 3회(재시도 제외)"
    assert any(action.startswith("inject ssh --case C01") for action in analyzing)

    rightsizing = {"execution_id": "e" * 8, "runbook_id": "RUNBOOK_EC2_RIGHTSIZING", "status": "IN_PROGRESS"}
    running = cli.next_actions(_snap([
        _incident("b" * 8, "FINOPS", "ACTION_IN_PROGRESS", IDLE_DEV, [rightsizing]),
        _incident("c" * 8, "SECOPS", "AWAITING_APPROVAL", IDLE),
        _incident("d" * 8, "SECOPS", "AWAITING_CLOSURE", IDLE),
    ]))
    assert running[0] == "dispatch — 진행 중인 실행 1건을 한 칸 진행한다"
    assert running[1].startswith("dispatch --fail-status-check vigilantis-seed-idle-dev")
    assert "FE에서 승인 — 보안 vigilantis-seed-idle (cccccccc)" in running
    assert "FE에서 [종료 판단] — 보안 vigilantis-seed-idle (dddddddd)" in running

    rolled_back = dict(rightsizing, status="ROLLED_BACK")
    done = cli.next_actions(_snap([_incident("b" * 8, "FINOPS", "AWAITING_CLOSURE", IDLE_DEV, [rolled_back])]))
    assert not any(action.startswith("dispatch") for action in done)


def test_analyze_asks_with_the_estimate_and_stops_on_no(tmp_path, repo):
    snap = {"incidents": [], "assets": ASSETS,
            "pending_analysis": [{"incident_id": "a" * 36, "category": "SECOPS", "subject_arn": IDLE}]}
    ctx, output, _ = _ctx(tmp_path, repo, FakeRunner(ps=_ours(repo)), snap=snap)
    _running_round(ctx, repo)
    prompts = []
    ctx.confirm = lambda prompt: prompts.append(prompt) or False

    assert cli.main(["analyze"], ctx) == 0
    assert "분석 대기 1건 — 예상 모델 호출 약 3회(재시도 제외 · 실제 과금)" in output
    assert prompts and output[-1] == "누르지 않았다"
    assert not any(isinstance(call, tuple) for remote in FakeRemote.created for call in remote.calls)


def test_status_output_and_record_keep_the_token_out(tmp_path, repo):
    snap = {
        "incidents": [_incident("1" * 36, "SECOPS", "AWAITING_APPROVAL", IDLE)],
        "pending_analysis": [], "assets": ASSETS,
    }
    ctx, output, _ = _ctx(tmp_path, repo, FakeRunner(ps=_ours(repo)), snap=snap)
    _running_round(ctx, repo)

    assert cli.main(["status"], ctx) == 0
    text = "\n".join(output)
    assert "[승인 대기] 보안 vigilantis-seed-idle (11111111)" in text
    assert "http://localhost:3000/incidents/" + "1" * 36 in text
    steps = (server.State(ctx.data_root).round_dir("20260923-200000") / "steps.jsonl").read_text(encoding="utf-8")
    assert json.loads(steps.splitlines()[-1])["command"] == "status"
    assert TOKEN not in text and TOKEN not in steps


def test_data_root_defaults_to_the_home_folder_on_any_os():
    assert server.DEFAULT_DATA_ROOT == Path.home() / ".vigilantis" / "stepper"
