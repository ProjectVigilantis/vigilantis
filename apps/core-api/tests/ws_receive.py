"""WebSocket 테스트 공용 — 시간 제한이 있는 수신 헬퍼."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import pytest


def receive_text_bounded(ws, timeout: float = 5.0) -> str:
    # TestClient 의 receive_text 는 동기 블로킹이라 authorize 가 모두 거절하면 영원히 멈춘다 — 시간 제한으로 CI 멈춤 대신 실패시킨다
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(ws.receive_text).result(timeout=timeout)
    except FutureTimeout:
        pytest.fail(f"{timeout}초 안에 WebSocket 메시지를 받지 못했다 — authorize 가 모든 이벤트를 거절하는지 확인")
    finally:
        pool.shutdown(wait=False)
