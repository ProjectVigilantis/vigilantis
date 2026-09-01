# ==============================================================================
# [파일 설명]
# 구조화 로깅 구성 — JSON 한 줄 로그를 stdout으로 출력한다. (Issue #68)
#
#   - request_id는 ContextVar로 전파한다. 미들웨어(main.request_context)가
#     설정·해제하며, 오류 봉투의 request_id와 같은 값이다.
#   - 기록 항목은 request_id·메서드·경로·상태 코드·소요 시간 중심이다.
#     요청/응답 본문·자격증명·Prompt 전문·모델 원문 응답은 로그로 남기지
#     않는다(Issue #68, ADR-0005 미보존 대상 포함).
# ==============================================================================

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

# 요청 처리 중에만 값이 있다 — 요청 밖(기동 로그 등)에서는 None
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# LogRecord 기본 속성 목록 — extra=로 전달된 사용자 필드만 골라내는 기준
_RESERVED_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


class JsonLineFormatter(logging.Formatter):
    """레코드를 JSON 한 줄로 직렬화한다. 공통 필드는 timestamp·level·logger·event
    (+문맥 식별자·extra 필드 최상위 합류)."""

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
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and key not in payload:
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


def setup_logging(level: str) -> None:
    """루트 로거를 JSON stdout 핸들러 하나로 구성한다(재호출 시 재구성)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = _StdoutHandler()
    handler.setFormatter(JsonLineFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # 접근 로그는 request_context 미들웨어가 남긴다 — uvicorn 기본 접근 로그와 중복 방지
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
