import assert from 'node:assert/strict';
import { test } from 'node:test';

import { costOfType, costPeriodLabel, costSlices, costTotal, type AssetCostsResponse } from './asset-costs.ts';

const ready: AssetCostsResponse = {
  status: 'READY',
  reason_code: null,
  currency: 'USD',
  period_start: '2026-10-04',
  period_end: '2026-11-03',
  total: '100.00',
  items: [
    { bucket: 'EC2', amount: '75.00' },
    { bucket: 'EBS', amount: '25.00' },
    { bucket: 'ALB_TARGET_GROUP', amount: '0.00' },
    { bucket: 'OTHER', amount: '0.00' },
  ],
  retrieved_at: '2026-11-03T00:10:00Z',
};

test('막대 조각은 0원을 빼고 고정 순서 · 합계 대비 비율이다', () => {
  assert.deepEqual(costSlices(ready), [
    { bucket: 'EC2', amount: 75, ratio: 0.75 },
    { bucket: 'EBS', amount: 25, ratio: 0.25 },
  ]);
  assert.equal(costTotal(ready), 100);
});

test('유형 금액: 요금 없는 유형은 FREE, 못 읽으면 null', () => {
  assert.equal(costOfType(ready, 'EC2'), 75);
  assert.equal(costOfType(ready, 'SG'), 'FREE');
  const unavailable: AssetCostsResponse = { ...ready, status: 'UNAVAILABLE', reason_code: 'COST_EXPLORER_ACCESS_DENIED', total: null, items: [] };
  assert.equal(costOfType(unavailable, 'EC2'), null);
  assert.equal(costOfType(unavailable, 'NACL'), 'FREE');
  assert.deepEqual(costSlices(unavailable), []);
});

test('구간 표기는 끝 날짜를 포함하지 않으므로 하루 앞을 적는다', () => {
  assert.equal(costPeriodLabel(ready), '10/4부터 11/2까지');
});
