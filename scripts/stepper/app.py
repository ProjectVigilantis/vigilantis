# ==============================================================================
# [파일 설명]
# FE 관찰용 테스트 서버의 진입점 — 제품 앱(main.app)에 단계 진행 라우터(/_stepper/*)를
# 붙인다. 버튼 하나가 앱의 주기 함수를 1회 부르고, 그 발행이 같은 프로세스의 WebSocket으로
# FE까지 간다. 별도 프로세스에서 같은 함수를 부르면 DB는 바뀌어도 FE로 이벤트가 가지 않는다.
#
# 테스트 서버(scripts/stepper/compose.override.yml)만 이 진입점을 쓴다:
#   uvicorn app:build_app --factory --app-dir /app/scripts/stepper
# 제품 진입점(main:app)과 apps/core-api는 바꾸지 않는다 — 제품 코드에 데모 분기를 두지 않는다.
#
# 기동 검사 — 아래 중 하나라도 어기면 앱을 만들지 않는다(uvicorn이 기동에 실패한다).
#   · 스캔·실행·AI 분석 타이머(SCAN_ENABLED·DISPATCH_ENABLED)가 켜져 있다
#     → 타이머와 버튼이 같은 주기 함수를 겹쳐 돌리면 주기 비중첩(max_instances=1) 전제가 깨진다
#   · 모의 위협 inbox 루프(MOCK_THREAT_INBOX_DIR)가 켜져 있다 → inject가 consume_once를 직접 부른다
#   · 토큰(STEPPER_TOKEN)이 없거나 inbox(STEPPER_INBOX_DIR)가 절대 경로가 아니다
#   · AWS 엔드포인트가 LocalStack이 아니다(scripts/seed_localstack.py와 같은 조건)
#
# 라우터는 공개 주기 함수의 보고를 그대로 돌려주고 ok로 성공 여부만 가른다. SQL·Boto3·업무
# 흐름을 직접 조정하지 않는다. 예외는 inject 결과 조회다 — 주입한 관측의 Incident를 목록의
# 최신 건으로 추정하지 않고, 정형화 함수가 만든 중복 키로 기존 repository에서 찾는다.
# 버튼은 한 번에 하나만 돈다. 처리 중에 들어온 요청은 409와 처리 중인 버튼 이름을 받는다.
# ==============================================================================

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

# import 경로: core-api + packages(schemas) — 다른 스크립트와 같은 부트스트랩
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT / "apps" / "core-api"), str(_REPO_ROOT / "packages")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import get_aws_settings, get_collector_settings, get_settings  # noqa: E402
from fastapi import (  # noqa: E402
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
)

logger = logging.getLogger("vigilantis.stepper")

SERVER_ID = "vigilantis-stepper"
TOKEN_ENV = "STEPPER_TOKEN"
INBOX_ENV = "STEPPER_INBOX_DIR"
MIN_TOKEN_LENGTH = 16


def startup_problems(environ: Mapping[str, str] = os.environ) -> list[str]:
    """기동을 막을 설정 위반 목록. 빈 목록이면 띄워도 된다."""
    problems = []
    if get_collector_settings().SCAN_ENABLED:
        problems.append("SCAN_ENABLED=false여야 한다 — 스캔 타이머가 scan·collect 버튼과 겹친다")
    settings = get_settings()
    if settings.DISPATCH_ENABLED:
        problems.append(
            "DISPATCH_ENABLED=false여야 한다 — 실행·AI 분석 타이머가 dispatch·analyze 버튼과 겹친다"
        )
    if settings.MOCK_THREAT_INBOX_DIR.strip():
        problems.append("MOCK_THREAT_INBOX_DIR는 비워야 한다 — inject가 소비를 직접 부른다")
    if len(environ.get(TOKEN_ENV, "")) < MIN_TOKEN_LENGTH:
        problems.append(f"{TOKEN_ENV}가 없거나 {MIN_TOKEN_LENGTH}자보다 짧다")
    inbox = environ.get(INBOX_ENV, "")
    if not inbox or not Path(inbox).is_absolute():
        problems.append(f"{INBOX_ENV}는 절대 경로여야 한다")
    endpoint = get_aws_settings().endpoint_url()
    if not endpoint:
        problems.append("AWS_ENDPOINT_URL이 없다 — 테스트 서버는 LocalStack 전용이다")
    elif "amazonaws.com" in endpoint:
        problems.append(f"실 AWS 엔드포인트({endpoint})에는 띄우지 않는다")
    return problems


