# ==============================================================================
# [파일 설명]  담당: 안성일 (BE · 접근 보호)
# GET /api/v1/platform/overview — 플랫폼 상태 · 고객사별 API 호출 통계(#572 6번) 외부 DTO입니다.
# 중앙 관리계정(PLATFORM · CENTRAL_ADMIN)의 대시보드만 부릅니다. 그 밖의 계정은 403 AUTH_FORBIDDEN 이다.
# 호출 통계 구역의 모양은 관리자 대시보드의 FE 제안 계약(apps/web/src/lib/platform-status.ts, #578)을 따른다 —
# 고객사 × 1시간 히트맵과 과다 호출 판정을 그리는 자리다(PR #599 리뷰).
#
# 값의 원천 — 새 수집을 만들지 않고 이미 저장된 기록을 센다.
#   - tenants    : `tenants.status`(TenantStatus) 별 고객사 수. 세 상태 전부를 0 이어도 싣는다.
#   - collection : 최근 COLLECTION_WINDOW_HOURS 시간 안에 끝난(IN_PROGRESS 는 시작한) 수집 회차(`collection_runs`) 수를
#                  CollectionRunStatus 별로 센다. 회차는 (고객사, 리전) 단위라 고객사 수와 다르다.
#   - incidents  : `incidents.status`(IncidentStatus) 별 인시던트 수. 여섯 상태 전부를 0 이어도 싣는다 — 대시보드는
#                  할 일 목록이 아니라 상태를 한눈에 보는 자리라 조치 대상이 아닌 상태도 센다.
#   - api_usage  : 고객사(TENANT) 계정이 세션을 통과해 `/api/*` 를 부른 횟수를 **고객사 × UTC 1시간**으로 센 것
#                  (`tenant_api_calls_hourly`). 세션 전 호출(로그인 6단계 · 비밀번호 재설정 · 로그아웃)과 WebSocket,
#                  플랫폼 계정의 호출은 세지 않는다. 응답 코드와 무관하게 1회다(403 · 404 · 422 도 호출이다).
#                  경로별 세분화는 없다. 보관은 오늘과 그 전 API_USAGE_RETENTION_DAYS 일(고객사당 최대 31 × 24 칸)이라
#                  최대 창 API_USAGE_MAX_WINDOW_HOURS 를 하루 중 언제 조회해도 지워진 칸이 끼지 않는다.
#   - 과다 호출 기준(`threshold_per_hour`)은 고객사마다 다르다(자산 · 계정 수에 따라 평소 호출량이 다르다). 계산식은
#                  PM 이 따로 정하므로 지금은 null 이고, 판정 입력 `asset_count` · `staff_count` 만 싣는다. 계산은 서버가
#                  한다 — FE 는 계수를 갖지 않는다.
#
# 계약 원칙 (tenants.py 와 동일)
#   - snake_case, UTC ISO 8601("Z") 문자열, extra 금지. 시간 칸은 UTC 정시이고 KST 변환은 FE 가 한다.
#   - 호출이 0인 고객사도 `api_usage.items` 에 싣는다 — 상위 N · 나머지 묶기는 FE 가 한다.
#
# 경로 (/api/v1)
#   GET /platform/overview?hours=24 → PlatformOverviewResponse. hours 는 1..API_USAGE_MAX_WINDOW_HOURS(기본
#     API_USAGE_DEFAULT_WINDOW_HOURS), 그 밖은 422 REQUEST_VALIDATION_FAILED.
#     기간: `hours` 의 마지막 칸은 조회 시각을 UTC 정시로 내린 시각(진행 중인 칸), 첫 칸은 그 (hours - 1)시간 전.
#           칸은 1시간 간격으로 빠짐없이 오래된 것부터 나열한다.
#     항목: `hourly` 는 `hours` 와 같은 길이 · 순서이고 호출 없는 칸은 0 이다. `total_calls` 는 그 합이다.
#     순서: items 는 total_calls 내림차순 → tenant_name 오름차순 → tenant_id 오름차순.
# ==============================================================================

from __future__ import annotations

from datetime import UTC, timedelta
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .assets import UtcDateTime
from .incidents import IncidentStatus
from .tenants import TenantStatus

#: 호출 통계 보관 기간(일). 오늘과 그 전 이 일수의 칸을 남긴다.
API_USAGE_RETENTION_DAYS = 30
#: 조회 기간 상한(시간) = 보관 기간을 시간으로 편 값. 오늘 치를 더 남기므로 어느 시각에 조회해도 보관 안이다.
API_USAGE_MAX_WINDOW_HOURS = API_USAGE_RETENTION_DAYS * 24
#: hours 를 생략했을 때의 조회 기간(시간) — 대시보드 기본 창.
API_USAGE_DEFAULT_WINDOW_HOURS = 24
#: 수집 회차 집계 기간(시간).
COLLECTION_WINDOW_HOURS = 24

_HOUR = timedelta(hours=1)


