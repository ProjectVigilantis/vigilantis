# ==============================================================================
# [파일 설명]
# 테스트 서버 수명주기 — compose 명령 조립, 렌더링된 설정 검사, 포트 소유 확인, 초기화 절차,
# 준비 확인, 토큰·회차 상태 파일. cli.py가 가져다 쓴다(직접 실행하지 않는다).
#
# 안전장치
#   · compose 명령에는 항상 -p vigilantis-test와 override를 붙인다. 다른 프로젝트명을 받지 않는다.
#   · 테스트 서버 포트(5432·4566·8000)를 다른 compose 프로젝트나 compose 밖 프로세스가 잡고
#     있으면 아무것도 하지 않고 거부한다. CLI의 시드·주입기·HTTP 호출은 이 노트북의 같은 포트
#     번호로 가므로, 그 자리에 개발·시연 스택이 있으면 시드가 그 LocalStack의 NACL 규칙을 비우고
#     실패 주입기가 그 인스턴스를 멈춘다. 남의 스택을 대신 내리지도 않는다.
#   · 띄우기 전에 렌더링된 설정을 본다 — 포트가 전부 127.0.0.1인지, 볼륨이 이 프로젝트 것인지.
#     `!override`를 모르는 Compose는 포트를 덧붙여 모든 인터페이스에 열 수 있다. 렌더링 결과에는
#     .env 값이 섞이므로 포트·이름 말고는 읽지도 출력하지도 않는다.
#   · 초기화는 볼륨째 한다. LocalStack은 비영속이라 DB만 남기면 새 인스턴스 ID로 카드가 이중으로
#     생긴다(docs/E2E_DEMO_SCENARIOS.md §사전 준비). 같은 이유로 down도 볼륨을 지운다.
#   · 토큰은 데이터 루트의 token 파일에만 두고 출력·기록에 남기지 않는다.
# ==============================================================================

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = "vigilantis-test"
LOOPBACK = "127.0.0.1"
SERVICES = ("db", "localstack", "api")
DEFAULT_PORTS = {"db": 5432, "localstack": 4566, "api": 8000}
SERVER_ID = "vigilantis-stepper"  # scripts/stepper/app.py의 ping 응답과 같은 값
TOKEN_HEADER = "X-Stepper-Token"
DEFAULT_DATA_ROOT = Path.home() / ".vigilantis" / "stepper"
SEED_REGION = "ap-northeast-2"  # override가 api에 고정한 리전과 같아야 시드한 자산이 수집된다
# 시연 사전 준비의 키 확인 줄과 같다 — 모델 조회라 토큰을 쓰지 않는다
MODEL_CHECK = (
    "import os; from openai import OpenAI; "
    "OpenAI(max_retries=0).models.retrieve(os.getenv('OPENAI_MODEL') or 'gpt-5.6-luna'); "
    "print('OK')"
)

Runner = Callable[..., subprocess.CompletedProcess]


class Refused(Exception):
    """거부·중단. 메시지는 사용자에게 그대로 보인다."""


def host_inbox(repo_root: Path = REPO_ROOT) -> Path:
    """리모콘 inbox의 호스트 경로 — compose가 apps/core-api를 마운트하므로 컨테이너의
    STEPPER_INBOX_DIR와 같은 폴더다. `.mock-threat-inbox/`는 .gitignore 대상이고, 개발 스택의
    소비자는 비재귀라 이 하위 폴더를 읽지 않는다."""
    return repo_root / "apps" / "core-api" / ".mock-threat-inbox" / "stepper"


def api_url(ports: dict) -> str:
    return f"http://{LOOPBACK}:{ports['api']}"


def ws_url(ports: dict) -> str:
    return f"ws://{LOOPBACK}:{ports['api']}/api/v1/ws"


