# ==============================================================================
# [파일 설명]
# /api/v1/groups 의 판정 · 트랜잭션 · 응답 조립 — 고객사 그룹 (#572 2번 · 계약: schemas.api.groups)
#
#   - 라우터는 얇게 두고 여기서 commit 한다. 저장소(db.repositories.tenant_groups)는 commit 하지 않는다.
#   - 조회: 중앙 관리계정은 모든 그룹, 관리자 직원계정은 세션 판정 시점의 담당 그룹만 받는다(고객사를 추가할 때
#     고를 그룹). 고객사(TENANT) 계정은 라우터 관문이 403 으로 막는다.
#   - 쓰기(생성 · 삭제)는 중앙 관리계정만 한다. users 잠금 → 캐시 비움 → 행위자 재판정(중앙 관리계정) → 본문 → commit 이며
#     예외는 전부 rollback 이다(admin_service._transaction — 직원 관리와 같은 경계). 의존성이 본 뒤 강등 · 세션 회수된
#     행위자는 401/403 이고 아무것도 쓰지 않는다.
#   - 이름 중복은 DB 유일성이 SAVEPOINT 안에서 거절하고 409 GROUP_NAME_CONFLICT 로 답한다. 이름은 받은 그대로 저장한다.
#   - 삭제는 그 그룹의 고객사를 그룹 없음으로 돌리고(FK SET NULL) 직원의 그 그룹 배정을 지운다(CASCADE) — 세션 판정이
#     매 요청 배정을 다시 읽으므로 다음 요청부터 그 직원들의 범위에서 빠진다.
# ==============================================================================

from __future__ import annotations

from auth import admin_service, service
from auth.access import is_central_admin, is_platform_account
from db.repositories import platform_events as platform_events_repo
from db.repositories import tenant_groups as groups_repo
from exceptions import ApiError
from identifiers import canonical_id
from schemas.api.auth import PlatformRole
from schemas.api.errors import ErrorCode
from schemas.api.groups import GroupCreateRequest, GroupItem, GroupsResponse
from schemas.platform_events import PlatformEventAction
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _item(row: groups_repo.GroupWithCount) -> GroupItem:
    group = row.group
    return GroupItem(
        group_id=group.group_id,
        name=group.name,
        tenant_count=row.tenant_count,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


def _scope(user: service.CurrentUser) -> tuple[str, ...] | None:
    """볼 수 있는 그룹 — None 은 전체(중앙 관리계정뿐), 관리자 직원계정은 담당 그룹, 그 밖은 없음."""
    if is_central_admin(user):
        return None
    if is_platform_account(user) and user.platform_role is PlatformRole.TENANT_MANAGER:
        return user.assigned_group_ids
    return ()


def list_groups(db: Session, user: service.CurrentUser) -> GroupsResponse:
    rows = groups_repo.list_groups(db, scope_group_ids=_scope(user))
    return GroupsResponse(items=[_item(row) for row in rows])


def create_group(db: Session, actor: admin_service.Actor, body: GroupCreateRequest) -> GroupItem:
    with admin_service._transaction(db, actor, central=True) as user:
        try:
            # SAVEPOINT — 유일성 거절이 이 쓰기만 되돌리고 409 로 답하게 한다
            with db.begin_nested():
                group = groups_repo.create_group(db, name=body.name, now=actor.clock())
        except IntegrityError as exc:
            if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == groups_repo.GROUP_NAME_UNIQUE:
                raise ApiError(ErrorCode.GROUP_NAME_CONFLICT) from exc
            raise
        platform_events_repo.record_platform_event(
            db,
            PlatformEventAction.TENANT_GROUP_CREATE,
            platform_events_repo.person(user),
            target_id=group.group_id,
        )
        # 새 그룹에는 고객사가 없다 — 다시 세지 않는다
        return _item(groups_repo.GroupWithCount(group, 0))


def delete_group(db: Session, actor: admin_service.Actor, group_id: str) -> None:
    """없거나 형식이 어긋난 ID 는 404 GROUP_NOT_FOUND — 행위자 판정(401 · 403) 다음이다."""
    stored_id = canonical_id(group_id)
    with admin_service._transaction(db, actor, central=True) as user:
        group = None if stored_id is None else groups_repo.get_group(db, stored_id)
        if group is None:
            raise ApiError(ErrorCode.GROUP_NOT_FOUND)
        target_id = group.group_id
        tenant_ids, staff_user_ids = groups_repo.group_member_ids(db, target_id)
        if not groups_repo.delete_group(db, target_id):
            raise ApiError(ErrorCode.GROUP_NOT_FOUND)
        platform_events_repo.record_platform_event(
            db,
            PlatformEventAction.TENANT_GROUP_DELETE,
            platform_events_repo.person(user),
            target_id=target_id,
            details={"tenant_ids": tenant_ids, "staff_user_ids": staff_user_ids},
        )
