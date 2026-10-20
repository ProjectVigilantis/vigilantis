"""진행 중인 로그인 흐름 저장소 — 프로세스 메모리 전용 (Post-MVP T-1, #492 PR 1).

uvicorn worker 1개 전제. 키는 불투명 토큰의 SHA-256 이며 토큰 원문은 저장하지 않는다.
DB · 로그에 쓰지 않는다. `take` 는 항목을 꺼내 소유권을 넘긴다 — 같은 토큰의 동시 요청은 하나만 진행한다.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections import deque
from typing import Generic, TypeVar

T = TypeVar("T")


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class FlowStore(Generic[T]):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, T]] = {}

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)

    def put(self, token: str, value: T, expires_at: float) -> None:
        with self._lock:
            self._items[_key(token)] = (expires_at, value)

    def take(self, token: str, now: float) -> tuple[T, bool] | None:
        """항목을 꺼내 제거한다. 없으면 None, 있으면 (값, 만료 여부) — 만료 항목도 정리할 수 있게 돌려준다."""
        with self._lock:
            entry = self._items.pop(_key(token), None)
        if entry is None:
            return None
        return entry[1], entry[0] <= now

    def sweep(self, now: float) -> list[T]:
        """만료 항목을 제거하고 돌려준다(호출자가 정리 작업을 한다)."""
        with self._lock:
            dead = [k for k, (exp, _) in self._items.items() if exp <= now]
            return [self._items.pop(k)[1] for k in dead]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class FailureLimiter:
    """키별 슬라이딩 윈도 실패 카운터 — 프로세스 메모리 전용, 스레드 안전."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if not hits:
            del self._hits[key]
        return hits

    def record(self, key: str, now: float) -> None:
        with self._lock:
            self._prune(key, now)
            self._hits.setdefault(key, deque()).append(now)

    def blocked(self, key: str, now: float) -> bool:
        with self._lock:
            return len(self._prune(key, now)) >= self._limit