def aws_env(ports: dict) -> dict[str, str]:
    """호스트에서 도는 시드·주입기의 환경 — 이 프로젝트의 LocalStack에만 붙는다.

    셸의 AWS_* 설정(실 계정 프로필 등)은 걷어 낸다. localhost 대신 127.0.0.1을 쓴다 — 포트가
    IPv4 loopback에만 열려 있어 localhost가 ::1로 먼저 풀리면 연결마다 수 초씩 헛돈다.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    env.update(
        AWS_ENDPOINT_URL=f"http://{LOOPBACK}:{ports['localstack']}",
        AWS_ACCESS_KEY_ID="test",
        AWS_SECRET_ACCESS_KEY="test",
        AWS_REGION=SEED_REGION,
        AWS_REGIONS="",
        PYTHONIOENCODING="utf-8",
    )
    return env


# ------------------------------------------------------------------------------
# 렌더링된 설정 · 포트 점유 — 순수 판정
# ------------------------------------------------------------------------------


def scope_problems(config: dict) -> list[str]:
    """down -v가 이 프로젝트의 것만 지우는지 — 프로젝트명과 볼륨 이름."""
    problems = []
    if config.get("name") != PROJECT:
        problems.append(f"프로젝트명이 {PROJECT}가 아니다({config.get('name')})")
    for key, volume in (config.get("volumes") or {}).items():
        if volume.get("external") or not str(volume.get("name", "")).startswith(f"{PROJECT}_"):
            problems.append(f"볼륨 {key}({volume.get('name')})가 이 프로젝트 것이 아니다")
    return problems


def check_rendered_config(config: dict) -> tuple[dict[str, int], list[str]]:
    """`docker compose config --format json` 결과에서 서비스별 호스트 포트와 위반 목록을 낸다."""
    problems = scope_problems(config)
    published: dict[str, list[int]] = {}
    for name, service in (config.get("services") or {}).items():
        for port in service.get("ports") or []:
            if port.get("published") in (None, ""):
                continue  # 호스트에 열지 않은 포트
            host_ip = port.get("host_ip") or ""
            if host_ip != LOOPBACK:
                problems.append(
                    f"{name}의 {port['published']}번 포트가 {host_ip or '모든 인터페이스'}에 열린다"
                )
            published.setdefault(name, []).append(int(port["published"]))
    for name in SERVICES:
        if len(published.get(name, [])) != 1:
            problems.append(f"{name}의 호스트 포트가 하나가 아니다({published.get(name, [])})")
    ports = {name: published[name][0] for name in SERVICES if len(published.get(name, [])) == 1}
    return ports, problems


@dataclass(frozen=True)
class Container:
    name: str
    project: str | None
    service: str | None
    working_dir: str | None
    host_ports: frozenset[int] = field(default_factory=frozenset)


def parse_ps(output: str) -> list[Container]:
    """`docker ps --format '{{json .}}'` 출력(한 줄에 컨테이너 하나)을 읽는다."""
    containers = []
    for line in output.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # 값에 쉼표가 든 라벨(config_files)은 조각나지만 여기서 읽는 라벨은 쉼표가 없다
        labels = dict(
            item.split("=", 1) for item in (row.get("Labels") or "").split(",") if "=" in item
        )
        containers.append(Container(
            name=row.get("Names", ""),
            project=labels.get("com.docker.compose.project"),
            service=labels.get("com.docker.compose.service"),
            working_dir=labels.get("com.docker.compose.project.working_dir"),
            host_ports=frozenset(_host_ports(row.get("Ports") or "")),
        ))
    return containers


def _host_ports(ports: str) -> set[int]:
    """`127.0.0.1:8000->8000/tcp, :::5432->5432/tcp` 꼴에서 호스트 쪽 포트만 뽑는다."""
    found: set[int] = set()
    for mapping in ports.split(","):
        if "->" not in mapping:
            continue  # 호스트에 열지 않은 포트
        port = mapping.split("->", 1)[0].strip().rpartition(":")[2]
        if "-" in port:
            start, end = port.split("-", 1)
            found.update(range(int(start), int(end) + 1))
        elif port.isdigit():
            found.add(int(port))
    return found


def foreign_holders(containers: Iterable[Container], ports: Iterable[int]) -> list[str]:
    """테스트 서버 포트를 잡고 있는 남의 컨테이너."""
    wanted = set(ports)
    held = []
    for container in containers:
        taken = sorted(container.host_ports & wanted)
        if taken and container.project != PROJECT:
            owner = (
                f"compose 프로젝트 '{container.project}'" if container.project
                else "compose 밖의 컨테이너"
            )
            held.append(f"{', '.join(map(str, taken))}번 포트 — {owner}의 {container.name}")
    return held


def port_answers(port: int, *, timeout: float = 0.3) -> bool:
    """loopback(IPv4·IPv6)에서 누가 이 포트를 받고 있는지. 컨테이너 밖 프로세스를 잡는다."""
    for host in (LOOPBACK, "::1"):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


# ------------------------------------------------------------------------------
# Docker · HTTP
# ------------------------------------------------------------------------------


class Docker:
    def __init__(self, run: Runner = subprocess.run, repo_root: Path = REPO_ROOT) -> None:
        self._run = run
        self.repo_root = repo_root

    def compose_args(self, *args: str) -> list[str]:
        return [
            "docker", "compose", "-p", PROJECT,
            "-f", str(self.repo_root / "docker-compose.yml"),
            "-f", str(self.repo_root / "scripts" / "stepper" / "compose.override.yml"),
            *args,
        ]

    def compose(
        self, *args: str, token: str, capture: bool = False, check: bool = True,
    ) -> subprocess.CompletedProcess:
        # override가 ${STEPPER_TOKEN:?}로 요구한다 — 모든 compose 명령에 넘긴다
        env = dict(os.environ, STEPPER_TOKEN=token)
        proc = self._run(
            self.compose_args(*args), cwd=self.repo_root, env=env, capture_output=capture,
            text=True, encoding="utf-8", errors="replace",
        )
        if check and proc.returncode != 0:
            raise Refused(
                f"docker compose {args[0]}이 실패했다(종료 코드 {proc.returncode}) — "
                "다음 단계로 가지 않았다"
            )
        return proc

    def rendered_config(self, token: str) -> dict:
        proc = self.compose("config", "--format", "json", token=token, capture=True, check=False)
        if proc.returncode != 0:
            # 오류 출력에는 설정값이 섞이지 않는다 — 문법·버전 문제를 알아보도록 보여 준다
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-5:])
            raise Refused(
                "compose 설정을 렌더링하지 못했다 — .env가 없거나 Compose가 !override를 모르는지 "
                f"아래 오류를 확인하세요\n{tail}"
            )
        return json.loads(proc.stdout)

    def running(self) -> list[Container]:
        proc = self._run(
            ["docker", "ps", "--no-trunc", "--format", "{{json .}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            raise Refused("Docker가 응답하지 않는다 — Docker Desktop을 켠 뒤 다시 실행하세요")
        return parse_ps(proc.stdout)


class Remote:
    """테스트 서버 HTTP 클라이언트. 토큰은 헤더로만 보내고 어디에도 남기지 않는다."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        # 셸의 HTTP(S)_PROXY가 loopback 호출을 가로채지 않게 프록시 없이 연다
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _send(self, method: str, path: str, *, auth: bool, timeout: float | None = None):
        request = urllib.request.Request(
            self.base_url + path, method=method, data=b"" if method == "POST" else None,
        )
        if auth:
            request.add_header(TOKEN_HEADER, self._token)
        try:
            with self._opener.open(request, timeout=timeout or self._timeout) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read())

    def healthy(self) -> bool:
        try:
            return self._send("GET", "/health", auth=False, timeout=2)[0] == 200
        except OSError:
            return False

    def ping(self) -> dict | None:
        try:
            status, body = self._send("GET", "/_stepper/ping", auth=True, timeout=5)
        except OSError:
            return None
        return body if status == 200 and isinstance(body, dict) else None

    def get(self, path: str, *, auth: bool = False) -> Any:
        status, body = self._send("GET", path, auth=auth)
        if status != 200:
            raise Refused(f"GET {path}가 {status}로 끝났다{_request_id(body)}")
        return body

    def press(self, button: str, *, timeout: float) -> dict:
        status, body = self._send("POST", f"/_stepper/{button}", auth=True, timeout=timeout)
        if status == 409:
            busy = (body.get("detail") or {}).get("busy") if isinstance(body, dict) else None
            raise Refused(f"서버가 다른 버튼({busy})을 처리하고 있다 — 끝난 뒤 다시 누르세요")
        if status == 401:
            raise Refused("테스트 서버가 이 CLI의 토큰을 받지 않는다 — reset으로 다시 띄우세요")
        if status != 200:
            raise Refused(
                f"{button}이 서버 오류({status})로 끝났다{_request_id(body)} — "
                f"docker compose -p {PROJECT} logs api"
            )
        return body


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _request_id(body: Any) -> str:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return f" · request_id {body['error'].get('request_id')}"
    return ""