def build_app() -> FastAPI:
    """uvicorn --factory 진입점. 기동 검사를 통과해야 제품 앱에 라우터를 붙여 돌려준다."""
    problems = startup_problems()
    if problems:
        raise SystemExit("테스트 서버 기동 거부:\n  - " + "\n  - ".join(problems))
    import main

    return attach(main.app, token=os.environ[TOKEN_ENV], inbox=Path(os.environ[INBOX_ENV]))


def attach(application: FastAPI, *, token: str, inbox: Path) -> FastAPI:
    application.include_router(build_router(token=token, inbox=inbox))
    # 기동 로그로 활성 설정을 진단할 수 있게 남긴다. 토큰은 남기지 않는다
    settings = get_settings()
    logger.info("stepper_router_attached", extra={
        "inbox": str(inbox),
        "status_check_wait": [
            settings.STATUS_CHECK_WAIT_DELAY_SECONDS, settings.STATUS_CHECK_WAIT_MAX_ATTEMPTS,
        ],
        "verification_retry_interval_seconds": settings.VERIFICATION_RETRY_INTERVAL_SECONDS,
    })
    return application


def _session_factory():
    """주기 함수에 넘길 세션 팩토리 — 테스트가 일회용 DB 세션으로 바꿔 끼우는 자리."""
    from db.session import get_session_factory

    return get_session_factory()


