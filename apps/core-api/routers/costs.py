# ==============================================================================
# [파일 설명]  담당: 김승철 (API 계약 · BE)
# GET /api/v1/costs/by-asset-type — 고객사 하나의 최근 30일 자산 유형별 자원 사용 금액(#572 11번)입니다.
#
#   - 응답은 공개 계약 schemas.api.costs.AssetCostsResponse 로만 직렬화한다.
#   - 대상 고객사: 고객사 계정은 자기 고객사(tenant_id 생략 또는 같은 값). 플랫폼 계정은 tenant_id 가 필요하고
#     범위(tenant_scope_ids) 밖이면 404 다 — 범위 밖은 없는 것과 같다(고객사 상세와 같은 규칙).
#   - ACTIVE 가 아닌 고객사는 Cost Explorer 를 부르지 않고 UNAVAILABLE `TENANT_<상태>` 다(메트릭 축과 같은 규칙).
#   - 읽지 못해도 500 이 아니라 200 + UNAVAILABLE 이다. 화면은 자산 개수 비율로 대신 그린다.
#
# 받아 두기 — Cost Explorer 는 호출(페이지)마다 과금되고 데이터가 최대 24시간 늦게 반영된다.
#   - 구간은 [오늘(UTC) − 30일, 오늘(UTC)) 이다. 날짜가 바뀌면 구간이 바뀌어 그날 첫 요청이 새로 받는다.
#     같은 날의 두 번째 요청부터는 받아 둔 값을 준다. 스케줄러는 두지 않는다.
#   - 대시보드는 실시간 이벤트마다 다시 그린다 — 같은 키로 동시에 들어온 요청은 먼저 온 요청의 호출 하나를 기다린다.
#   - 성공은 그날 내내, 실패는 _FAILURE_TTL 동안만 둔다. 실패를 담지 않으면 이벤트마다 다시 부르고, 하루를
#     담으면 권한을 고친 뒤에도 그날 내내 실패로 남는다.
#   - 프로세스 메모리라 uvicorn worker 1개를 전제한다(메트릭의 CloudWatch 캐시와 같다). 재기동하면 고객사마다
#     한 번 더 부를 뿐이다. worker 를 늘리면 DB 표로 옮긴다.
# ==============================================================================

from __future__ import annotations

import logging
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import NamedTuple

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from schemas.api.auth import AccountType
from schemas.api.costs import AssetCostsResponse, CostItem
from schemas.api.errors import ErrorCode
from schemas.api.metrics import AxisStatus
from schemas.api.tenants import TenantStatus

from auth.access import require_user, tenant_scope_ids
from auth.service import CurrentUser
from db.repositories import tenants as tenants_repo
from db.session import get_db
from exceptions import ApiError
from services.aws.client import TenantAccess
from services.costs import CostUnavailable, fetch_costs

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["costs"])

#: 조회 구간(일). 화면 문구 「최근 30일」의 근거다.
PERIOD_DAYS = 30

#: 실패를 다시 묻기까지의 시간 — 이벤트마다 다시 그리는 화면이 실패한 호출을 되풀이하지 않게 한다.
_FAILURE_TTL = timedelta(minutes=10)


def _now() -> datetime:
    """조회 시각. 테스트가 날짜 경계와 실패 보존 시간을 재현하려고 바꿔 끼운다."""
    return datetime.now(timezone.utc)


class _CostKey(NamedTuple):
    """받아 둔 결과의 키 — 요청을 정하는 값이 전부 들어간다.

    접근 재료(고객사 · 역할 ARN · ExternalId)가 바뀌면 다른 키다 — 역할을 다시 만든 고객사가 옛 실패를 물려받지
    않는다. 관제 리전이 바뀌어도 다른 키다. ExternalId 는 repr 에 실리지 않는다.
    """

    access: TenantAccess
    regions: tuple[str, ...]
    period_end: date


class _Entry(NamedTuple):
    response: AssetCostsResponse
    #: None 이면 그날 내내 쓴다(성공). 실패는 이 시각까지만 쓴다.
    expires_at: datetime | None


