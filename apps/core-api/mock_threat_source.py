"""앱이 소비하는 모의 관측 파일 공급자 (Issue #322).

지정 폴더의 *.json 한 파일이 관측 한 건이다. 공급자는 임시 파일을 완성한 뒤
원자적으로 공개한다. 소비 완료는 done/, 계약 거부는 rejected/에 원문을 보관한다.
DB 데이터 오류는 거부로 보관하고, 그 밖의 저장소·파일 I/O 실패는 원본을 남겨
재시도하되, 같은 파일이 MOCK_THREAT_MAX_ATTEMPTS번 연속 실패하면 failed/로 격리한다. commit 직후 종료되어 재전달돼도 기존 Intake의 중복 키가 저장을 멱등하게 만든다.

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

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session, sessionmaker

from schemas.api.ws import WsEvent
from schemas.events import MockThreatEventInput
from schemas.mock_logs import MockSshLogEvidence
from threat_ingress import ThreatInputRejected, receive_threat

logger = logging.getLogger("vigilantis.mock_threat_source")
_INPUT = TypeAdapter(MockThreatEventInput)
MAX_OBSERVATION_BYTES = 64 * 1024


def parse_observation(raw: dict) -> MockThreatEventInput:
    """편집기용 $schema만 걷고 기존 입력 계약을 검증한다."""
    if not isinstance(raw, dict):
        raise ValueError("모의 관측은 JSON 객체여야 합니다")
    return _INPUT.validate_python({key: value for key, value in raw.items() if key != "$schema"})


class MockThreatSubmission(BaseModel):
    """Optional MVP log attachment; plain legacy observations remain accepted."""

    model_config = ConfigDict(extra="forbid")
    observation: MockThreatEventInput
    log_evidence: MockSshLogEvidence

    @model_validator(mode="after")
    def _same_observation(self):
        if not self.log_evidence.matches_observation(self.observation):
            raise ValueError("log evidence differs from mock observation")
        return self


def parse_submission(raw: dict) -> tuple[MockThreatEventInput, MockSshLogEvidence | None]:
    if isinstance(raw, dict) and "observation" in raw:
        value = MockThreatSubmission.model_validate(raw)
        return value.observation, value.log_evidence
    return parse_observation(raw), None


def prepare_observation(
    inbox: Path, observation: MockThreatEventInput, *, log_evidence: MockSshLogEvidence | None = None,
) -> Path:
    """관측 준비만 수행한다. 파일명은 배달 ID이고 업무 중복 키는 정형화 단계가 소유한다."""
    value = (MockThreatSubmission(observation=observation, log_evidence=log_evidence)
             if log_evidence is not None else observation)
    payload = value.model_dump_json().encode("utf-8")
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
        max_attempts: int = 5,
    ) -> None:
        self.inbox = inbox
        self._max_attempts = max_attempts
        # 파일명별 연속 실패 횟수 — 프로세스 메모리(dispatcher의 _FailureLedger와 같은 방식).
        self._attempts: dict[str, int] = {}
        # 거부됐거나 상한에 닿았지만 rejected/·failed/ 이동이 아직 끝나지 않은 파일 —
        # 재파싱·재전달 없이 이동만 다시 시도한다. 값: (대상 디렉터리, 시도 횟수, 사유, 오류 유형).
        self._pending: dict[str, tuple[str, int, str, str]] = {}
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

    def _log_archive_failed(self, path: Path, category: str, exc: OSError) -> None:
        # OSError 문자열에는 경로가 섞이므로 exc_info 없이 분류와 errno만 남긴다.
        logger.error("mock_threat_archive_failed", extra={
            "file": path.name,
            "category": category,
            "error_type": type(exc).__name__,
            "errno": exc.errno,
        })

    def _finish_pending(self, path: Path, report: dict[str, int]) -> None:
        """처리가 끝난 파일을 보관 디렉터리로 옮긴다. 이동이 실패하면 상태를 유지해 이동만 다시 시도한다."""
        category, attempts, reason, error_type = self._pending[path.name]
        try:
            self._archive(path, category)
        except OSError as exc:
            self._log_archive_failed(path, category, exc)
            return
        del self._pending[path.name]
        self._attempts.pop(path.name, None)
        if category == "rejected":
            return
        report["quarantined"] += 1
        logger.critical("mock_threat_delivery_quarantined", extra={
            "file": path.name,
            "attempts": attempts,
            "reason": reason,
            "error_type": error_type,
        })

    def _record_failure(self, path: Path, reason: str, exc: Exception, report: dict[str, int]) -> None:
        """실패 1회를 세고 상한에 닿으면 failed/로 격리한다. 입력 내용·예외 문자열은 남기지 않는다."""
        attempts = self._attempts.get(path.name, 0) + 1
        self._attempts[path.name] = attempts
        if attempts < self._max_attempts:
            return
        self._pending[path.name] = ("failed", attempts, reason, type(exc).__name__)
        self._finish_pending(path, report)

    def consume_once(self) -> dict[str, int]:
        report = {"created": 0, "existing": 0, "rejected": 0, "failed": 0, "quarantined": 0}
        paths = sorted(self.inbox.glob("*.json"))
        # 인박스에서 사라진 파일의 횟수는 버린다 — 무한히 쌓이지 않게 한다.
        present = {path.name for path in paths}
        for name in [name for name in self._attempts if name not in present]:
            del self._attempts[name]
        for name in [name for name in self._pending if name not in present]:
            del self._pending[name]
        for path in paths:
            if self._stopping.is_set():
                break
            if path.name in self._pending:
                self._finish_pending(path, report)
                continue
            try:
                # 읽기 상한도 적용한다 — stat 이후 파일이 커져도 메모리 제한을 지킨다.
                with path.open("rb") as stream:
                    payload = stream.read(MAX_OBSERVATION_BYTES + 1)
                try:
                    if len(payload) > MAX_OBSERVATION_BYTES:
                        raise ValueError("모의 관측 파일 크기 초과")
                    observation, log_evidence = parse_submission(json.loads(payload))
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ThreatInputRejected("모의 관측 파일 계약 거부") from exc

                with self._sessions() as db:
                    options = {"log_evidence": log_evidence} if log_evidence is not None else {}
                    outcome = receive_threat(db, observation, self._publish, **options)
                report["created" if outcome.created else "existing"] += 1
                self._archive(path, "done")
                self._attempts.pop(path.name, None)
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
                self._pending[path.name] = ("rejected", 0, "", type(cause).__name__)
                self._finish_pending(path, report)
            except ValidationError as exc:
                report["failed"] += 1
                # 저장 자료를 고친 뒤 재시도한다. 검증 예외에 포함된 원문은 로그에 남기지 않는다.
                logger.error("mock_threat_delivery_failed", extra={
                    "file": path.name,
                    "reason": "stored_data_validation_failed",
                    "error_type": type(exc).__name__,
                })
                self._record_failure(path, "stored_data_validation_failed", exc, report)
            except Exception as exc:  # noqa: BLE001 — 한 건의 실패가 다음 관측을 막지 않는다
                report["failed"] += 1
                # 예외 문자열·트레이스백에는 입력 원문이 섞일 수 있어 exc_info 없이 분류만 남긴다.
                logger.error("mock_threat_delivery_failed", extra={
                    "file": path.name,
                    "reason": "unexpected_exception",
                    "error_type": type(exc).__name__,
                })
                self._record_failure(path, "unexpected_exception", exc, report)
        return report

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.consume_once()
            except OSError:
                logger.exception("mock_threat_scan_failed")
            self._stopping.wait(self._interval)
