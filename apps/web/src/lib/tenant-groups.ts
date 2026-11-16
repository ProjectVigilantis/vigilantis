// 고객사 그룹(#572 2번)과 고객사 검색(#572 7번) 화면이 쓰는 계산. 화면 밖에 두어 `node --test`로 확인한다.

import type { TenantListItem, TenantSearchItem } from '../types/api.ts';

/** 그룹 이름 한도. 서버 `GroupName`(schemas/api/groups.py)이 1-100자로 막는다. */
export const GROUP_NAME_MAX = 100;

/** `GET /tenants/search`의 `q` 한도. 서버 `Query(min_length=1, max_length=100)`와 같다. */
export const TENANT_SEARCH_MAX = 100;

/**
 * `GET /tenants/search`가 돌려주는 최대 건수. 서버 `TENANT_SEARCH_LIMIT`(schemas/api/tenants.py)와 같다.
 * 서버는 담당 안팎을 가리지 않고 이름순 앞 20건을 자르므로, 결과가 이만큼이면 담당 밖 일치가 더 있을 수 있다.
 */
export const TENANT_SEARCH_RESULT_MAX = 20;

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

/** 배정 표의 그룹 필터. `ALL`은 전체, `NONE`은 그룹 없는 고객사, 그 밖은 그룹 ID다. */
export const GROUP_FILTER_ALL = 'ALL';
export const GROUP_FILTER_NONE = 'NONE';

/**
 * 배정 표에 보일 고객사(이름순). `groupOf`는 저장 중인 값을 덮어 쓴 지금 그룹이다. 필터는 그 값으로 가르므로
 * 저장하는 동안에도 행이 필터에서 바로 빠지거나 들어온다.
 */
export function assignmentRows<T extends Pick<TenantListItem, 'tenant_id' | 'name' | 'aws_account_id'>>(
  tenants: readonly T[],
  groupOf: (tenant: T) => string | null,
  filter: string,
  matches: (tenant: T) => boolean,
): T[] {
  return tenants
    .filter((t) => {
      const g = groupOf(t);
      if (filter === GROUP_FILTER_NONE) return g === null;
      if (filter !== GROUP_FILTER_ALL) return g === filter;
      return true;
    })
    .filter(matches)
    .sort((a, b) => a.name.localeCompare(b.name, 'ko'));
}