class _InFlight:
    """진행 중인 조회 하나. 같은 키로 동시에 들어온 요청이 그 결과를 함께 받는다."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: AssetCostsResponse | None = None


_cost_cache: dict[_CostKey, _Entry] = {}
_cost_inflight: dict[_CostKey, _InFlight] = {}
#: 두 사전만 지킨다 — Cost Explorer 를 부르는 동안에는 쥐지 않는다.
_cost_cache_lock = threading.Lock()


@router.get("/costs/by-asset-type", response_model=AssetCostsResponse)
def get_asset_costs(
    tenant_id: str | None = Query(
        None, description="고객사 ID. 고객사 계정은 생략한다(자기 고객사). 플랫폼 계정은 필요하다."
    ),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_user),
) -> AssetCostsResponse:
    target = _target_tenant(user, tenant_id)
    row = tenants_repo.get_tenant(db, target, scope_ids=tenant_scope_ids(user))
    if row is None:
        raise ApiError(ErrorCode.TENANT_NOT_FOUND)
    tenant = row.tenant
    status = tenant.status
    regions = tuple(tenant.regions)
    access = TenantAccess(
        tenant_id=tenant.tenant_id,
        access_role_arn=tenant.access_role_arn,
        external_id=tenant.external_id,
    )
    # 값을 옮겨 담은 뒤 트랜잭션을 끝낸다 — Cost Explorer 를 기다리는 동안 풀 연결을 쥐지 않는다.
    db.rollback()

    now = _now()
    period_end = now.date()
    period_start = period_end - timedelta(days=PERIOD_DAYS)
    if status is not TenantStatus.ACTIVE:
        return _unavailable(f"TENANT_{status.value}", period_start, period_end, regions, now)
    return _cached(_CostKey(access, regions, period_end), period_start)


def _target_tenant(user: CurrentUser, tenant_id: str | None) -> str:
    """조회할 고객사(정규 UUID 문자열). 범위 판정은 저장소 조회가 한다 — 여기는 대상만 고른다."""
    if user.account_type is AccountType.TENANT and user.tenant_id is not None:
        if tenant_id is None:
            return user.tenant_id
    elif tenant_id is None:
        # 플랫폼 계정은 고객사를 골라야 한다 — 범위 전체의 합을 주지 않는다.
        raise ApiError(ErrorCode.REQUEST_VALIDATION_FAILED)
    try:
        return str(uuid.UUID(tenant_id))
    except ValueError:
        raise ApiError(ErrorCode.TENANT_NOT_FOUND) from None


def _cached(key: _CostKey, period_start: date) -> AssetCostsResponse:
    """그날 받아 둔 결과를 준다. 없거나 실패가 만료됐으면 한 번 받는다(동시 요청은 그 하나를 기다린다)."""
    with _cost_cache_lock:
        hit = _cost_cache.get(key)
        if hit is not None and (hit.expires_at is None or _now() < hit.expires_at):
            return hit.response
        flight = _cost_inflight.get(key)
        leader = flight is None
        if flight is None:
            flight = _cost_inflight[key] = _InFlight()

    if not leader:
        flight.done.wait()
        assert flight.response is not None
        return flight.response

    try:
        response, entry = _fetch(key, period_start)
        flight.response = response
        with _cost_cache_lock:
            # 지난 구간의 결과는 버린다 — 날짜가 바뀐 뒤에는 아무도 그 키로 묻지 않는다.
            for stale in [k for k in _cost_cache if k.period_end < key.period_end]:
                del _cost_cache[stale]
            _cost_cache[key] = entry
        return response
    finally:
        with _cost_cache_lock:
            del _cost_inflight[key]
        flight.done.set()


def _fetch(key: _CostKey, period_start: date) -> tuple[AssetCostsResponse, _Entry]:
    """Cost Explorer 를 한 번 부른다. 실패는 응답(UNAVAILABLE)으로 바꾸고 짧게만 둔다 — 예외를 올리지 않는다."""
    try:
        fetched = fetch_costs(
            key.access, regions=key.regions, period_start=period_start, period_end=key.period_end
        )
    except CostUnavailable as exc:
        _log.warning("유형별 비용 조회 실패 — UNAVAILABLE 로 답한다(%s)", exc.reason_code)
        now = _now()
        response = _unavailable(exc.reason_code, period_start, key.period_end, key.regions, now)
        return response, _Entry(response, now + _FAILURE_TTL)

    items = [CostItem(bucket=bucket, amount=amount) for bucket, amount in fetched.amounts]
    response = AssetCostsResponse(
        status=AxisStatus.READY,
        period_start=period_start,
        period_end=key.period_end,
        regions=list(key.regions),
        # 합은 서버가 싣는다 — 반올림한 칸의 합이라 화면의 합계와 칸 합이 어긋나지 않는다.
        total=sum((item.amount for item in items), Decimal("0")),
        items=items,
        estimated=fetched.estimated,
        retrieved_at=_now(),
    )
    return response, _Entry(response, None)


def _unavailable(
    reason_code: str, period_start: date, period_end: date, regions: tuple[str, ...], now: datetime
) -> AssetCostsResponse:
    return AssetCostsResponse(
        status=AxisStatus.UNAVAILABLE,
        reason_code=reason_code,
        period_start=period_start,
        period_end=period_end,
        regions=list(regions),
        retrieved_at=now,
    )
