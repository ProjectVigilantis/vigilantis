# ==============================================================================
# [파일 설명]
# 구조화 로깅 구성 — JSON 한 줄 로그를 stdout(+선택 시 파일)으로 출력한다. (Issue #68·#424)
#
#   - request_id는 ContextVar로 전파한다. 미들웨어(main.request_context)가
#     설정·해제하며, 오류 봉투의 request_id와 같은 값이다.
#   - 배경 작업의 문맥(incident_id·분석 단계 등)은 log_context()로 싣는다. 그 블록
#     안의 모든 로그 줄에 같은 필드가 붙어, 호출 경계(모델 클라이언트 등)가 문맥을
#     인자로 받지 않아도 한 실행 단위로 묶인다. request_id와는 다른 축이다.
#   - 기록 항목은 request_id·메서드·경로·상태 코드·소요 시간 중심이다.
#     요청/응답 본문·자격증명·Prompt 전문·모델 원문 응답은 로그로 남기지
#     않는다(Issue #68, ADR-0005 미보존 대상 포함).
#   - 파일 출력은 켤 때만 붙는다(Settings.LOG_FILE_ENABLED). 크기 상한이 있는 회전
#     파일이며, 상대 경로는 이 폴더(apps/core-api) 기준이다 — compose가 이 폴더를
#     호스트와 공유하므로 컨테이너를 다시 만들어도 남는다.
# ==============================================================================

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Optional

# 요청 처리 중에만 값이 있다 — 요청 밖(기동 로그 등)에서는 None
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# log_context()가 쌓는 필드. 기본값을 읽기 전용으로 두어 공유 dict가 오염되지 않게 한다
_EMPTY_CONTEXT: Mapping[str, Any] = MappingProxyType({})
_log_context_var: ContextVar[Mapping[str, Any]] = ContextVar(
    "log_context", default=_EMPTY_CONTEXT
)

# 파일 출력 상한 — 파일 1개 10 MiB × (현재 + 백업 5개) = 스택당 최대 60 MiB
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5

_CORE_API_DIR = Path(__file__).resolve().parent

# LogRecord 기본 속성 목록 — extra=로 전달된 사용자 필드만 골라내는 기준
_RESERVED_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}

_logger = logging.getLogger("vigilantis.logging")


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """블록 안의 모든 로그 줄에 fields를 싣는다. 바깥 문맥 위에 겹쳐 쌓는다.

    **나갈 때 반드시 되돌린다.** 스케줄러의 동기 잡은 executor 스레드를 서로 나눠
    쓰므로(APScheduler), 남겨 두면 다음 잡의 로그에 이전 incident_id가 붙는다.
    값이 None인 필드는 싣지 않는다 — 없는 값을 null로 남기면 "모름"과 "없음"이 섞인다.
    """
    merged = {**_log_context_var.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _log_context_var.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _log_context_var.reset(token)


class JsonLineFormatter(logging.Formatter):
    """레코드를 JSON 한 줄로 직렬화한다. 공통 필드는 timestamp·level·logger·event
    (+문맥 식별자·extra 필드 최상위 합류). 같은 키면 extra가 log_context보다 우선한다."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        base_keys = frozenset(payload)
        payload.update(
            (key, value)
            for key, value in _log_context_var.get().items()
            if key not in base_keys
        )
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and key not in base_keys:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _StdoutHandler(logging.StreamHandler):
    """항상 '현재의' sys.stdout에 쓴다 — 객체를 고정하면 stdout을 교체하는
    실행 환경(pytest 캡처, 재기동)에서 로그가 유실된다."""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value):  # StreamHandler.__init__의 대입은 무시한다
        pass


class _JsonFileHandler(RotatingFileHandler):
    """setup_logging이 만든 파일 핸들러 — 재구성 시 닫을 대상을 가려내는 표지다."""


def resolve_log_file_path(raw: str) -> Path:
    """상대 경로는 실행 위치가 아니라 apps/core-api 기준으로 푼다.

    컨테이너(WORKDIR /app/apps/core-api)와 호스트 직접 실행(저장소 루트에서 pytest 등)이
    같은 값으로 같은 폴더를 가리키게 하기 위해서다.
    """
    path = Path(raw)
    return path if path.is_absolute() else _CORE_API_DIR / path


def setup_logging(
    level: str,
    *,
    log_file_path: Optional[str] = None,
    max_bytes: int = LOG_FILE_MAX_BYTES,
    backup_count: int = LOG_FILE_BACKUP_COUNT,
) -> None:
    """루트 로거를 JSON stdout 핸들러(+파일 핸들러)로 구성한다(재호출 시 재구성).

    log_file_path가 있으면 같은 줄을 회전 파일에도 쓴다. 부모 폴더는 만들고, 파일을
    열 수 없으면 여기서 예외로 기동을 멈춘다 — 켜 둔 파일 출력이 조용히 빠지면
    나중에 원인을 다시 볼 수 없다는 것이 이 기능의 목적과 어긋난다.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        # 우리가 만든 핸들러만 닫는다 — 테스트 도구 등 남이 붙인 핸들러의 수명은 그쪽 몫이다.
        # 닫지 않으면 create_app이 반복 호출될 때 파일 핸들이 쌓인다
        if isinstance(handler, (_StdoutHandler, _JsonFileHandler)):
            handler.close()

    formatter = JsonLineFormatter()
    stdout_handler = _StdoutHandler()
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    log_file: Optional[Path] = None
    if log_file_path is not None:
        # 빈 문자열도 그대로 푼다 — 폴더를 가리키게 되어 열기에서 실패한다(켜 놓고 조용히 꺼지지 않게)
        log_file = resolve_log_file_path(log_file_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = _JsonFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(level.upper())
    # 접근 로그는 request_context 미들웨어가 남긴다 — uvicorn 기본 접근 로그와 중복 방지
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    if log_file is not None:
        _logger.info(
            "log_file_enabled",
            extra={
                "log_file": str(log_file),
                "max_bytes": max_bytes,
                "backup_count": backup_count,
            },
        )
