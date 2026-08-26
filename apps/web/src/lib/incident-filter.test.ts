// 프리셋 전환 시 필터 잔존 회귀 — `npm test`. PR #171 리뷰에서 막힌 경로입니다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ALL, clampStatus, visibleIncidents } from './incident-filter.ts';
import type { IncidentListItem, IncidentStatus } from '../types/api.ts';

function item(id: string, status: IncidentStatus): IncidentListItem {
  return {
    incident_id: id,
    title: null,
    subject_arn: `arn:aws:ec2:ap-northeast-2:1:instance/${id}`,
    category: 'SECOPS',
    status,
    initial_risk_level: 'HIGH',
    reviewed_risk_level: null,
    response_mode: null,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  };
}

test('지금 목록에 없는 상태 필터는 전체로 접힌다', () => {
  assert.equal(clampStatus('RESOLVED', ['AWAITING_APPROVAL']), ALL);
});

test('목록에 있는 값과 전체는 그대로 둔다', () => {
  assert.equal(clampStatus('AWAITING_APPROVAL', ['AWAITING_APPROVAL', 'RESOLVED']), 'AWAITING_APPROVAL');
  assert.equal(clampStatus(ALL, ['AWAITING_APPROVAL']), ALL);
});

test('프리셋 전환 후에는 전량이 보인다 — 이전 필터가 0건을 만들지 않는다', () => {
  // `전체`에서 `종료`를 고른 뒤 `승인 대기` 프리셋으로 넘어간 상황.
  const pendingList = [item('a', 'AWAITING_APPROVAL'), item('b', 'AWAITING_APPROVAL')];
  const visible = visibleIncidents(pendingList, ALL, 'RESOLVED');
  assert.equal(visible.length, pendingList.length);
});

test('유효한 필터는 그대로 걸린다', () => {
  const mixed = [item('a', 'AWAITING_APPROVAL'), item('b', 'RESOLVED')];
  assert.deepEqual(
    visibleIncidents(mixed, ALL, 'RESOLVED').map((i) => i.incident_id),
    ['b'],
  );
});