# ------------------------------------------------------------------------------
# 데이터 루트 — 토큰·현재 회차·회차 기록
# ------------------------------------------------------------------------------


class State:
    """<데이터 루트>/token · state.json · rounds/<회차 ID>/. 저장소 밖(사용자 홈)에 둔다."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def _token_path(self) -> Path:
        return self.root / "token"

    def token(self) -> str | None:
        try:
            return self._token_path.read_text(encoding="ascii").strip() or None
        except FileNotFoundError:
            return None

    def new_token(self) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(24)
        self._token_path.write_text(token, encoding="ascii")
        try:
            os.chmod(self._token_path, 0o600)
        except OSError:
            pass  # 권한 비트가 없는 파일 시스템
        return token

    def load(self) -> dict:
        try:
            return json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def save(self, **values: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        merged = {**self.load(), **values}
        (self.root / "state.json").write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def round_dir(self, round_id: str) -> Path:
        return self.root / "rounds" / round_id


def new_round_id(state: State, now: datetime) -> str:
    base = now.strftime("%Y%m%d-%H%M%S")
    candidate, suffix = base, 2
    while state.round_dir(candidate).exists():
        candidate, suffix = f"{base}-{suffix}", suffix + 1
    return candidate


def archive_inbox(inbox: Path, destination: Path) -> int:
    """리모콘 inbox의 내용(done/·rejected/ 포함)을 회차 기록으로 옮기고 빈 inbox를 남긴다.

    지우지 않고 옮기기만 한다. 대상은 `.mock-threat-inbox/stepper`로 끝나는 경로뿐이다.
    """
    if inbox.name != "stepper" or inbox.parent.name != ".mock-threat-inbox":
        raise Refused(f"리모콘 inbox가 아닌 경로는 정리하지 않는다: {inbox}")
    moved = 0
    if inbox.is_dir():
        entries = sorted(inbox.iterdir())
        if entries:
            target, suffix = destination, 2
            while target.exists():
                target, suffix = destination.with_name(f"{destination.name}-{suffix}"), suffix + 1
            target.mkdir(parents=True)
            for entry in entries:
                shutil.move(str(entry), str(target / entry.name))
                moved += 1
    inbox.mkdir(parents=True, exist_ok=True)
    return moved


# ------------------------------------------------------------------------------
# 수명주기
# ------------------------------------------------------------------------------


def ask_tty(prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise Refused("확인을 받을 수 없는 입력이다 — 확인 없이 진행하려면 -y를 붙이세요")
    return input(prompt).strip().lower() in ("y", "yes")


@dataclass
class Context:
    """명령이 쓰는 바깥 세계 — 테스트가 대역으로 바꾼다."""

    data_root: Path = DEFAULT_DATA_ROOT
    repo_root: Path = REPO_ROOT
    run: Runner = subprocess.run
    popen: Callable[..., subprocess.Popen] = subprocess.Popen
    confirm: Callable[[str], bool] = ask_tty
    remote_factory: Callable[[str, str], Remote] = Remote
    port_answers: Callable[[int], bool] = port_answers
    out: Callable[[str], None] = print
    now: Callable[[], datetime] = lambda: datetime.now().astimezone()
    sleep: Callable[[float], None] = time.sleep

    @property
    def docker(self) -> Docker:
        return Docker(self.run, self.repo_root)

    @property
    def state(self) -> State:
        return State(self.data_root)


@dataclass
class Live:
    """소유 확인을 통과한 떠 있는 테스트 서버."""

    remote: Remote
    ports: dict
    round_id: str
    round_dir: Path


def check_can_start(ctx: Context, token: str) -> dict:
    """기동 전 확인 — 렌더링된 설정과 포트 점유. 통과하면 서비스별 호스트 포트를 돌려준다."""
    ports, problems = check_rendered_config(ctx.docker.rendered_config(token))
    if problems:
        raise Refused("렌더링된 compose 설정이 안전하지 않아 띄우지 않았다:\n" + _bullets(problems))
    containers = ctx.docker.running()
    _refuse_foreign(containers, ports)
    held = set().union(*(container.host_ports for container in containers)) if containers else set()
    strangers = [port for port in ports.values() if port not in held and ctx.port_answers(port)]
    if strangers:
        raise Refused(
            f"compose 밖의 프로세스가 {', '.join(map(str, strangers))}번 포트를 쓰고 있어 "
            "아무것도 하지 않았다 — 그 프로세스를 끈 뒤 다시 실행하세요"
        )
    return ports


def connect(ctx: Context) -> Live:
    """명령마다 소유 확인 — 이 프로젝트가 포트를 잡고 있고 토큰 ping이 통해야 진행한다."""
    state = ctx.state
    saved = state.load()
    ports = saved.get("ports") or dict(DEFAULT_PORTS)
    containers = ctx.docker.running()
    _refuse_foreign(containers, ports)
    ours = {container.service: container for container in containers if container.project == PROJECT}
    missing = [name for name in SERVICES if name not in ours]
    if missing:
        raise Refused(f"테스트 서버가 떠 있지 않다({', '.join(missing)} 없음) — up으로 띄우세요")
    api = ours["api"]
    if api.working_dir and not same_path(api.working_dir, ctx.repo_root):
        raise Refused(
            f"지금 테스트 서버는 다른 체크아웃({api.working_dir})에서 띄웠다 — "
            "그 체크아웃의 CLI를 쓰거나 여기서 reset하세요"
        )
    for name in SERVICES:
        if ports[name] not in ours[name].host_ports:
            raise Refused(f"{name}의 포트 기록이 실제와 다르다 — reset으로 다시 띄우세요")
    token = state.token()
    ping = ctx.remote_factory(api_url(ports), token).ping() if token else None
    if not ping or ping.get("server") != SERVER_ID:
        raise Refused(
            f"{LOOPBACK}:{ports['api']}의 API가 이 CLI의 토큰을 받지 않는다 — reset으로 다시 띄우세요"
        )
    round_id = saved.get("round")
    if not round_id:
        raise Refused("회차 기록이 없다 — reset으로 다시 띄우세요")
    return Live(ctx.remote_factory(api_url(ports), token), ports, round_id, state.round_dir(round_id))


def reset(ctx: Context) -> Live:
    """지우고 처음 상태로 준비한다. 부분 초기화는 두지 않는다."""
    state = ctx.state
    ports = check_can_start(ctx, state.token() or state.new_token())
    previous = state.load().get("round")
    round_id = new_round_id(state, ctx.now())
    round_dir = state.round_dir(round_id)
    round_dir.mkdir(parents=True)
    archived_to = (state.round_dir(previous) if previous else round_dir) / "inbox"
    moved = archive_inbox(host_inbox(ctx.repo_root), archived_to)
    if moved:
        ctx.out(f"이전 inbox {moved}건을 옮겼다 → {archived_to}")
    token = state.new_token()
    state.save(round=round_id, ports=ports, repo_root=str(ctx.repo_root))

    docker = ctx.docker
    ctx.out(f"[1/5] {PROJECT} 프로젝트만 볼륨째 내린다")
    docker.compose("down", "-v", "--remove-orphans", token=token)
    ctx.out("[2/5] db·localstack 기동")
    docker.compose("up", "-d", "--wait", "db", "localstack", token=token)
    ctx.out("[3/5] LocalStack 시드")
    ctx.out("      " + seed(ctx, ports, round_dir))
    ctx.out("[4/5] api 기동(migrate가 먼저 돈다)")
    docker.compose("up", "-d", "--wait", "api", token=token)
    ctx.out("[5/5] 준비 확인")
    remote = ctx.remote_factory(api_url(ports), token)
    wait_ready(ctx, remote, expect_empty=True)
    if check_model_key(docker, token):
        ctx.out("      모델 키 확인 OK")
    else:
        ctx.out("      ! 모델 키 확인 실패 — analyze가 실패한다. .env의 OPENAI_API_KEY를 고친 뒤 reset")
    return Live(remote, ports, round_id, round_dir)


def up(ctx: Context) -> tuple[Live, bool]:
    """떠 있으면 그대로 두고, 아니면 reset과 같이 준비한다. (Live, 새로 띄웠는지)"""
    containers = ctx.docker.running()
    if set(SERVICES) <= {c.service for c in containers if c.project == PROJECT}:
        try:
            return connect(ctx), False
        except Refused as exc:
            ctx.out(f"떠 있는 테스트 서버를 쓸 수 없다: {exc}")
            ctx.out("처음부터 다시 준비한다")
    return reset(ctx), True


def down(ctx: Context) -> None:
    """이 프로젝트를 볼륨째 내린다. 회차 기록은 데이터 루트에 남는다."""
    state = ctx.state
    token = state.token() or state.new_token()
    problems = scope_problems(ctx.docker.rendered_config(token))
    if problems:
        raise Refused("볼륨 범위를 확인하지 못해 내리지 않았다:\n" + _bullets(problems))
    ctx.docker.compose("down", "-v", "--remove-orphans", token=token)


def seed(ctx: Context, ports: dict, round_dir: Path) -> str:
    proc = ctx.run(
        [sys.executable, str(ctx.repo_root / "scripts" / "seed_localstack.py")],
        cwd=ctx.repo_root, env=aws_env(ports), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    log = round_dir / "seed.log"
    log.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
    lines = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0:
        tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-8:])
        raise Refused(f"시드가 실패했다(종료 코드 {proc.returncode}) — 전체 출력: {log}\n{tail}")
    return lines[-1] if lines else "시드 완료"


def wait_ready(ctx: Context, remote: Remote, *, expect_empty: bool, timeout: float = 120.0) -> None:
    waited = 0.0
    while not remote.healthy():
        if waited >= timeout:
            raise Refused(
                f"API가 {timeout:g}초 안에 응답하지 않았다 — docker compose -p {PROJECT} logs api"
            )
        ctx.sleep(1.0)
        waited += 1.0
    incidents = remote.get("/api/v1/incidents")["items"]  # DB·migrate까지 거치는 조회
    if expect_empty and incidents:
        raise Refused(f"초기화했는데 사건 {len(incidents)}건이 남아 있다 — 볼륨이 지워지지 않았다")
    if remote.ping() is None:
        raise Refused("리모콘 경로가 토큰을 받지 않는다 — override의 STEPPER_TOKEN을 확인하세요")


def check_model_key(docker: Docker, token: str) -> bool:
    # 출력은 보이지 않는다 — 인증 오류 문구에 키 일부가 섞일 수 있다
    proc = docker.compose(
        "exec", "-T", "api", "uv", "run", "python", "-c", MODEL_CHECK,
        token=token, capture=True, check=False,
    )
    lines = (proc.stdout or "").strip().splitlines()
    return proc.returncode == 0 and bool(lines) and lines[-1].strip() == "OK"


def _refuse_foreign(containers: list[Container], ports: dict) -> None:
    held = foreign_holders(containers, ports.values())
    if held:
        raise Refused(
            "다른 스택이 테스트 서버 포트를 쓰고 있어 아무것도 하지 않았다:\n" + _bullets(held)
            + "\n그 스택을 직접 내린 뒤 다시 실행하세요. 이 도구는 남의 스택을 내리지 않는다."
        )


def _bullets(lines: Iterable[str]) -> str:
    return "\n".join(f"  - {line}" for line in lines)
