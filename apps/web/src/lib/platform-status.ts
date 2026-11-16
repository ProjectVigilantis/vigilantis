// 플랫폼 상태: 중앙 관리계정 대시보드 맨 위 구역의 계산. 계약은 서버 `GET /api/v1/platform/overview`
// (packages/schemas/api/platform.py, 타입은 types/api.ts `PlatformOverviewResponse`)다. 중앙 관리계정만 부르며 그 밖은 403이다
// (lib/roles.ts `canViewPlatformStatus`). 서버가 전 고객사를 싣고, 상위 N · 나머지 묶기는 FE가 한다.
//
// 과다 호출 기준(`threshold_per_hour`)은 고객사마다 다르고 서버가 계산한다. 계산식이 정해지기 전에는 null이며, 그 고객사는 판정하지 않는다.

import type { ApiUsageSummary, TenantApiUsage } from '../types/api.ts';

type IsoDateTime = string;

export interface OverCallTenant {
  tenant_id: string;
  tenant_name: string;
  /** 그 고객사의 과다 호출 기준(시간당 건수). */
  limit: number;
  /** 가장 많이 부른 시간 칸의 시작 시각과 건수. */
  peakAt: IsoDateTime;
  peakRequests: number;
  /** 기준을 넘은 시간 칸 수와 그중 가장 최근 칸의 시작 시각. */
  hoursOver: number;
  lastOverAt: IsoDateTime;
}

/** 기준을 한 시간이라도 넘은 고객사. 최고 건수가 큰 순. 기준이 없는(null) 고객사는 판정하지 않는다. */
export function overCallTenants(usage: ApiUsageSummary): OverCallTenant[] {
  const out: OverCallTenant[] = [];
  for (const t of usage.items) {
    const limit = t.threshold_per_hour;
    if (limit === null) continue;
    let peak = -1;
    let peakIdx = -1;
    let hoursOver = 0;
    let lastOverIdx = -1;
    t.hourly.forEach((n, i) => {
      if (i >= usage.hours.length) return;
      if (n > peak) {
        peak = n;
        peakIdx = i;
      }
      if (n > limit) {
        hoursOver += 1;
        lastOverIdx = i;
      }
    });
    if (hoursOver === 0) continue;
    out.push({
      tenant_id: t.tenant_id,
      tenant_name: t.tenant_name,
      limit,
      peakAt: usage.hours[peakIdx],
      peakRequests: peak,
      hoursOver,
      lastOverAt: usage.hours[lastOverIdx],
    });
  }
  return out.sort((a, b) => b.peakRequests - a.peakRequests);
}

/** 기준이 정해진 고객사가 한 곳이라도 있는가. 없으면 과다 호출을 판정하지 않았다고 적는다. */
export function hasOverCallThreshold(items: readonly TenantApiUsage[]): boolean {
  return items.some((t) => t.threshold_per_hour !== null);
}

/** 그 칸이 그 고객사의 기준을 넘었는가. */
export function exceedsThreshold(t: TenantApiUsage, n: number): boolean {
  return t.threshold_per_hour !== null && n > t.threshold_per_hour;
}

/** 히트맵 칸 진하기 단계(0-4). 0은 호출 없음, 4는 표에서 가장 큰 값 근처다. */
export function usageLevel(requests: number, max: number): 0 | 1 | 2 | 3 | 4 {
  if (requests <= 0 || max <= 0) return 0;
  return Math.min(4, Math.max(1, Math.ceil((requests / max) * 4))) as 1 | 2 | 3 | 4;
}
