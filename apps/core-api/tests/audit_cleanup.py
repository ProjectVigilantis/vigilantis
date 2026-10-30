"""커밋된 시드를 지우는 테스트 정리 전용 — 감사 기록(append-only · T-2 #544)을 함께 지운다.

audit_events 는 BEFORE UPDATE OR DELETE 트리거가 지우기를 거절하고, 인시던트 · 실행 · 사용자를
FK 로 가리킨다. 그래서 커밋된 인시던트 · 실행 · 사용자를 지우는 정리는 그 전에 이 함수를 부른다 —
트리거를 정리 트랜잭션 안에서만 끄고 다시 켠다(테이블 소유자만 할 수 있다 · 마이그레이션 b544a7d1e2f3 머리말).
제품 코드는 감사 기록을 지우지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text


def purge_audit_events(
    session,
    *,
    incident_ids: Iterable[str] = (),
    execution_ids: Iterable[str] = (),
    actor_user_ids: Iterable[str] = (),
) -> None:
    keys = {
        "incidents": [str(value) for value in incident_ids],
        "executions": [str(value) for value in execution_ids],
        "actors": [str(value) for value in actor_user_ids],
    }
    if not any(keys.values()):
        return
    session.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only"))
    session.execute(
        text(
            "DELETE FROM audit_events WHERE incident_id::text = ANY(:incidents)"
            " OR execution_id::text = ANY(:executions)"
            " OR actor_user_id::text = ANY(:actors)"
        ),
        keys,
    )
    session.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only"))
