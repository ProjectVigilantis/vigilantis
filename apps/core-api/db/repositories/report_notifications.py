"""#623이 읽는 #667 알림 이벤트 단위 발송 기록. 채널별 전달 원문은 읽지 않는다."""

from datetime import datetime

from schemas.platform_events import (
    PlatformActorKind,
    PlatformEventAction,
    PlatformEventCategory,
    PlatformEventOutcome,
    PlatformEventTargetType,
)
from sqlalchemy import String, cast, select
from sqlalchemy.orm import Session

from db import models


def list_send_events(
    db: Session, *, incident_id: str, tenant_id: str | None, as_of: datetime,
) -> list[models.PlatformEvent]:
    if tenant_id is None:
        # #667의 귀속 불명 기록은 중앙 조회용이다. 신고 초안의 고객사 사실로 옮기지 않는다.
        return []
    activity, notification = models.PlatformEvent, models.NotificationEvent
    return list(db.scalars(
        select(activity)
        .join(notification, activity.target_id == cast(notification.event_id, String))
        .where(
            notification.incident_id == incident_id,
            activity.tenant_id == tenant_id,
            activity.category == PlatformEventCategory.NOTIFICATION,
            activity.action == PlatformEventAction.NOTIFICATION_SEND.value,
            activity.target_type == PlatformEventTargetType.NOTIFICATION,
            activity.actor_kind == PlatformActorKind.SYSTEM,
            activity.outcome.in_((PlatformEventOutcome.SUCCEEDED, PlatformEventOutcome.FAILED)),
            activity.occurred_at <= as_of,
        )
        .order_by(activity.occurred_at, activity.event_id)
    ))
