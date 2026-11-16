import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { TenantListItem } from '../types/api.ts';
import { outOfScopeResults, tenantSearchQuery, tenantsByGroup, TENANT_SEARCH_MAX } from './tenant-groups.ts';

test('tenantSearchQuery: 공백은 보내지 않고, 한도를 넘는 검색어도 보내지 않는다', () => {
  assert.equal(tenantSearchQuery('   '), null);
  assert.equal(tenantSearchQuery('  광주  '), '광주');
  assert.equal(tenantSearchQuery('a'.repeat(TENANT_SEARCH_MAX)), 'a'.repeat(TENANT_SEARCH_MAX));
  assert.equal(tenantSearchQuery('a'.repeat(TENANT_SEARCH_MAX + 1)), null);
});

test('outOfScopeResults: 담당 밖 결과만 남긴다', () => {
  const items = [
    { tenant_id: 't1', name: '가', accessible: true },
    { tenant_id: 't2', name: '나', accessible: false },
  ];
  assert.deepEqual(outOfScopeResults(items).map((i) => i.tenant_id), ['t2']);
});

test('tenantsByGroup: 그룹별로 이름순으로 묶고, 그룹 없는 고객사는 null 키에 둔다', () => {
  const tenant = (tenant_id: string, name: string, group_id: string | null) => ({ tenant_id, name, group_id }) as TenantListItem;
  const byGroup = tenantsByGroup([tenant('t1', '다', 'g1'), tenant('t2', '가', 'g1'), tenant('t3', '나', null)]);
  assert.deepEqual(byGroup.get('g1')?.map((t) => t.tenant_id), ['t2', 't1']);
  assert.deepEqual(byGroup.get(null)?.map((t) => t.tenant_id), ['t3']);
  assert.equal(byGroup.get('g2'), undefined);
});
