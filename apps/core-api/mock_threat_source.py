"""앱이 소비하는 모의 관측 파일 공급자 (Issue #322).

지정 폴더의 *.json 한 파일이 관측 한 건이다. 공급자는 임시 파일을 완성한 뒤
원자적으로 공개한다. 소비 완료는 done/, 계약 거부는 rejected/에 원문을 보관한다.
DB 데이터 오류는 거부로 보관하고, 그 밖의 저장소·파일 I/O 실패는 원본을 남겨
재시도한다. commit 직후 종료되어 재전달돼도 기존 Intake의 중복 키가 저장을 멱등하게 만든다.

worker 1개가 순차 소비한다. 종료 요청 뒤 새 파일은 받지 않고 처리 중인 한 건을
마친 뒤 돌아온다. lifespan은 이 종료를 기다린 뒤 RealtimeManager를 닫는다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Callable

from pydantic import TypeAdapter
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session, sessionmaker

from schemas.api.ws import WsEvent
from schemas.events import MockThreatEventInput
from threat_ingress import ThreatInputRejected, receive_threat

logger = logging.getLogger("vigilantis.mock_threat_source")
_INPUT = TypeAdapter(MockThreatEventInput)
MAX_OBSERVATION_BYTES = 64 * 1024


def parse_observation(raw: dict) -> MockThreatEventInput:
    """편집기용 $schema만 걷고 기존 입력 계약을 검증한다."""
    if not isinstance(raw, dict):
        raise ValueError("모의 관측은 JSON 객체여야 합니다")
    return _INPUT.validate_python({key: value for key, value in raw.items() if key != "$schema"})


def prepare_observation(inbox: Path, observation: MockThreatEventInput) -> Path:
    """관측 준비만 수행한다. 파일명은 배달 ID이고 업무 중복 키는 정형화 단계가 소유한다."""
    payload = observation.model_dump_json().encode("utf-8")
    if len(payload) > MAX_OBSERVATION_BYTES:
        raise ValueError("모의 관측 파일은 64 KiB 이하여야 합니다")
    inbox.mkdir(parents=True, exist_ok=True)
    delivery = uuid.uuid4().hex
    pending = inbox / f".{delivery}.tmp"
    ready = inbox / f"{delivery}.json"
    try:
        with pending.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(ready)
    finally:
        pending.unlink(missing_ok=True)
    return ready


class MockThreatConsumer:
    def __init__(
        self,
        inbox: Path,
        session_factory: sessionmaker[Session],
        publish: Callable[[WsEvent], None],
        *,
        interval_seconds: float,
    ) -> None:
        self.inbox = inbox
        self._sessions = session_factory
        self._publish = publish
        self._interval = interval_seconds
        self._stopping = threading.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        # cwd에 따라 달라지는 상대 경로와 없는 부모 경로는 기동 실패로 드러낸다.
        if not self.inbox.is_absolute():
            raise ValueError(f"MOCK_THREAT_INBOX_DIR는 절대 경로여야 합니다: {self.inbox}")
        self.inbox.mkdir(exist_ok=True)
        self._task = asyncio.create_task(asyncio.to_thread(self._run))
        logger.info("mock_threat_consumer_started", extra={
            "inbox": str(self.inbox), "interval_seconds": self._interval,
        })

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await asyncio.shield(self._task)

    def _archive(self, path: Path, category: str) -> None:
        destination = self.inbox / category
        destination.mkdir(exist_ok=True)
        # 사용자가 같은 파일명을 재전달해도 앞선 수신 원문을 덮어쓰지 않는다.
        path.rename(destination / f"{path.stem}-{uuid.uuid4().hex}.json")

    def consume_once(self) -> dict[str, int]:
        report = {"created": 0, "existing": 0, "rejected": 0, "failed": 0}
        for path in sorted(self.inbox.glob("*.json")):
            if self._stopping.is_set():
                break
            try:
                # 읽기 상한도 적용한다 — stat 이후 파일이 커져도 메모리 제한을 지킨다.
                with path.open("rb") as stream:
                    payload = stream.read(MAX_OBSERVATION_BYTES + 1)
                try:
                    if len(payload) > MAX_OBSERVATION_BYTES:
                        raise ValueError("모의 관측 파일 크기 초과")
                    observation = parse_observation(json.loads(payload))
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ThreatInputRejected("모의 관측 파일 계약 거부") from exc

                with self._sessions() as db:
                    outcome = receive_threat(db, observation, self._publish)
                report["created" if outcome.created else "existing"] += 1
                self._archive(path, "done")
            except (ThreatInputRejected, DataError) as exc:
                report["rejected"] += 1
                is_data_error = isinstance(exc, DataError)
                cause = exc.orig if is_data_error else (exc.__cause__ or exc)
                # 예외 문자열에는 입력값·SQL이 섞일 수 있어 분류와 SQLSTATE만 기록한다.
                logger.warning("mock_threat_input_rejected", extra={
                    "file": path.name,
                    "reason": "database_data_error" if is_data_error else "input_contract_rejected",
                    "error_type": type(cause).__name__,
                    "sqlstate": getattr(cause, "sqlstate", None) if is_data_error else None,
                })
                try:
                    self._archive(path, "rejected")
                except OSError:
                    logger.exception("mock_threat_archive_failed", extra={"file": path.name})
            except Exception:  # noqa: BLE001 — 한 건의 실패가 다음 관측을 막지 않는다
                report["failed"] += 1
                logger.exception("mock_threat_delivery_failed", extra={"file": path.name})
        return report

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.consume_once()
            except OSError:
                logger.exception("mock_threat_scan_failed")
            self._stopping.wait(self._interval)
