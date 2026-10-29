# ==============================================================================
# [파일 설명]
# GET /api/v1/assets — 자산 목록·연결관계·최신 판정 조회 라우터입니다. (Issue #68)
#
#   - 응답은 공개 계약 schemas.api.assets.AssetsResponse로만 직렬화한다.
#     계약 불변식(판정 상태 ↔ verdict/skip, spec ↔ asset_type)은 DTO가 검증한다.
#   - SQL은 db.repositories 경유 — 라우터는 응답 조립만 한다.
#   - 필터·페이지네이션 없음(전체 반환) — SSOT §API 계약.
# ==============================================================================

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Sequence

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from schemas.api.assets import (
    AssetsResponse,
    AssetType,
    CollectionStatus,
    UncollectedAssetType,
)
from schemas.collections import CollectionRunStatus

from asset_mapping import to_asset_item
from auth.access import require_tenant_scope
from config import get_aws_settings
from db import models
from db.repositories import assets as assets_repo
from db.repositories import tenants as tenants_repo
from db.session import get_db
from services.collector import (
    REGION_FAILURE_LABEL,
    UNOBSERVED_TYPES_BY_FAILURE,
    VPC_SCOPE_FAILURE_LABEL,
)
from tenancy import MonitoredRegions, TenantScope

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["assets"])

_COLLECTION_STATUS = {
    CollectionRunStatus.IN_PROGRESS: CollectionStatus.COLLECTING,
    CollectionRunStatus.SUCCESS: CollectionStatus.READY,
    CollectionRunStatus.PARTIAL: CollectionStatus.PARTIAL,
    CollectionRunStatus.FAILED: CollectionStatus.FAILED,
}

# 리전별 최신 run 을 하나로 접을 때의 우선순위 — 나쁜 쪽이 이긴다. (Issue #231)
# IN_PROGRESS 가 SUCCESS 보다 위인 이유: 아직 안 끝난 리전이 있는데 READY 로 확정하면
# 다음 순간 FAILED 로 뒤집힌다. 실패는 진행 중보다 위다 — 실패를 늦게 보여줄 이유가 없다.
_STATUS_SEVERITY = {
    CollectionRunStatus.SUCCESS: 0,
    CollectionRunStatus.IN_PROGRESS: 1,
    CollectionRunStatus.PARTIAL: 2,
    CollectionRunStatus.FAILED: 3,
}


def _worst_status(runs: list[models.CollectionRun]) -> CollectionRunStatus:
    """리전별 최신 run 들 중 가장 나쁜 상태. 빈 목록은 호출 전에 걸러야 한다."""
    return max((run.status for run in runs), key=_STATUS_SEVERITY.__getitem__)


def _configured_regions() -> list[str]:
    """설정된 리전(AWS_REGIONS, 없으면 AWS_REGION) — 소속 없는 MVP 기록의 관제 범위. (#261)

    전체 범위의 관제 리전에 들어간다(_monitored_regions). collection_status·items·last_collected_at 을
    관제 리전으로 함께 좁혀 응답 안에서 리전 범위가 갈리지 않게 한다. (테스트는 이 함수를 monkeypatch 로 대체한다)
    """
    regions = get_aws_settings().regions_list()
    if not regions:
        # 관제 범위가 비면 세 필드가 빈 스코프가 되어 조용히 NOT_COLLECTED 가 된다 —
        # "아직 수집 안 함"과 "설정 오류"가 화면에서 구분되지 않는다. 같은 오설정에서
        # services/aws/client.default_region() 은 소리내어 죽으므로 여기서도 같은 결로 실패한다.
        raise RuntimeError("리전 해석 실패 — AWS_REGION / AWS_REGIONS 값을 확인할 것")
    return regions


