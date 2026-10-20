import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { IncidentListItem, ThreatEventAxis } from '../types/api.ts';
import { postureOf, recentDone, savingsTotal, threatHours, timeAgo } from './customer-dashboard.ts';

const NOW = Date.parse('2026-10-20T05:30:00Z');

function incident(over: Partial<IncidentListItem> & Pick<IncidentListItem, 'incident_id'>): IncidentListItem {
  return {
    title: null,
    subject_arn: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-1',
    category: 'SECOPS',
    status: 'AWAITING_APPROVAL',
    initial_risk_level: 'MEDIUM',
    reviewed_risk_level: null,
    response_mode: null,
    threat_context: null,
    analysis_result: null,
    created_at: '2026-10-20T05:00:00Z',
    updated_at: '2026-10-20T05:00:00Z',
    ...over,
  };
}

test('위험도 높음 보안 건이 있으면 위험, 보안 건만 있으면 주의다', () => {
  const high = postureOf({ queue: [incident({ incident_id: 'a', initial_risk_level: 'HIGH' })], threatAssets: 1, collection: 'READY' });
  assert.equal(high.level, 'critical');
  assert.match(high.headline, /1건/);
  const medium = postureOf({ queue: [incident({ incident_id: 'b' })], threatAssets: 0, collection: 'READY' });
  assert.equal(medium.level, 'warning');
});

test('비용 건만 있으면 상태를 올리지 않는다', () => {
  const p = postureOf({
    queue: [incident({ incident_id: 'c', category: 'FINOPS', initial_risk_level: null })],
    threatAssets: 0,
    collection: 'READY',
  });
  assert.equal(p.level, 'good');
  assert.match(p.detail, /비용 절감 제안 1건/);
});

test('인시던트를 못 읽으면 정상이라 하지 않는다', () => {
  assert.equal(postureOf({ queue: null, threatAssets: 0, collection: 'READY' }).level, 'unknown');
});

test('수집 일부 실패는 주의다', () => {
  assert.equal(postureOf({ queue: [], threatAssets: 0, collection: 'PARTIAL' }).level, 'warning');
});

test('절감액은 추정 있는 비용 제안만 더하고 추정 없음은 따로 센다', () => {
  const rows = [
    { incident: incident({ incident_id: 'f1', category: 'FINOPS' }), savings: 62.05 },
    { incident: incident({ incident_id: 'f2', category: 'FINOPS' }), savings: null },
    { incident: incident({ incident_id: 's1' }), savings: null },
  ];
  assert.deepEqual(savingsTotal(rows), { amount: 62.05, counted: 1, unknown: 1 });
});

test('위협 이벤트는 24칸으로 펴고 빈 칸은 0이다', () => {
  const axis: ThreatEventAxis = {
    status: 'READY',
    reason_code: null,
    points: [
      { at: '2026-10-20T05:10:00Z', total: 2, counts: { SSH_BRUTE_FORCE: 2 } },
      { at: '2026-10-19T07:00:00Z', total: 1, counts: { OPEN_IP: 1 } },
      { at: '2026-10-18T00:00:00Z', total: 9, counts: { OPEN_IP: 9 } },
    ],
  };
  const hours = threatHours(axis, '2026-10-20T05:30:00Z');
  assert.ok(hours);
  assert.equal(hours.length, 24);
  assert.equal(hours[23].at, '2026-10-20T05:00:00.000Z');
  assert.equal(hours[23].total, 2);
  assert.equal(hours[1].total, 1);
  assert.equal(hours.reduce((s, h) => s + h.total, 0), 3);
});

test('축을 못 읽었으면 0건 그래프가 아니라 null이다', () => {
  assert.equal(threatHours({ status: 'UNAVAILABLE', reason_code: 'X', points: [] }, '2026-10-20T05:30:00Z'), null);
});

test('최근 끝난 건은 7일 안의 종료 · 종료 대기만 최신순으로', () => {
  const list = [
    incident({ incident_id: 'old', status: 'RESOLVED', updated_at: '2026-10-10T00:00:00Z' }),
    incident({ incident_id: 'r1', status: 'RESOLVED', updated_at: '2026-10-19T00:00:00Z' }),
    incident({ incident_id: 'c1', status: 'AWAITING_CLOSURE', updated_at: '2026-10-20T03:00:00Z' }),
    incident({ incident_id: 'open', status: 'AWAITING_APPROVAL', updated_at: '2026-10-20T04:00:00Z' }),
  ];
  assert.deepEqual(recentDone(list, NOW)?.map((i) => i.incident_id), ['c1', 'r1']);
});

test('상대 시각', () => {
  assert.equal(timeAgo('2026-10-20T05:12:00Z', NOW), '18분 전');
  assert.equal(timeAgo('2026-10-20T02:30:00Z', NOW), '3시간 전');
  assert.equal(timeAgo('2026-10-18T05:30:00Z', NOW), '2일 전');
});
