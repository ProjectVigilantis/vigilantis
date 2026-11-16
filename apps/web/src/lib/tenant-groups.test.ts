import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  assignmentRows, GROUP_FILTER_ALL, GROUP_FILTER_NONE, outOfScopeResults, tenantSearchQuery, TENANT_SEARCH_MAX,
} from './tenant-groups.ts';

test('assignmentRows: 덮어 쓴 그룹으로 필터를 가르고 이름순으로 낸다', () => {
  const rows = [
    { tenant_id: 't1', name: '다', aws_account_id: '1', group_id: 'g1' },
    { tenant_id: 't2', name: '가', aws_account_id: '2', group_id: null },
    { tenant_id: 't3', name: '나', aws_account_id: '3', group_id: 'g1' },
  ];
  // t2를 g1으로 저장하는 중이다.
  const groupOf = (t: (typeof rows)[number]) => (t.tenant_id === 't2' ? 'g1' : t.group_id);
  const all = () => true;
  assert.deepEqual(assignmentRows(rows, groupOf, GROUP_FILTER_ALL, all).map((t) => t.tenant_id), ['t2', 't3', 't1']);
  assert.deepEqual(assignmentRows(rows, groupOf, 'g1', all).map((t) => t.tenant_id), ['t2', 't3', 't1']);
  assert.deepEqual(assignmentRows(rows, groupOf, GROUP_FILTER_NONE, all), []);
  assert.deepEqual(assignmentRows(rows, groupOf, GROUP_FILTER_ALL, (t) => t.name === '나').map((t) => t.tenant_id), ['t3']);
});

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