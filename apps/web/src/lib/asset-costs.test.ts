import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  costFailureNote,
  costOfType,
  costPeriodLabel,
  costSlices,
  costTotal,
  pastCostTypes,
  readyCostView,
  type AssetCostsResponse,
} from './asset-costs.ts';

const ready: AssetCostsResponse = {
  status: 'READY',
  reason_code: null,
  currency: 'USD',
  period_start: '2026-10-04',
  period_end: '2026-11-03',
  regions: ['ap-northeast-2'],
  total: '100.00',
  items: [
    { bucket: 'EC2', amount: '75.00' },
    { bucket: 'EBS', amount: '25.00' },
    { bucket: 'ALB_TARGET_GROUP', amount: '0.00' },
    { bucket: 'OTHER', amount: '0.00' },
  ],
  estimated: false,
  retrieved_at: '2026-11-03T00:10:00Z',
};

const zero: AssetCostsResponse = {
  ...ready,
  total: '0.00',
  items: ready.items.map((i) => ({ ...i, amount: '0.00' })),
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

test('정상 0원 응답은 조회 성공이다: 합계 0원으로 그리고 실패 문구를 내지 않는다', () => {
  assert.deepEqual(costSlices(zero), []);
  assert.deepEqual(readyCostView(zero), { total: 0, slices: [], other: 0 });
  assert.equal(costFailureNote({ kind: 'ready', data: zero }), null);
  assert.equal(costFailureNote({ kind: 'ready', data: ready }), null);
});

test('못 읽은 경우만 실패 문구를 낸다', () => {
  const unavailable: AssetCostsResponse = { ...ready, status: 'UNAVAILABLE', reason_code: 'COST_EXPLORER_ACCESS_DENIED', total: null, items: [] };
  assert.equal(readyCostView(unavailable), null);
  assert.equal(costFailureNote({ kind: 'ready', data: unavailable }), '비용을 읽지 못했습니다(COST_EXPLORER_ACCESS_DENIED).');
  assert.equal(costFailureNote({ kind: 'missing' }), '비용 API가 서버에 아직 없습니다.');
  assert.equal(costFailureNote({ kind: 'failed' }), '비용을 불러오지 못했습니다.');
});

test('지금 자산에 없는 유형도 기간 안 비용이 있으면 요약 줄 후보가 된다', () => {
  // EBS 25달러가 나갔지만 볼륨은 이미 지웠다
  assert.deepEqual(pastCostTypes(ready, ['EC2', 'SG']), ['EBS']);
  // 자산이 하나도 없어도 비용이 난 유형은 남는다
  assert.deepEqual(pastCostTypes(ready, []), ['EC2', 'EBS']);
  // 0원 유형과 이미 있는 유형은 넣지 않는다
  assert.deepEqual(pastCostTypes(ready, ['EC2', 'EBS']), []);
  assert.deepEqual(pastCostTypes(zero, []), []);
});

test('구간 표기는 끝 날짜를 포함하지 않으므로 하루 앞을 적는다', () => {
  assert.equal(costPeriodLabel(ready), '10/4부터 11/2까지');
});