def _check_status_counts(by_status: dict, total: int, enum_cls: type[Enum], label: str) -> None:
    if set(by_status) != set(enum_cls):
        raise ValueError(f"by_status 는 {enum_cls.__name__} 전부를 가져야 합니다")
    if any(count < 0 for count in by_status.values()):
        raise ValueError(f"{label} 수는 음수일 수 없습니다")
    if sum(by_status.values()) != total:
        raise ValueError("total 은 상태별 수의 합이어야 합니다")


class TenantCounts(BaseModel):
    """연결 상태별 고객사 수. by_status 는 TenantStatus 세 값을 전부 가진다."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    by_status: dict[TenantStatus, int]

    @model_validator(mode="after")
    def _complete(self):
        _check_status_counts(self.by_status, self.total, TenantStatus, "고객사")
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
    """상태별 인시던트 수. by_status 는 IncidentStatus 여섯 값을 전부 가진다(tenants.by_status 와 같은 모양)."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    by_status: dict[IncidentStatus, int]

    @model_validator(mode="after")
    def _complete(self):
        _check_status_counts(self.by_status, self.total, IncidentStatus, "인시던트")
        return self


class TenantApiUsage(BaseModel):
    """고객사 하나의 시간별 호출 수와 과다 호출 판정 입력. hourly 는 ApiUsageSummary.hours 와 같은 길이 · 순서."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1)
    tenant_name: str = Field(min_length=1)
    tenant_status: TenantStatus
    #: 그 고객사의 수집 자산 수(`assets.tenant_id` 기준, 사라진 자산 제외) — 과다 호출 기준 계산의 입력.
    asset_count: int = Field(ge=0)
    #: 그 고객사의 계정 수(`app_users.tenant_id` 기준, 상태 무관) — 과다 호출 기준 계산의 입력.
    staff_count: int = Field(ge=0)
    #: 한 시간에 이 수를 넘으면 과다 호출. 계산식이 정해지기 전에는 null 이며 FE 는 판정을 그리지 않는다.
    threshold_per_hour: int | None = Field(default=None, ge=1)
    total_calls: int = Field(ge=0)
    hourly: list[int]

    @model_validator(mode="after")
    def _consistent(self):
        if any(calls < 0 for calls in self.hourly):
            raise ValueError("시간별 호출 수는 음수일 수 없습니다")
        if sum(self.hourly) != self.total_calls:
            raise ValueError("total_calls 는 hourly 의 합이어야 합니다")
        return self


class ApiUsageSummary(BaseModel):
    """고객사별 시간 단위 호출 통계 — hours 는 UTC 정시, 1시간 간격, 오래된 것부터. 호출 0인 고객사도 싣는다."""

    model_config = ConfigDict(extra="forbid")

    window_hours: int = Field(ge=1, le=API_USAGE_MAX_WINDOW_HOURS)
    hours: list[UtcDateTime]
    retention_days: int = Field(ge=1)
    items: list[TenantApiUsage]

    @model_validator(mode="after")
    def _consistent(self):
        if len(self.hours) != self.window_hours:
            raise ValueError("hours 는 window_hours 개여야 합니다")
        # 직렬화는 UTC 로 나가므로 정시 판정도 UTC 로 옮긴 뒤 한다 — 다른 오프셋의 정시(+05:30 01:00 등)는 UTC 정시가 아니다
        utc_hours = [at.astimezone(UTC) if at.tzinfo else at.replace(tzinfo=UTC) for at in self.hours]
        for index, at in enumerate(utc_hours):
            if at.minute or at.second or at.microsecond:
                raise ValueError("hours 의 칸은 UTC 정시여야 합니다")
            if index and at - utc_hours[index - 1] != _HOUR:
                raise ValueError("hours 는 1시간 간격으로 오래된 것부터 빠짐없이 나열해야 합니다")
        if self.window_hours > self.retention_days * 24:
            raise ValueError("조회 기간은 보관 기간을 넘을 수 없습니다")
        keys = [(-item.total_calls, item.tenant_name, item.tenant_id) for item in self.items]
        if keys != sorted(keys):
            raise ValueError("items 는 total_calls 내림차순 · tenant_name · tenant_id 오름차순이어야 합니다")
        if len({item.tenant_id for item in self.items}) != len(self.items):
            raise ValueError("같은 고객사가 두 번 나올 수 없습니다")
        for item in self.items:
            if len(item.hourly) != self.window_hours:
                raise ValueError("hourly 는 hours 와 같은 길이여야 합니다")
        return self


class PlatformOverviewResponse(BaseModel):
    """중앙 관리계정 대시보드의 플랫폼 상태 · 호출 통계. 머리말 「경로」."""

    model_config = ConfigDict(extra="forbid")

    generated_at: UtcDateTime
    tenants: TenantCounts
    collection: CollectionCounts
    incidents: IncidentCounts
    api_usage: ApiUsageSummary
