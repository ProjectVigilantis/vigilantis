import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  failureRate,
  orderedComponents,
  overCallTenants,
  overallState,
  scanSummary,
  scopeApiUsage,
  usageLevel,
  type PlatformComponent,
} from './platform-status.ts';

const comp = (key: PlatformComponent['key'], state: PlatformComponent['state']): PlatformComponent => ({
  key,
  state,
  last_success_at: null,
  detail: null,
});

test('전체 상태는 가장 나쁜 구성 요소를 따르고 꺼 둔 것은 세지 않는다', () => {
  assert.equal(overallState([comp('API', 'OK'), comp('NOTIFIER', 'DEGRADED')]), 'DEGRADED');
  assert.equal(overallState([comp('API', 'OK'), comp('OPENAI', 'DOWN'), comp('NOTIFIER', 'DEGRADED')]), 'DOWN');
  assert.equal(overallState([comp('SCAN_SCHEDULER', 'DISABLED')]), 'OK');
  assert.equal(overallState([]), 'OK');
});

test('구성 요소는 정해진 순서로 편다', () => {
  const ordered = orderedComponents([comp('OPENAI', 'OK'), comp('API', 'OK'), comp('DATABASE', 'OK')]);
  assert.deepEqual(ordered.map((c) => c.key), ['API', 'DATABASE', 'OPENAI']);
});

test('수집 주기 요약: 주기 두 번 넘게 새 수집이 없으면 늦음', () => {
  const now = Date.parse('2026-11-02T10:00:00Z');
  const cycle = (minAgo: number, failed = 0) => ({
    started_at: new Date(now - minAgo * 60_000).toISOString(),
    duration_seconds: 60,
    tenants_total: 10,
    tenants_failed: failed,
  });
  const fresh = scanSummary({ interval_seconds: 300, cycles: [cycle(10, 2), cycle(5, 1)] }, now);
  assert.equal(fresh.lastAgeMinutes, 5);
  assert.equal(fresh.late, false);
  assert.equal(fresh.lastFailed, 1);
  assert.equal(fresh.cyclesWithFailure, 2);
  assert.equal(fresh.avgDurationSeconds, 60);

  assert.equal(scanSummary({ interval_seconds: 300, cycles: [cycle(11)] }, now).late, true);
  assert.equal(scanSummary({ interval_seconds: 300, cycles: [] }, now).late, true);
});

test('실패율은 요청이 없으면 null', () => {
  assert.equal(failureRate(0, 0), null);
  assert.equal(failureRate(400, 6), 1.5);
});

test('과다 호출: 기준을 넘은 고객사만, 최고 건수 큰 순, 최고 · 마지막 초과 시각을 함께 준다', () => {
  const hours = ['2026-11-03T00:00:00Z', '2026-11-03T01:00:00Z', '2026-11-03T02:00:00Z'];
  const usage = {
    window_hours: 3,
    hours,
    threshold_per_hour: 100,
    other_tenants: 0,
    tenants: [
      { tenant_id: 'a', tenant_name: 'A', hourly: [50, 60, 70] },
      { tenant_id: 'b', tenant_name: 'B', hourly: [150, 20, 120] },
      { tenant_id: 'c', tenant_name: 'C', hourly: [10, 400, 30] },
    ],
  };
  const over = overCallTenants(usage);
  assert.deepEqual(over.map((t) => t.tenant_id), ['c', 'b']);
  assert.equal(over[0].peakAt, hours[1]);
  assert.equal(over[0].peakRequests, 400);
  assert.equal(over[1].hoursOver, 2);
  assert.equal(over[1].lastOverAt, hours[2]);
  // 기준과 같은 건수는 넘은 것이 아니다
  assert.equal(overCallTenants({ ...usage, tenants: [{ tenant_id: 'd', tenant_name: 'D', hourly: [100] }] }).length, 0);
  // 기준이 없으면 판정하지 않는다
  assert.equal(overCallTenants({ ...usage, threshold_per_hour: null }).length, 0);
});

test('히트맵 단계: 0은 호출 없음, 가장 큰 값은 4', () => {
  assert.equal(usageLevel(0, 100), 0);
  assert.equal(usageLevel(1, 100), 1);
  assert.equal(usageLevel(51, 100), 3);
  assert.equal(usageLevel(100, 100), 4);
  assert.equal(usageLevel(5, 0), 0);
});

test('담당 범위로 좁히면 다른 고객사 줄이 빠지고 나머지 수는 담당 고객사 기준이다', () => {
  const usage = {
    window_hours: 1,
    hours: ['2026-11-03T00:00:00Z'],
    threshold_per_hour: 100,
    other_tenants: 50,
    tenants: [
      { tenant_id: 'A', tenant_name: 'A', hourly: [500] },
      { tenant_id: 'b', tenant_name: 'B', hourly: [10] },
    ],
  };
  const scoped = scopeApiUsage(usage, ['a', 'c', 'd']);
  assert.deepEqual(scoped.tenants.map((t) => t.tenant_id), ['A']);
  assert.equal(scoped.other_tenants, 2);
  assert.deepEqual(overCallTenants(scoped).map((t) => t.tenant_id), ['A']);
});
