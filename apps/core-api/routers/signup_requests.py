# ==============================================================================
# [파일 설명]
# /api/v1/signup-requests — 도입 신청. (#552, 계약: schemas.api.signup_requests)
#
#   - 공개 접수(POST)와 중앙 관리자 처리(GET · PUT)가 한 라우터에 있어 전역 보호 의존성 없이 마운트한다.
#     각 라우트가 의존성을 직접 건다: POST 는 Origin 만(세션 없음), 관리 라우트는 중앙 관리자 관문 + PUT 은 Origin.
#   - 재시도 키는 Idempotency-Key 헤더(UUIDv4)로 받고 202 로 응답한다. 기존 actions 의 본문 키 · 200 재생과 다르다.
#   - 얇게 둔다: 판정 · 잠금 · 트랜잭션은 signup_requests_service 가 맡는다.
# ==============================================================================

from __future__ import annotations

from typing import Annotated

import signup_requests_service as service
from auth.access import require_central_admin
from auth.http import require_origin
from db.session import get_db
from fastapi import APIRouter, Depends, Header
from pydantic import UUID4
from schemas.api.signup_requests import (
    SignupRequestAccepted,
    SignupRequestCreate,
    SignupRequestItem,
    SignupRequestsResponse,
    SignupRequestStatusRequest,
)
from sqlalchemy.orm import Session
from support_access import Actor, get_actor

_DB = Depends(get_db)
_ACTOR = Depends(get_actor)

router = APIRouter(prefix="/api/v1/signup-requests", tags=["signup-requests"])


@router.post(
    "",
    status_code=202,
    response_model=SignupRequestAccepted,
    dependencies=[Depends(require_origin)],
)
def create_signup_request(
    body: SignupRequestCreate,
    idempotency_key: Annotated[UUID4, Header(alias="Idempotency-Key")],
    db: Session = _DB,
) -> SignupRequestAccepted:
    return service.create(db, idempotency_key, body)


@router.get(
    "",
    response_model=SignupRequestsResponse,
    dependencies=[Depends(require_central_admin)],
)
def list_signup_requests(
    db: Session = _DB, actor: Actor = _ACTOR
) -> SignupRequestsResponse:
    return service.list_requests(db, actor)


@router.put(
    "/{request_id}/status",
    response_model=SignupRequestItem,
    dependencies=[Depends(require_origin), Depends(require_central_admin)],
)
def change_signup_request_status(
    request_id: str,
    body: SignupRequestStatusRequest,
    db: Session = _DB,
    actor: Actor = _ACTOR,
) -> SignupRequestItem:
    return service.change_status(db, actor, request_id, body)
