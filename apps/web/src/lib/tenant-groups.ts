// 고객사 그룹(#572 2번)과 고객사 검색(#572 7번) 화면이 쓰는 계산. 화면 밖에 두어 `node --test`로 확인한다.

import type { TenantListItem, TenantSearchItem } from '../types/api.ts';

/** 그룹 이름 한도. 서버 `GroupName`(schemas/api/groups.py)이 1-100자로 막는다. */
export const GROUP_NAME_MAX = 100;

/** `GET /tenants/search`의 `q` 한도. 서버 `Query(min_length=1, max_length=100)`와 같다. */
export const TENANT_SEARCH_MAX = 100;

/** 서버 검색에 보낼 검색어. 공백뿐이거나 한도를 넘으면 null이다(서버가 422로 거절한다). */
export function tenantSearchQuery(raw: string): string | null {
  const q = raw.trim();
  return q === '' || q.length > TENANT_SEARCH_MAX ? null : q;
}

/**
 * 검색 결과 중 담당 밖(`accessible=false`)만 남긴다. 담당 안 고객사는 목록 표가 이미 보이므로 두 번 그리지 않는다.
 * 담당 밖 고객사는 상세가 403이라 화면은 이름과 권한 없음만 보인다.
 */
export function outOfScopeResults(items: readonly TenantSearchItem[]): TenantSearchItem[] {
  return items.filter((item) => !item.accessible);
}

/** 그룹 ID별 고객사(이름순). 키 null은 담당 그룹이 없는 고객사다. */
export function tenantsByGroup(tenants: readonly TenantListItem[]): Map<string | null, TenantListItem[]> {
  const byGroup = new Map<string | null, TenantListItem[]>();
  for (const tenant of [...tenants].sort((a, b) => a.name.localeCompare(b.name, 'ko'))) {
    const list = byGroup.get(tenant.group_id);
    if (list) list.push(tenant);
    else byGroup.set(tenant.group_id, [tenant]);
  }
  return byGroup;
}