def _monitored_regions(db: Session, scope: TenantScope) -> MonitoredRegions:
    """범위의 관제 대상 — 범위 안 고객사마다 (고객사, 그 테넌트 행의 관제 리전) 쌍. 전체 범위만 설정 리전(legacy)을 더한다.

    쌍으로 넘기는 것이 요점이다 — 리전 합집합으로 넘기면 A(서울) · B(도쿄)를 함께 볼 때 A 가 관제하지 않는 도쿄의
    옛 A 기록이 B 의 도쿄 때문에 들어온다. 설정 리전(_configured_regions)은 소속 없는 MVP 기록의 관제 범위다(#261) —
    배정 범위는 소속 없는 기록을 보지 않으므로 설정 리전을 읽지 않는다. 메트릭(routers.metrics)과 같은 규칙이다 —
    두 화면이 다른 범위를 보면 같은 순간에 자산 수와 곡선 수가 어긋난다. 빈 범위는 빈 값(거짓)이다. ARN 에서 리전 ·
    소유를 추론하지 않는다.
    """
    pairs = tuple(
        dict.fromkeys(
            (row.tenant.tenant_id, region)
            for row in tenants_repo.list_tenants(db, scope_ids=scope.tenant_ids)
            for region in row.tenant.regions
        )
    )
    if scope.is_platform:
        return MonitoredRegions(pairs=pairs, legacy=tuple(dict.fromkeys(_configured_regions())))
    return MonitoredRegions(pairs=pairs)


def _collection_status(
    runs: list[models.CollectionRun], regions: Sequence[str]
) -> CollectionStatus:
    """관제 리전 스코프 안에서 collection_status 산출. (#261) ``runs`` 는 범위 안 (소유 × 리전)별 최신 run 이다.

    ``regions`` 는 관제 리전의 합집합(MonitoredRegions.regions)이고 덮음은 종전대로 **리전 단위**다 — 한 리전에 범위 안
    run 이 하나라도 있으면 그 리전은 덮인 것으로 본다(쌍마다 따지지 않는다 — 알려진 한계).

    관제 리전 중 범위 안 run 이 하나도 없는 리전이 있으면(최초 수집 대기·수집 중 모두 해당)
    '전체 관제 범위 수집 미완료'로 보아 COLLECTING 을 하한으로 깐다 — 기존 리전의
    SUCCESS 만으로 READY 를 주지 않는다. 단 IN_PROGRESS 는 심각도상 PARTIAL·FAILED
    아래라, 다른 리전의 실제 실패는 그대로 드러난다(안성일 확정).
    """
    worst = _worst_status(runs)
    covered = {run.region for run in runs}
    if not set(regions) <= covered:  # 아직 run 이 없는 설정 리전이 있음
        worst = max(
            worst, CollectionRunStatus.IN_PROGRESS, key=_STATUS_SEVERITY.__getitem__
        )
    return _COLLECTION_STATUS[worst]


def _uncollected(runs: list[models.CollectionRun]) -> list[UncollectedAssetType]:
    """리전별 최신 run 의 `error_summary`(= collector 가 남긴 {수집 라벨: 사유 코드} JSON)를
    **자산 유형**으로 환원한다. 화면이 가진 축은 유형이지 수집기 라벨이 아니기 때문이다 —
    "대상 그룹 열이 왜 비었나"에 답하려면 `ALB_TARGET_GROUP`이 와야 한다.

    세 가지를 고의로 하지 않는다.

    1. **모르는 라벨은 지어내지 않는다.** 누가 흡수 조회를 새로 더하고
       ``UNOBSERVED_TYPES_BY_FAILURE``(라벨 → 그 조회로만 채워지는 유형)를 안 고치면,
       엉뚱한 유형을 지목하는 대신 아무 유형도 싣지 않고 경고만 남긴다. 적재 쪽 소멸 표시와
       같은 fail-closed 태도다. 이때도 `collection_status`는 PARTIAL·FAILED로 나가 이상은 보인다.
    2. **리전 전체 실패(`collect_region`)는 유형으로 펴지 않는다.** 그 회차는 전부를 못 본
       것이고 status 가 이미 FAILED다. 유형 7개를 나열하면 화면이 "이 일곱만 문제"라고 말한다.
       수집 범위 확인 실패(`vpc_scope`)도 유형으로 펴지 않는다 — 특정 유형이 아니라 회차의
       소멸 판단을 막은 것이며, status 가 PARTIAL로 이미 이상을 알린다.
    3. **사유가 여러 리전에서 갈려도 유형당 한 줄만 낸다.** 계약이 유형 중복을 금지한다.
       먼저 만난 사유를 남긴다 — 사유는 보조 정보이고, 유형이 빠졌다는 사실이 본론이다.
    """
    reason_by_type: dict[AssetType, str] = {}
    for run in runs:
        if not run.error_summary:
            continue
        try:
            failures: dict[str, str] = json.loads(run.error_summary)
        except (ValueError, TypeError):
            # 상한 초과로 항목을 버린 `_truncated` 표식도 여기 온다 — 유형을 모르므로 건너뛴다.
            _log.warning("리전 %s: error_summary 를 JSON 으로 읽지 못해 uncollected 산출에서 제외", run.region)
            continue
        for label, reason in failures.items():
            if label in (REGION_FAILURE_LABEL, VPC_SCOPE_FAILURE_LABEL):
                continue
            types = UNOBSERVED_TYPES_BY_FAILURE.get(label)
            if types is None:
                _log.warning("리전 %s: 모르는 수집 실패 라벨 %r — uncollected 에 싣지 않는다", run.region, label)
                continue
            for name in types:
                reason_by_type.setdefault(AssetType(name), reason)

    return [
        UncollectedAssetType(asset_type=t, reason_code=reason_by_type[t])
        for t in AssetType  # enum 선언 순서로 고정 — 응답이 회차마다 뒤바뀌지 않게
        if t in reason_by_type
    ]


