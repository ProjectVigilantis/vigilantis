import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { ApiUsageSummary, TenantApiUsage } from '../types/api.ts';
import { exceedsThreshold, hasOverCallThreshold, overCallTenants, usageLevel } from './platform-status.ts';

const hours = ['2026-11-03T00:00:00Z', '2026-11-03T01:00:00Z', '2026-11-03T02:00:00Z'];

const item = (tenant_id: string, hourly: number[], threshold_per_hour: number | null): TenantApiUsage => ({
  tenant_id,
  tenant_name: tenant_id.toUpperCase(),
  tenant_status: 'ACTIVE',
  asset_count: 0,
  staff_count: 0,
  threshold_per_hour,
  total_calls: hourly.reduce((s, n) => s + n, 0),
  hourly,
});

const summary = (items: TenantApiUsage[]): ApiUsageSummary => ({ window_hours: 3, hours, retention_days: 30, items });

test('과다 호출: 고객사마다 자기 기준으로 판정하고, 최고 건수 큰 순으로 최고 · 마지막 초과 시각을 준다', () => {
  const usage = summary([
    item('a', [50, 60, 70], 100),
    item('b', [150, 20, 120], 100),
    item('c', [10, 400, 30], 300),
    // 건수는 가장 크지만 기준이 높아 넘지 않았다
    item('d', [500, 500, 500], 1000),
  ]);
  const over = overCallTenants(usage);
  assert.deepEqual(over.map((t) => t.tenant_id), ['c', 'b']);
  assert.equal(over[0].peakAt, hours[1]);
  assert.equal(over[0].peakRequests, 400);
  assert.equal(over[0].limit, 300);
  assert.equal(over[1].hoursOver, 2);
  assert.equal(over[1].lastOverAt, hours[2]);
});

test('과다 호출: 기준과 같은 건수는 넘은 것이 아니고, 기준이 없는 고객사는 판정하지 않는다', () => {
  assert.equal(overCallTenants(summary([item('a', [100, 0, 0], 100)])).length, 0);
  assert.equal(overCallTenants(summary([item('a', [9999, 0, 0], null)])).length, 0);
  assert.equal(exceedsThreshold(item('a', [], 100), 101), true);
  assert.equal(exceedsThreshold(item('a', [], 100), 100), false);
  assert.equal(exceedsThreshold(item('a', [], null), 9999), false);
});

test('기준이 정해진 고객사가 하나도 없으면 판정 자체가 없다', () => {
  assert.equal(hasOverCallThreshold([item('a', [1], null), item('b', [2], null)]), false);
  assert.equal(hasOverCallThreshold([item('a', [1], null), item('b', [2], 10)]), true);
  assert.equal(hasOverCallThreshold([]), false);
});

test('히트맵 단계: 0은 호출 없음, 가장 큰 값은 4', () => {
  assert.equal(usageLevel(0, 100), 0);
  assert.equal(usageLevel(1, 100), 1);
  assert.equal(usageLevel(51, 100), 3);
  assert.equal(usageLevel(100, 100), 4);
  assert.equal(usageLevel(5, 0), 0);
});
