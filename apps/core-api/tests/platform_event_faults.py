"""플랫폼 활동 로그(#589) 쓰기를 일부러 실패시키는 테스트 도우미.

「상태 변경은 같은 트랜잭션」을 확인할 때 쓴다 — 활동 로그 행을 쓰지 못하면 그 업무 변경도 commit 되지 않아야
한다. 실패는 흉내가 아니라 **DB 가 거절하게** 만든다: INSERT 직전에 행동(action) 칸을 비워 NOT NULL 제약에
걸리게 한다. 결과(성공 · 거절 · 실패)와 무관하게 어느 행동에나 쓸 수 있다.

한 commit 에 활동 로그 행이 둘 이상 실리는 자리(예: 실행 확정의 ACTION_COMPLETE + 시작 관문 거절의
ACTION_BLOCK)는 `only` 로 한 행동만 실패시킨다. 기록 함수는 행마다 바로 flush 하므로, 전부 실패시키면 먼저
쓰는 행이 예외를 올려 뒤의 기록 자리에는 닿지도 않는다 — 그 자리가 실패를 삼켜도 테스트가 통과한다.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from db import models
from schemas.platform_events import PlatformEventAction
from sqlalchemy import event


@dataclass
class _Failed:
    count: int = 0  # 실패시킨 INSERT 수 — 0 이면 기록이 시도되지 않은 것이다(테스트가 다른 이유로 통과하지 않게 본다)


@contextmanager
def failing_platform_event_write(only: PlatformEventAction | None = None):
    """이 블록 안의 platform_events INSERT 는 IntegrityError 로 실패한다.

    only 를 주면 그 행동의 행만 실패시키고 나머지는 그대로 쓴다. count 도 실패시킨 행만 센다."""
    failed = _Failed()

    def corrupt(_mapper, _connection, target):
        if only is not None and target.action != only.value:
            return
        failed.count += 1
        target.action = None  # NOT NULL 위반

    event.listen(models.PlatformEvent, "before_insert", corrupt)
    try:
        yield failed
    finally:
        event.remove(models.PlatformEvent, "before_insert", corrupt)