class _Gate:
    """버튼 하나씩. 비차단으로 잡고, 못 잡으면 처리 중인 버튼 이름으로 409를 낸다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: str | None = None

    @contextmanager
    def hold(self, button: str) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail={"busy": self.current})
        self.current = button
        try:
            yield
        finally:
            self.current = None
            self._lock.release()


def build_router(*, token: str, inbox: Path) -> APIRouter:
    # 주기 함수 모듈은 여기서 가져온다 — 기동 검사가 무거운 import보다 먼저 돌게 하고,
    # 호출 시점에 모듈 속성으로 찾으므로 테스트가 그 함수를 대역으로 바꿀 수 있다
    import agent_dispatcher
    import dispatcher
    from db.repositories import incidents as incidents_repo
    from mock_threat_source import MockThreatConsumer
    from services import collector
    from services import scheduler as scan_scheduler

    gate = _Gate()
    expected = token.encode("utf-8")

    def require_token(x_stepper_token: str | None = Header(default=None)) -> None:
        if x_stepper_token is None or not hmac.compare_digest(
            x_stepper_token.encode("utf-8"), expected
        ):
            raise HTTPException(status_code=401, detail="stepper 토큰이 없거나 다르다")

    router = APIRouter(
        prefix="/_stepper", dependencies=[Depends(require_token)], include_in_schema=False
    )

    def press(button: str, work: Callable[[], tuple[bool, dict]]) -> dict:
        with gate.hold(button):
            started = time.perf_counter()
            ok, fields = work()
            elapsed = round(time.perf_counter() - started, 2)
        logger.info("stepper_button_done", extra={
            "button": button, "ok": ok, "elapsed_seconds": elapsed,
        })
        return {"button": button, "ok": ok, "elapsed_seconds": elapsed, **fields}

    @router.get("/ping")
    def ping() -> dict:
        settings = get_settings()
        return {
            "server": SERVER_ID,
            "busy": gate.current,
            "inbox": str(inbox),
            "model": settings.OPENAI_MODEL,
            "status_check_wait": [
                settings.STATUS_CHECK_WAIT_DELAY_SECONDS, settings.STATUS_CHECK_WAIT_MAX_ATTEMPTS,
            ],
            "verification_retry_interval_seconds": settings.VERIFICATION_RETRY_INTERVAL_SECONDS,
        }

    @router.get("/pending-analysis")
    def pending_analysis() -> dict:
        """analyze 한 번이 처리할 Incident — 주기 함수가 읽는 조회를 그대로 쓴다."""
        items = []
        with _session_factory()() as db:
            for incident_id, category in incidents_repo.list_pending_agent_analysis(db):
                incident = incidents_repo.get_incident(db, incident_id)
                items.append({
                    "incident_id": incident_id,
                    "category": category.value,
                    "subject_arn": None if incident is None else incident.subject_arn,
                })
        return {"incidents": items}

    @router.post("/collect")
    def collect() -> dict:
        def work() -> tuple[bool, dict]:
            regions = collector.collect_and_store()
            return not _failed_regions(regions), {"report": regions}

        return press("collect", work)

    @router.post("/scan")
    def scan(request: Request) -> dict:
        def work() -> tuple[bool, dict]:
            summary = scan_scheduler.run_pipeline(_publisher(request))
            return _scan_ok(summary), {"report": summary}

        return press("scan", work)

    @router.post("/consume")
    def consume(request: Request) -> dict:
        def work() -> tuple[bool, dict]:
            sessions = _session_factory()
            pending = _read_pending(inbox)
            keys = [entry["dedup_key"] for entry in pending if entry["dedup_key"]]
            with sessions() as db:
                seen = {
                    key for key in keys
                    if incidents_repo.get_threat_event_by_dedup_key(db, key) is not None
                }
            consumer = MockThreatConsumer(
                inbox, sessions, _publisher(request), interval_seconds=1.0
            )
            report = consumer.consume_once()
            with sessions() as db:
                observations = [
                    _locate(db, incidents_repo, entry, seen) for entry in pending
                ]
            ok = (
                report["rejected"] == 0
                and report["failed"] == 0
                and report["created"] + report["existing"] > 0
            )
            return ok, {"report": report, "observations": observations}

        return press("consume", work)

    @router.post("/analyze")
    def analyze(request: Request) -> dict:
        def work() -> tuple[bool, dict]:
            report = dict(vars(agent_dispatcher.run_agent_dispatch_cycle(
                _session_factory(), _publisher(request)
            )))
            # FAILED는 분석이 진행 불가로 끝난 건이다 — 도구 오류는 아니지만 성공으로 보고하지 않는다
            return report["errored"] == 0 and report["failed"] == 0, {"report": report}

        return press("analyze", work)

    @router.post("/dispatch")
    def dispatch(request: Request) -> dict:
        def work() -> tuple[bool, dict]:
            report = dict(vars(dispatcher.run_dispatch_cycle(_session_factory(), _publisher(request))))
            return report["errored"] == 0, {"report": report}

        return press("dispatch", work)

    return router


def _publisher(request: Request):
    return request.app.state.realtime.publish


def _failed_regions(regions: list[dict]) -> list[str]:
    return [region.get("region", "?") for region in regions if region.get("status") == "FAILED"]


def _scan_ok(summary: dict) -> bool:
    """skip(advisory lock 미획득)·리전 실패·Incident 저장 실패는 성공이 아니다."""
    if summary.get("skipped"):
        return False
    return not _failed_regions(summary.get("stored", [])) and summary["incidents"]["failed"] == 0


def _read_pending(inbox: Path) -> list[dict]:
    """소비 전 inbox의 관측 파일과 그 업무 중복 키. 소비자와 같은 순서·상한으로 읽는다."""
    from mock_threat_source import MAX_OBSERVATION_BYTES, parse_submission
    from security.threat_normalizer import normalize_threat_event

    pending = []
    for path in sorted(inbox.glob("*.json")):
        entry: dict = {"file": path.name, "dedup_key": None}
        try:
            with path.open("rb") as stream:
                payload = stream.read(MAX_OBSERVATION_BYTES + 1)
            observation, _ = parse_submission(json.loads(payload))
            event = normalize_threat_event(observation)
        except Exception:  # noqa: BLE001 — 거부 판정은 소비자 몫이다. 여기서는 식별만 포기한다
            pending.append(entry)
            continue
        entry.update(
            dedup_key=event.deduplication_key,
            event_type=event.event_type.value,
            target_arn=event.target_arn,
            occurred_at=event.occurred_at.isoformat(),
        )
        pending.append(entry)
    return pending


def _locate(db, incidents_repo, entry: dict, seen: set[str]) -> dict:
    """관측 1건의 Incident. new는 이번 소비로 처음 저장됐는지다(같은 관측 재전달이면 False)."""
    located = {key: value for key, value in entry.items() if key != "dedup_key"}
    located.update(incident_id=None, new=False)
    key = entry["dedup_key"]
    if key is None:
        return located
    threat = incidents_repo.get_threat_event_by_dedup_key(db, key)
    incident = (
        None if threat is None
        else incidents_repo.get_incident_by_threat_event_id(db, threat.threat_event_id)
    )
    if incident is not None:
        located.update(incident_id=incident.incident_id, new=key not in seen)
    return located