@router.get("/assets", response_model=AssetsResponse)
def get_assets(
    db: Session = Depends(get_db),
    scope: TenantScope = Depends(require_tenant_scope),
) -> AssetsResponse:
    # 고객사 범위(#533 · #492): 범위는 신원에서 만든 것만 쓴다(기본값 없음). 중앙(전체)은 소속 없는 MVP
    # 기록까지, 담당자는 배정 고객사들만(배정 없음 = 아무것도), 고객사 계정은 자기 고객사만 본다. 자산 ·
    # 수집 회차 · 연결 · 판정을 모두 같은 범위로 읽고, 수집 상태 · 마지막 수집 시각 · uncollected 도 범위 안
    # 회차에서만 산출해 남의 회차가 내 상태를 바꾸지 않는다.
    # 관제 대상은 범위의 (고객사, 리전) 쌍과 전체 범위의 설정 리전(_monitored_regions)이다. 세 필드를 모두 이 범위로
    # 좁혀 응답 안에서 범위가 갈리지 않게 한다 — 수집 대상서 빠진 리전 · 고객사가 관제하지 않는 리전의 옛 run 이 화면을
    # 붙잡던 문제를 막는다(Issue #261, 안성일 확정). (소유 × 리전)별 최신 run 을 최악 상태로 접는 것은 #231 그대로 —
    # 리전 격리(C4) 이후 실패가 실행 순서에, 한 고객사의 실패가 같은 리전 다른 고객사의 더 새 run 에 가려지지 않게 한다.
    regions = _monitored_regions(db, scope)
    runs = assets_repo.latest_collection_run_per_region(db, regions=regions, scope=scope)
    if not runs:
        # 범위의 관제 리전 전체에 수집 이력이 없음 — 계약상 목록·last_collected_at도 비어야 한다
        return AssetsResponse(collection_status=CollectionStatus.NOT_COLLECTED)

    relationships: dict[str, list[models.AssetRelationship]] = defaultdict(list)
    for rel in assets_repo.list_all_relationships(db, scope=scope):
        relationships[rel.source_asset_id].append(rel)
    evaluations = assets_repo.latest_rule_evaluation_by_asset(db, scope=scope)

    items = [
        to_asset_item(asset, relationships.get(asset.asset_id, []), evaluations.get(asset.asset_id))
        for asset in assets_repo.list_assets(db, regions=regions, scope=scope)
    ]
    return AssetsResponse(
        collection_status=_collection_status(runs, regions.regions),
        last_collected_at=assets_repo.last_finished_collection_at(db, regions=regions, scope=scope),
        items=items,
        uncollected=_uncollected(runs),
    )
