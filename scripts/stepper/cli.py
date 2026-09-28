# ==============================================================================
# [파일 설명]
# FE 관찰용 테스트 서버의 리모콘 — 타이머를 끈 테스트 서버를 명령 한 번에 한 주기씩 진행한다.
# 명령 요약·흐름 예시·주의는 scripts/stepper/README.md.
#
#   uv run python scripts/stepper/cli.py <명령>        (저장소 루트에서)
#
# 명령마다 한 일 → FE에서 볼 곳 → 다음에 할 수 있는 것을 출력한다. 누르는 동안 받은 WebSocket
# 이벤트와 누른 뒤의 REST 상태는 회차 기록(<데이터 루트>/rounds/<회차 ID>/steps.jsonl)에 남긴다.
# 순서를 강제하지 않는다 — 서버가 받지 않는 조합은 서버의 거부·실패가 그대로 보인다.
# 승인·해제·종료 판단은 FE에서 누른다. 그 화면이 관찰 대상이다.
# 모든 명령은 포트 소유부터 확인하고(server.py), 통과하지 못하면 아무것도 하지 않는다.
# ==============================================================================

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self


def _load_server():
    """같은 폴더의 server.py. scripts는 패키지가 아니라 경로로 불러온다."""
    name = "stepper_server"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("server.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclass가 정의 모듈을 sys.modules에서 찾는다
        spec.loader.exec_module(module)
    return sys.modules[name]


server = _load_server()
Refused = server.Refused

# FE 배지와 같은 말(apps/web/src/lib/enum-labels.ts)
INCIDENT_STATUS = {
    "ANALYZING": "분석 중", "AWAITING_APPROVAL": "승인 대기", "ACTION_IN_PROGRESS": "조치 진행 중",
    "AWAITING_CLOSURE": "종료 판단 대기", "RESOLVED": "종료", "FAILED": "진행 불가",
}
EXECUTION_STATUS = {
    "IN_PROGRESS": "실행 중", "SUCCESS": "완료", "FAILED": "실패", "ROLLBACK_INITIATED": "복구 중",
    "ROLLED_BACK": "복구 완료", "ROLLBACK_FAILED": "복구 실패", "UNVERIFIED": "결과 확인 불가",
}
CATEGORY = {"FINOPS": "최적화", "SECOPS": "보안"}
RISK = {"HIGH": "높음", "MEDIUM": "중간", "LOW": "낮음"}
RUNNING_EXECUTIONS = {"IN_PROGRESS", "ROLLBACK_INITIATED"}

# 버튼 한 번을 기다리는 시간(초). analyze는 대기 건마다 모델을 직렬로 부른다
TIMEOUTS = {"collect": 300, "scan": 300, "consume": 120, "analyze": 900, "dispatch": 300}

TITLES = {
    "collect": "자산·메트릭 수집·적재(판정·카드 없음)",
    "scan": "수집 → 판정 → FinOps 사건 생성",
    "consume": "관측 1건 주입 → 소비 1회",
    "analyze": "분석 대기 전부를 실제 모델로 분석",
    "dispatch": "진행 중인 실행 한 칸 + 끝난 차단의 해제 후보",
}
# "FE에서 볼 곳"은 새로고침 없이 바뀌는 서버 값을 가리킨다(WebSocket 이벤트가 없는 collect만 새로고침해야
# 보인다). 보안 인시던트 목록의 「승인 대기 N」 칩과 T1 원복 안내 패널은 그렇지 않다(PR #402가 기록한
# FE 동작. 자산 인시던트 목록의 칩은 따라 바뀐다). 대신 카드 배지와 「수행된 조치」를 본다.
FE_HINTS = {
    "collect": "자산 — 새로고침하면 목록이 바뀐다(WebSocket 이벤트가 없다. 카드는 생기지 않는다)",
    "scan": "자산 인시던트 — 새 카드가 '분석 중'으로 뜬다 · 자산 — 판정 배지와 토폴로지 색",
    "consume": "보안 인시던트 — 새 카드('분석 중'). 선제 차단 경로면 '선제 차단됨' 배지가 붙는다",
    "analyze": "카드 배지 — '승인 대기' 또는 '진행 불가'로 바뀐다"
               "(보안 인시던트 목록의 「승인 대기 N」 칩은 새로고침 전까지 그대로)",
    "dispatch": "인시던트 상세 「수행된 조치」 — 실행 배지. 자동 원복은 '복구 완료' 배지로 본다"
                "(원복 안내 패널은 상세를 떠나면 사라진다)",
}
DISPATCH_COUNTERS = {
    "started": "실행 시작", "judged": "Status Check 판정", "closed": "확정",
    "awaiting_status_check": "판정 대기", "awaiting_judgement": "현물 판정 대기",
    "rollback_initiated": "원복 필요", "rollback_started": "자동 원복 접수",
    "deferred": "조회 실패 보류", "retry_waiting": "재시도 간격 대기", "held": "재시도 소진 확정",
    "release_offered": "해제 후보", "release_rejected": "해제 후보 거절",
    "release_skipped": "해제 후보 미저장", "skipped": "건너뜀", "unsupported": "미구현 런북",
    "errored": "오류",
}
ANALYZE_COUNTERS = {
    "claimed": "분석", "succeeded": "제안 생성", "no_proposal": "제안 없음", "failed": "실패",
    "skipped": "선점 실패", "unsupported": "미지원 분류", "reclaimed": "회수", "errored": "오류",
}


# ------------------------------------------------------------------------------
# 순수 판정 — 대상 이름·관측 시각·예상 호출 수·다음 동작
# ------------------------------------------------------------------------------


def resolve_target(assets: list[dict], name: str) -> str:
    """자산 목록 응답에서 Name이 정확히 일치하는 1건의 ARN. 아니면 넣지 않는다 —
    접수할 때 대상 근거가 없으면 분석이 실패한다(docs/E2E_DEMO_SCENARIOS.md §T2 주입 전 전제)."""
    matches = [asset for asset in assets if asset.get("name") == name]
    if len(matches) == 1:
        return matches[0]["arn"]
    if not assets:
        raise Refused("수집된 자산이 없다 — collect나 scan을 먼저 누르세요")
    if not matches:
        known = sorted({asset["name"] for asset in assets if asset.get("name")})
        raise Refused(
            f"'{name}' 자산이 없다 — collect나 scan을 먼저 누르세요. 수집된 이름: {', '.join(known)}"
        )
    raise Refused(f"'{name}' 자산이 {len(matches)}건이라 대상을 하나로 정하지 못해 넣지 않았다")


def utc_seconds(now: datetime) -> str:
    """누른 시각 — UTC, 초 단위. 로그 코퍼스 prepare는 초 단위 이동만 받는다."""
    return now.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def estimate_calls(pending: list[dict]) -> int:
    """analyze 한 번의 예상 모델 호출 수(재시도 제외).

    FinOps EC2(다운사이징)는 근거 요약·추천·절감 단가 3회, 그 밖의 FinOps는 2회, SecOps는
    3회다(docs/E2E_DEMO_SCENARIOS.md §사전 준비 · datasets/secops-log-corpus/OBSERVATION_SUMMARY.md).
    """
    total = 0
    for item in pending:
        if item["category"] == "SECOPS" or ":instance/" in (item.get("subject_arn") or ""):
            total += 3
        else:
            total += 2
    return total


def label(incident: dict, names: dict) -> str:
    subject = incident["subject_arn"]
    return (
        f"{CATEGORY.get(incident['category'], incident['category'])} "
        f"{names.get(subject) or subject.rsplit('/', 1)[-1]} ({incident['incident_id'][:8]})"
    )


def next_actions(snap: dict) -> list[str]:
    """현재 상태에서 할 수 있는 것. 순서를 강제하지 않으므로 해당하는 것을 모두 낸다."""
    incidents, names = snap["incidents"], snap["asset_names"]
    actions = []
    pending = snap["pending_analysis"]
    if pending:
        actions.append(
            f"analyze — 분석 대기 {len(pending)}건 · 예상 모델 호출 약 {estimate_calls(pending)}회"
            "(재시도 제외)"
        )
    running = [
        (incident, execution)
        for incident in incidents for execution in incident["executions"]
        if execution["status"] in RUNNING_EXECUTIONS
    ]
    if running:
        actions.append(f"dispatch — 진행 중인 실행 {len(running)}건을 한 칸 진행한다")
    # API 상태로는 실행 전과 판정 대기를 가를 수 없다(둘 다 IN_PROGRESS) — 조건을 문구로 밝힌다.
    # 이미 한 칸 진행했으면 주입기가 시작 조건(시드 유형)에서 멈추므로 잘못 눌러도 주입되지 않는다
    for incident, execution in running:
        if execution["runbook_id"] == "RUNBOOK_EC2_RIGHTSIZING" and execution["status"] == "IN_PROGRESS":
            name = names.get(incident["subject_arn"]) or incident["subject_arn"]
            actions.append(
                f"dispatch --fail-status-check {name} — 아직 실행 첫 칸 전이면 이렇게 눌러 "
                "Status Check 실패와 자동 원복을 본다"
            )
    for status, verb in (
        ("AWAITING_APPROVAL", "FE에서 승인"),
        ("AWAITING_CLOSURE", "FE에서 [종료 판단]"),
        ("FAILED", "FE에서 [종료 판단](진행 불가)"),
    ):
        matched = [incident for incident in incidents if incident["status"] == status]
        if matched:
            actions.append(f"{verb} — " + ", ".join(label(incident, names) for incident in matched))
    open_ = [incident for incident in incidents if incident["status"] != "RESOLVED"]
    if not names:
        actions.append("collect 또는 scan — 자산부터 수집한다(scan은 FinOps 카드까지 만든다)")
    else:
        if not any(incident["category"] == "FINOPS" for incident in open_):
            actions.append("scan — 판정으로 FinOps 카드를 만든다")
        if not any(incident["category"] == "SECOPS" for incident in open_):
            actions.append(
                "inject ssh --case C01 --target vigilantis-seed-idle — 로그 근거가 붙은 SSH 관측 1건"
            )
    return actions


# ------------------------------------------------------------------------------
# 보고 문구
# ------------------------------------------------------------------------------


def describe(button: str, body: dict) -> list[str]:
    report = body.get("report")
    if button == "collect":
        return _region_lines(report or [])
    if button == "scan":
        if report.get("skipped"):
            return ["다른 스캔이 advisory lock을 잡고 있어 건너뛰었다(성공 아님)"]
        incidents = report["incidents"]
        verdicts = report.get("verdicts") or {}
        return [
            f"사건 새로 {incidents['created']} · 기존 {incidents['existing']} · 실패 {incidents['failed']}",
            "판정 " + " · ".join(f"{key} {value}" for key, value in verdicts.items()),
            *_region_lines(report.get("stored") or []),
        ]
    if button == "consume":
        lines = [(
            f"소비 새로 {report['created']} · 기존 {report['existing']} · 거부 {report['rejected']} "
            f"· 실패 {report['failed']}"
        )]
        for row in body.get("observations") or []:
            if row.get("incident_id"):
                same = "" if row.get("new") else " (같은 관측 — 기존 Incident)"
                lines.append(f"{row['file']} → Incident {row['incident_id']}{same}")
            else:
                lines.append(f"{row['file']} → Incident 없음(거부·실패 — 서버 로그를 확인하세요)")
        return lines
    counters = ANALYZE_COUNTERS if button == "analyze" else DISPATCH_COUNTERS
    parts = [f"대상 {report.get('scanned', 0)}"] + [
        f"{text} {report[key]}" for key, text in counters.items() if report.get(key)
    ]
    return [" · ".join(parts)]


def _region_lines(regions: list[dict]) -> list[str]:
    lines = []
    for region in regions:
        if region.get("status") == "FAILED":
            lines.append(f"리전 {region.get('region')} 수집 실패 — {region.get('error')}")
            continue
        # LocalStack Community에는 elbv2·autoscaling이 없어 그 둘은 늘 빠진다(ADR-0006)
        degraded = region.get("degraded_collectors") or []
        suffix = f" · 미수집 유형 {', '.join(degraded)}" if degraded else ""
        lines.append(f"리전 {region.get('region')} 자산 {region.get('total')}건{suffix}")
    return lines


def events_line(recorder: WsRecorder) -> str:
    if recorder.error:
        return f"WS 구독 실패({recorder.error}) — 이벤트를 기록하지 못했다"
    if not recorder.events:
        return "WS 이벤트 없음"
    counts = Counter(event.get("event_type", "?") for event in recorder.events)
    parts = [f"{kind} ×{count}" for kind, count in counts.items()]
    executions = [
        f"{event['data']['execution_id'][:8]} {event['data']['status']}"
        for event in recorder.events if event.get("event_type") == "EXECUTION_UPDATED"
    ]
    tail = f" (실행 {', '.join(executions)})" if executions else ""
    return f"WS 이벤트 {len(recorder.events)}건: " + " · ".join(parts) + tail


def _touched(recorder: WsRecorder, body: dict) -> list[str]:
    ids = [event.get("data", {}).get("incident_id") for event in recorder.events]
    ids += [row.get("incident_id") for row in body.get("observations") or []]
    return list(dict.fromkeys(incident_id for incident_id in ids if incident_id))


# ------------------------------------------------------------------------------
# 서버 호출 · WS 구독 · 기록
# ------------------------------------------------------------------------------


class WsRecorder:
    """버튼을 누르는 동안 /api/v1/ws를 구독해 받은 이벤트를 모은다. 연결 실패는 기록만 한다."""

    def __init__(self, url: str, *, settle: float = 0.5) -> None:
        self.url = url
        self.settle = settle
        self.events: list[dict] = []
        self.error: str | None = None
        self._connection = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        try:
            from websockets.sync.client import connect

            # 셸의 프록시 설정이 loopback 연결을 가로채지 않게 한다
            self._connection = connect(self.url, open_timeout=3, proxy=None)
        except Exception as exc:  # noqa: BLE001 — 구독 실패가 버튼을 막지 않는다
            self.error = type(exc).__name__
            return self
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return self

    def _pump(self) -> None:
        try:
            for message in self._connection:
                try:
                    self.events.append(json.loads(message))
                except ValueError:
                    self.events.append({"raw": str(message)})
        except Exception:  # noqa: BLE001 — 닫힘·끊김
            pass

    def __exit__(self, *exc) -> bool:
        if self._connection is not None:
            # 발행은 큐를 거쳐 나가므로 응답 뒤에 도착하는 이벤트를 잠시 더 받는다
            time.sleep(self.settle)
            self._connection.close()
            self._thread.join(2)
        return False


def snapshot(remote) -> dict:
    """버튼 뒤의 REST 상태 — 상태의 원천은 DB다. WS 수신과 별개로 남긴다."""
    listing = remote.get("/api/v1/incidents")["items"]
    incidents = [remote.get(f"/api/v1/incidents/{item['incident_id']}") for item in listing]
    incidents.sort(key=lambda incident: incident["created_at"])
    pending = remote.get("/_stepper/pending-analysis", auth=True)["incidents"]
    assets = remote.get("/api/v1/assets").get("items") or []
    names = {asset["arn"]: asset.get("name") or asset.get("resource_id") for asset in assets}
    return {"incidents": incidents, "pending_analysis": pending, "asset_names": names}


def record(ctx, live, entry: dict) -> Path:
    """회차 기록에 한 줄. 토큰은 어디에도 들어가지 않는다(요청 헤더에만 있다)."""
    live.round_dir.mkdir(parents=True, exist_ok=True)
    path = live.round_dir / "steps.jsonl"
    step = 1
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            step += sum(1 for _ in stream)
    line = {"step": step, "at": ctx.now().isoformat(timespec="seconds"), "round": live.round_id, **entry}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    return path


def press(live, button: str) -> tuple[dict, WsRecorder]:
    with WsRecorder(server.ws_url(live.ports)) as recorder:
        body = live.remote.press(button, timeout=TIMEOUTS[button])
    return body, recorder


def finish_step(
    ctx, live, args, *, command: str, button: str, body: dict, recorder: WsRecorder,
    detail: dict | None = None, extra_lines: tuple[str, ...] = (),
) -> None:
    snap = snapshot(live.remote)
    path = record(ctx, live, {
        "command": command,
        "detail": detail or {},
        "ok": body["ok"],
        "elapsed_seconds": body.get("elapsed_seconds"),
        "report": {key: value for key, value in body.items() if key not in ("button", "ok")},
        "ws": {"events": recorder.events, "error": recorder.error},
        "snapshot": snap,
    })
    verdict = "" if body["ok"] else "  ← 성공 아님"
    ctx.out(f"[{command}] {TITLES[button]} ({body.get('elapsed_seconds')}초){verdict}")
    for line in (*extra_lines, *describe(button, body), events_line(recorder)):
        ctx.out(f"  {line}")
    ctx.out(f"FE에서 볼 곳: {FE_HINTS[button]}")
    by_id = {incident["incident_id"]: incident for incident in snap["incidents"]}
    for incident_id in _touched(recorder, body)[:5]:
        incident = by_id.get(incident_id)
        what = "" if incident is None else (
            f"  ({label(incident, snap['asset_names'])} · "
            f"{INCIDENT_STATUS.get(incident['status'], incident['status'])})"
        )
        ctx.out(f"  {args.fe_url}/incidents/{incident_id}{what}")
    show_next(ctx, snap)
    ctx.out(f"기록: {path}")


def show_next(ctx, snap: dict) -> None:
    ctx.out("다음에 할 수 있는 것:")
    for action in next_actions(snap):
        ctx.out(f"  - {action}")


class Injector:
    """inject_status_check_failure.py를 실행 칸 전에 띄워 두고, dispatch가 끝난 뒤 결과를 거둔다.

    시간이 멈춘 서버라 실행 칸 뒤에도 유형이 바뀐 채 그대로 있다 — 시연 때의 주입 창 경합이 없다.
    """

    ARMED = "대기 중"
    INJECTED = "주입: stop_instances"

    def __init__(self, ctx, ports: dict, name: str) -> None:
        self.lines: list[str] = []
        self._queue: queue.Queue = queue.Queue()
        self._proc = ctx.popen(
            [sys.executable, str(ctx.repo_root / "scripts" / "inject_status_check_failure.py"),
             "--name", name, "--timeout", "180"],
            cwd=ctx.repo_root, env=server.aws_env(ports), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self._proc.stdout:
            self._queue.put(line.rstrip("\n"))
        self._queue.put(None)

    def wait_armed(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                return False  # 시작 조건에서 멈췄다
            self.lines.append(line)
            if self.ARMED in line:
                return True
        return False

    def finish(self, grace: float = 5.0) -> bool:
        """주입했는지. 이번 dispatch에 그 인스턴스의 기동이 없었으면 멈추고 False."""
        try:
            self._proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        while True:
            try:
                line = self._queue.get(timeout=1)
            except queue.Empty:
                break
            if line is None:
                break
            self.lines.append(line)
        return any(self.INJECTED in line for line in self.lines)


# ------------------------------------------------------------------------------
# 명령
# ------------------------------------------------------------------------------


def cmd_up(ctx, args) -> None:
    live, started = server.up(ctx)
    if not started:
        ctx.out("테스트 서버가 이미 떠 있다 — 데이터를 그대로 쓴다")
    _show_ready(ctx, live, args, command="up" if not started else "up(처음부터 준비)")


def cmd_reset(ctx, args) -> None:
    live = server.reset(ctx)
    _show_ready(ctx, live, args, command="reset")


def _show_ready(ctx, live, args, *, command: str) -> None:
    snap = snapshot(live.remote)
    record(ctx, live, {"command": command, "snapshot": snap})
    ctx.out(f"회차 {live.round_id} · API {server.api_url(live.ports)} · 사건 {len(snap['incidents'])}건")
    ctx.out(f"FE: apps/web에서 npm run dev -- -H localhost → {args.fe_url}")
    show_next(ctx, snap)
    ctx.out(f"기록: {live.round_dir}")


def cmd_down(ctx, args) -> None:
    server.down(ctx)
    ctx.out(f"{server.PROJECT}를 볼륨째 내렸다. 회차 기록은 남는다: {ctx.data_root / 'rounds'}")


def cmd_status(ctx, args) -> None:
    live = server.connect(ctx)
    snap = snapshot(live.remote)
    record(ctx, live, {"command": "status", "snapshot": snap})
    names = snap["asset_names"]
    ctx.out(f"회차 {live.round_id} · API {server.api_url(live.ports)} · 사건 {len(snap['incidents'])}건")
    for incident in snap["incidents"]:
        risk = incident.get("initial_risk_level")
        reviewed = incident.get("reviewed_risk_level")
        risk_text = ""
        if risk:
            risk_text = f" · 위험 {RISK.get(risk, risk)}"
            if reviewed and reviewed != risk:
                risk_text += f"→{RISK.get(reviewed, reviewed)}"
        executions = ", ".join(
            f"{execution['runbook_id'].removeprefix('RUNBOOK_')} "
            f"{EXECUTION_STATUS.get(execution['status'], execution['status'])}"
            for execution in incident["executions"]
        )
        ctx.out(
            f"  [{INCIDENT_STATUS.get(incident['status'], incident['status'])}] "
            f"{label(incident, names)}{risk_text}" + (f" · 실행: {executions}" if executions else "")
        )
        ctx.out(f"      {args.fe_url}/incidents/{incident['incident_id']}")
    show_next(ctx, snap)


def cmd_collect(ctx, args) -> None:
    live = server.connect(ctx)
    body, recorder = press(live, "collect")
    finish_step(ctx, live, args, command="collect", button="collect", body=body, recorder=recorder)


def cmd_scan(ctx, args) -> None:
    live = server.connect(ctx)
    body, recorder = press(live, "scan")
    finish_step(ctx, live, args, command="scan", button="scan", body=body, recorder=recorder)


def cmd_inject(ctx, args) -> None:
    live = server.connect(ctx)
    arn = resolve_target(live.remote.get("/api/v1/assets").get("items") or [], args.target)
    occurred_at = args.occurred_at or utc_seconds(ctx.now())
    inbox = server.host_inbox(ctx.repo_root)
    inbox.mkdir(parents=True, exist_ok=True)
    scripts = ctx.repo_root / "scripts"
    if args.kind == "ssh":
        source = f"ssh {args.case}"
        command = [
            sys.executable, str(scripts / "secops_log_corpus.py"), "prepare", "--case", args.case,
            "--inbox", str(inbox), "--target-arn", arn, "--occurred-at", occurred_at,
        ]
    else:
        source = f"golden {args.file}"
        command = [
            sys.executable, str(scripts / "inject_mock_threat.py"), args.file,
            "--prepare-inbox", str(inbox), "--target-arn", arn, "--occurred-at", occurred_at,
        ]
    proc = ctx.run(
        command, cwd=ctx.repo_root, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        tail = "\n".join(((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-5:])
        raise Refused(f"관측을 준비하지 못해 넣지 않았다\n{tail}")
    prepared = Path(json.loads(proc.stdout)["path"])
    inputs = live.round_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    shutil.copy2(prepared, inputs / prepared.name)

    body, recorder = press(live, "consume")
    finish_step(
        ctx, live, args, command=f"inject {source}", button="consume", body=body, recorder=recorder,
        detail={"target": args.target, "target_arn": arn, "occurred_at": occurred_at,
                "input": str(inputs / prepared.name)},
        extra_lines=(f"{source} → 대상 {args.target} · 관측 시각 {occurred_at}",),
    )


def cmd_analyze(ctx, args) -> None:
    live = server.connect(ctx)
    pending = live.remote.get("/_stepper/pending-analysis", auth=True)["incidents"]
    if pending and not args.yes:
        ctx.out(
            f"분석 대기 {len(pending)}건 — 예상 모델 호출 약 {estimate_calls(pending)}회"
            "(재시도 제외 · 실제 과금)"
        )
        if not ctx.confirm("진행할까요? [y/N] "):
            ctx.out("누르지 않았다")
            return
    body, recorder = press(live, "analyze")
    finish_step(
        ctx, live, args, command="analyze", button="analyze", body=body, recorder=recorder,
        detail={"pending": pending, "estimated_calls": estimate_calls(pending)},
    )


def cmd_dispatch(ctx, args) -> None:
    live = server.connect(ctx)
    target = args.fail_status_check
    injector = None
    if target:
        injector = Injector(ctx, live.ports, target)
        if not injector.wait_armed():
            injector.finish(grace=0)
            raise Refused(
                "실패 주입기가 대기 상태가 되지 않아 dispatch를 누르지 않았다:\n"
                + "\n".join(f"  │ {line}" for line in injector.lines)
                + "\n테스트 서버에서는 reset이 '사전 준비를 처음부터'와 같다"
            )
    try:
        body, recorder = press(live, "dispatch")
    finally:
        injected = injector.finish() if injector else None
    extra: tuple[str, ...] = ()
    detail: dict = {}
    if injector is not None:
        armed = next((line for line in injector.lines if line.startswith("대상:")), "")
        outcome = (
            "주입함 — 다음 dispatch의 2/2 Status Check가 실패하고 자동 원복이 이어진다"
            if injected else "주입 안 됨 — 이번 dispatch에 이 인스턴스의 다운사이징 기동이 없었다"
        )
        extra = (f"실패 주입기 {armed.strip()} → {outcome}",)
        detail = {"fail_status_check": {"name": target, "injected": injected, "lines": injector.lines}}
    finish_step(
        ctx, live, args, command="dispatch" + (f" --fail-status-check {target}" if target else ""),
        button="dispatch", body=body, recorder=recorder, detail=detail, extra_lines=extra,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stepper",
        description="FE 관찰용 테스트 서버 리모콘 — 명령 한 번에 한 주기씩 진행한다 (scripts/stepper/README.md)",
    )
    parser.add_argument(
        "--data-root", type=Path, default=None,
        help=f"토큰·회차 기록 폴더 (기본 {server.DEFAULT_DATA_ROOT})",
    )
    parser.add_argument("--fe-url", default="http://localhost:3000", help="안내에 쓰는 FE 주소")
    commands = parser.add_subparsers(dest="command", required=True, metavar="명령")
    for name, handler, text in (
        ("up", cmd_up, "테스트 서버를 띄운다(떠 있으면 그대로 쓴다)"),
        ("reset", cmd_reset, "볼륨째 지우고 처음 상태로 준비한다"),
        ("down", cmd_down, "테스트 서버를 볼륨째 내린다(회차 기록은 남는다)"),
        ("status", cmd_status, "사건·실행 상태와 다음에 할 수 있는 것"),
        ("collect", cmd_collect, "자산·메트릭 수집·적재만(판정·카드 없음)"),
        ("scan", cmd_scan, "수집 → 판정 → FinOps 사건 생성"),
    ):
        commands.add_parser(name, help=text).set_defaults(handler=handler)

    inject = commands.add_parser("inject", help="위협 관측 1건 주입 → 소비 1회")
    kinds = inject.add_subparsers(dest="kind", required=True, metavar="종류")
    ssh = kinds.add_parser("ssh", help="로그 근거가 붙은 SSH 관측(datasets/secops-log-corpus)")
    ssh.add_argument("--case", required=True, choices=[f"C{i:02}" for i in range(1, 8)])
    golden = kinds.add_parser("golden", help="골든 위협 입력(datasets/golden/secops/input)")
    golden.add_argument("file", help="입력 파일명(확장자 생략 가능). 예: evt_open_ip_001")
    for sub in (ssh, golden):
        sub.add_argument("--target", required=True, help="대상 자산의 Name — 수집된 목록에서 정확히 1건")
        sub.add_argument("--occurred-at", help="관측 시각(ISO 8601). 생략하면 누른 시각(UTC, 초 단위)")
        sub.set_defaults(handler=cmd_inject)

    analyze = commands.add_parser("analyze", help="분석 대기 전부를 실제 모델로 분석한다(과금)")
    analyze.add_argument("-y", "--yes", action="store_true", help="예상 호출 수 확인을 건너뛴다")
    analyze.set_defaults(handler=cmd_analyze)

    dispatch = commands.add_parser("dispatch", help="진행 중인 실행을 한 칸 진행하고 해제 후보를 낸다")
    dispatch.add_argument(
        "--fail-status-check", metavar="NAME",
        help="실행 전에 실패 주입기를 띄운다(다운사이징 대상 EC2의 Name)",
    )
    dispatch.set_defaults(handler=cmd_dispatch)
    return parser


def main(argv: list[str] | None = None, ctx: Any = None) -> int:
    args = build_parser().parse_args(argv)
    if ctx is None:
        # Windows 콘솔(cp949)은 이 파일의 한국어·기호를 못 낸다 — UTF-8로 고정한다
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass
        ctx = server.Context()
    if args.data_root is not None:
        ctx.data_root = args.data_root
    try:
        args.handler(ctx, args)
    except Refused as exc:
        ctx.out(f"중단: {exc}")
        return 2
    except KeyboardInterrupt:
        ctx.out("중단: 사용자가 멈췄다")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
