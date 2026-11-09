# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · 접근 보호)
# GET /api/v1/platform/overview — 플랫폼 상태 · 고객사별 API 호출 통계(#572 6번) 외부 DTO입니다.
# 중앙 관리계정(PLATFORM · CENTRAL_ADMIN)의 대시보드만 부릅니다. 그 밖의 계정은 403 AUTH_FORBIDDEN 이다.
#
# 값의 원천 — 새 수집을 만들지 않고 이미 저장된 기록을 센다.
#   - tenants    : `tenants.status`(TenantStatus) 별 고객사 수. 세 상태 전부를 0 이어도 싣는다.
#   - collection : 최근 COLLECTION_WINDOW_HOURS 시간 안에 끝난(IN_PROGRESS 는 시작한) 수집 회차(`collection_runs`) 수를
#                  CollectionRunStatus 별로 센다. 회차는 (고객사, 리전) 단위라 고객사 수와 다르다.
#   - incidents  : `incidents.status = AWAITING_APPROVAL`(실행 가능한 제안이 있고 실행 중이 아닌 인시던트) 건수.
#   - api_usage  : 고객사(TENANT) 계정이 세션을 통과해 `/api/*` 를 부른 횟수를 **고객사 × UTC 날짜**로 센 것
#                  (`tenant_api_calls_daily`). 세션 전 호출(로그인 6단계 · 비밀번호 재설정 · 로그아웃)과 WebSocket,
#                  플랫폼 계정의 호출은 세지 않는다. 응답 코드와 무관하게 1회다(403 · 404 · 422 도 호출이다).
#                  경로별 세분화는 없다. 보관은 API_USAGE_RETENTION_DAYS 일(오늘 포함)이다.
#
# 계약 원칙 (tenants.py 와 동일)
#   - snake_case, UTC ISO 8601("Z") 문자열, 날짜는 "YYYY-MM-DD"(UTC), extra 금지.
#   - 호출이 0인 고객사도 `api_usage.items` 에 싣는다 — 대시보드가 고객사 표를 그대로 그린다.
#
# 경로 (/api/v1)
#   GET /platform/overview?days=7 → PlatformOverviewResponse. days 는 1..API_USAGE_RETENTION_DAYS(기본
#     API_USAGE_DEFAULT_WINDOW_DAYS), 그 밖은 422 REQUEST_VALIDATION_FAILED.
#     기간: window_end = 조회 시각의 UTC 날짜, window_start = window_end - (days - 1). 양 끝 포함.
#     순서: items 는 total_calls 내림차순 → tenant_name 오름차순 → tenant_id 오름차순. daily 는 day 오름차순이고
#           호출이 없는 날은 빠진다.
# ==============================================================================

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .assets import UtcDateTime
from .tenants import TenantStatus

#: 호출 통계 보관 기간(일, 오늘 포함). 조회 기간 상한이기도 하다.
API_USAGE_RETENTION_DAYS = 30
#: days 를 생략했을 때의 조회 기간(일).
API_USAGE_DEFAULT_WINDOW_DAYS = 7
#: 수집 회차 집계 기간(시간).
COLLECTION_WINDOW_HOURS = 24


class TenantCounts(BaseModel):
    """연결 상태별 고객사 수. by_status 는 TenantStatus 세 값을 전부 가진다."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    by_status: dict[TenantStatus, int]

    @model_validator(mode="after")
    def _complete(self):
        if set(self.by_status) != set(TenantStatus):
            raise ValueError("by_status 는 TenantStatus 전부를 가져야 합니다")
        if any(count < 0 for count in self.by_status.values()):
            raise ValueError("고객사 수는 음수일 수 없습니다")
        if sum(self.by_status.values()) != self.total:
            raise ValueError("total 은 상태별 수의 합이어야 합니다")
        return self


class CollectionCounts(BaseModel):
    """최근 window_hours 시간의 수집 회차 수 — CollectionRunStatus 별."""

    model_config = ConfigDict(extra="forbid")

    window_hours: int = Field(ge=1)
    success: int = Field(ge=0)
    partial: int = Field(ge=0)
    failed: int = Field(ge=0)
    in_progress: int = Field(ge=0)


class IncidentCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    awaiting_approval: int = Field(ge=0)


class TenantApiUsageDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date
    calls: int = Field(ge=1)


class TenantApiUsage(BaseModel):
    """고객사 하나의 기간 안 호출 수. daily 는 호출이 있던 날만, day 오름차순."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    tenant_name: str = Field(min_length=1)
    tenant_status: TenantStatus
    total_calls: int = Field(ge=0)
    daily: list[TenantApiUsageDay]

    @model_validator(mode="after")
    def _consistent(self):
        days = [point.day for point in self.daily]
        if days != sorted(set(days)):
            raise ValueError("daily 는 day 오름차순이고 같은 날이 두 번 나올 수 없습니다")
        if sum(point.calls for point in self.daily) != self.total_calls:
            raise ValueError("total_calls 는 daily 의 합이어야 합니다")
        return self


class ApiUsageSummary(BaseModel):
    """고객사별 호출 통계 — [window_start, window_end] 양 끝 포함, 호출 0인 고객사도 싣는다."""

    model_config = ConfigDict(extra="forbid")

    window_start: date
    window_end: date
    window_days: int = Field(ge=1, le=API_USAGE_RETENTION_DAYS)
    retention_days: int = Field(ge=1)
    items: list[TenantApiUsage]

    @model_validator(mode="after")
    def _consistent(self):
        if self.window_end - self.window_start != timedelta(days=self.window_days - 1):
            raise ValueError("window_days 는 window_start..window_end 의 날 수와 같아야 합니다")
        if self.window_days > self.retention_days:
            raise ValueError("조회 기간은 보관 기간을 넘을 수 없습니다")
        keys = [(-item.total_calls, item.tenant_name, item.tenant_id) for item in self.items]
        if keys != sorted(keys):
            raise ValueError("items 는 total_calls 내림차순 · tenant_name · tenant_id 오름차순이어야 합니다")
        if len({item.tenant_id for item in self.items}) != len(self.items):
            raise ValueError("같은 고객사가 두 번 나올 수 없습니다")
        for item in self.items:
            for point in item.daily:
                if not self.window_start <= point.day <= self.window_end:
                    raise ValueError("daily 의 day 는 기간 안이어야 합니다")
        return self


class PlatformOverviewResponse(BaseModel):
    """중앙 관리계정 대시보드의 플랫폼 상태 · 호출 통계. 머리말 「경로」."""

    model_config = ConfigDict(extra="forbid")

    generated_at: UtcDateTime
    tenants: TenantCounts
    collection: CollectionCounts
    incidents: IncidentCounts
    api_usage: ApiUsageSummary
