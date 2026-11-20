# ==============================================================================
# [파일 설명]
# 인시던트 보고서 최신 생성본 보관소 (#623). 인시던트당 1행 — 검증 · 마스킹을 마친 최종 마크다운 · JSON 바이트만
# 둔다(Prompt · 모델 원본 응답 · 고객 수정본 없음). 생성 후 7일이 지나면 조회가 막히고 정리 잡이 지운다.
#
#   - commit / rollback 은 호출자 몫이다. 저장을 되돌리면 이전 보관본이 그대로 남는다(삭제 후 삽입이 아닌 UPSERT).
#   - 더 오래된 생성은 더 새로운 보관본을 덮지 못한다: as_of 가 더 늦어야 하고, 같으면 generated_at 이 더 늦어야 한다.
# ==============================================================================

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db import models

RETENTION = timedelta(days=7)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def save_latest(
    db: Session,
    *,
    incident_id: str,
    tenant_id: str | None,
    as_of: datetime,
    generated_at: datetime,
    markdown: bytes,
    json_body: bytes,
) -> bool:
    """최신 생성본으로 저장한다. 저장했으면 True, 이미 더 새로운 생성본이 있어 저장하지 않았으면 False."""
    generated_at = _utc(generated_at)
    as_of = _utc(as_of)
    insert_stmt = insert(models.IncidentReportDraft).values(
        incident_id=incident_id,
        tenant_id=tenant_id,
        as_of=as_of,
        generated_at=generated_at,
        expires_at=generated_at + RETENTION,
        markdown=markdown,
        json_body=json_body,
    )
    table = models.IncidentReportDraft.__table__
    excluded = insert_stmt.excluded
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=[table.c.incident_id],
        set_={
            "tenant_id": excluded.tenant_id,
            "as_of": excluded.as_of,
            "generated_at": excluded.generated_at,
            "expires_at": excluded.expires_at,
            "markdown": excluded.markdown,
            "json_body": excluded.json_body,
        },
        where=(excluded.as_of > table.c.as_of)
        | ((excluded.as_of == table.c.as_of) & (excluded.generated_at > table.c.generated_at)),
    ).returning(table.c.incident_id)
    return db.execute(stmt).first() is not None


def get_latest(
    db: Session, *, incident_id: str, tenant_id: str | None, now: datetime
) -> models.IncidentReportDraft | None:
    """보관본이 있고 고객사가 정확히 같으며(None 은 None 과만) 만료 전이면 돌려준다."""
    if tenant_id is None:
        tenant_match = models.IncidentReportDraft.tenant_id.is_(None)
    else:
        tenant_match = models.IncidentReportDraft.tenant_id == tenant_id
    return db.execute(
        select(models.IncidentReportDraft)
        .where(
            models.IncidentReportDraft.incident_id == incident_id,
            tenant_match,
            models.IncidentReportDraft.expires_at > _utc(now),
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def delete_expired(db: Session, *, now: datetime) -> int:
    """만료 시각이 지났거나 같은 행을 지우고 지운 수를 돌려준다."""
    result = db.execute(
        delete(models.IncidentReportDraft).where(models.IncidentReportDraft.expires_at <= _utc(now))
    )
    return result.rowcount
