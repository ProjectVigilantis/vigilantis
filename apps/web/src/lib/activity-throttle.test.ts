// 유휴 연장 간격 제한 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ACTIVITY_INTERVAL_MS, createActivityThrottle } from './activity-throttle.ts';

test('첫 입력은 바로 보내고, 60초 안의 입력은 보내지 않는다', () => {
  let clock = 1_000;
  const shouldSend = createActivityThrottle(ACTIVITY_INTERVAL_MS, () => clock);
  assert.equal(shouldSend(), true);
  clock += 1;
  assert.equal(shouldSend(), false);
  clock += ACTIVITY_INTERVAL_MS - 2;
  assert.equal(shouldSend(), false);
  clock += 1; // 마지막 전송에서 정확히 60초
  assert.equal(shouldSend(), true);
  clock += 30_000;
  assert.equal(shouldSend(), false);
});

test('간격은 마지막 "전송" 기준이다 — 막힌 입력이 창을 늘리지 않는다', () => {
  let clock = 0;
  const shouldSend = createActivityThrottle(60_000, () => clock);
  assert.equal(shouldSend(), true);
  for (clock = 10_000; clock < 60_000; clock += 10_000) assert.equal(shouldSend(), false);
  clock = 60_000;
  assert.equal(shouldSend(), true);
});
