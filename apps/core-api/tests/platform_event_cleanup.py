"""커밋된 시드를 지우는 테스트 정리 전용 — 플랫폼 활동 로그(append-only · #589)를 함께 지운다.

platform_events 는 BEFORE UPDATE OR DELETE 트리거가 지우기를 거절한다. 사람 행은 행위자 사용자를 FK(RESTRICT)로
가리켜 그 사용자를 지우려면 먼저 지워야 하고, 로그인 · MFA · 재설정 행은 행위자가 ANONYMOUS 라 FK 가 없어
사용자를 지워도 **남는다** — 대상(target_id) · 소속(tenant_id)으로 찾아 지우지 않으면 다음 테스트로 샌다.

행이 있을 때만 트리거를 이 정리 트랜잭션 안에서 끈다 — ALTER 는 테이블 전체 잠금이라 다른 연결의 열린
트랜잭션을 기다리면 제한 시간에 실패한다. 제품 코드는 활동 로그를 지우지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text


def purge_platform_events(
    session,
    *,
    actor_user_ids: Iterable[str] = (),
    target_ids: Iterable[str] = (),
    tenant_ids: Iterable[str] = (),
) -> None:
    """그 사용자가 행위자이거나, 그 ID 가 대상이거나, 그 고객사에 속한 행을 지운다. commit 은 호출부가 한다."""
    keys = {
        "actors": [str(value) for value in actor_user_ids],
        "targets": [str(value) for value in target_ids],
        "tenants": [str(value) for value in tenant_ids],
    }
    if not any(keys.values()):
        return
    held = session.scalars(
        text(
            "SELECT event_id::text FROM platform_events WHERE actor_user_id::text = ANY(:actors)"
            " OR target_id = ANY(:targets) OR tenant_id::text = ANY(:tenants)"
        ),
        keys,
    ).all()
    if not held:
        return
    session.execute(text("SET LOCAL lock_timeout = '10s'"))
    session.execute(text("ALTER TABLE platform_events DISABLE TRIGGER platform_events_append_only"))
    session.execute(
        text("DELETE FROM platform_events WHERE event_id::text = ANY(:ids)"), {"ids": held}
    )
    session.execute(text("ALTER TABLE platform_events ENABLE TRIGGER platform_events_append_only"))
