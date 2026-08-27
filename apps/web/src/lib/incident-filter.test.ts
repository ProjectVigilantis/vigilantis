// 프리셋 판정·필터 잔존 회귀 — `npm test`. PR #171·#180 리뷰에서 막힌 경로와 v1.6 프리셋입니다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  ALL,
  byPreset,
  clampOption,
  isPreemptive,
  riskOptionsOf,
  statusOptionsOf,
  visibleIncidents,
} from './incident-filter.ts';
import type { IncidentListItem, IncidentStatus, ResponseMode, RiskLevel } from '../types/api.ts';

function item(
  id: string,
  status: IncidentStatus,
  responseMode: ResponseMode | null = null,
  risk: RiskLevel | null = 'HIGH',
): IncidentListItem {
  return {
    incident_id: id,
    title: null,
    subject_arn: `arn:aws:ec2:ap-northeast-2:1:instance/${id}`,
    category: 'SECOPS',
    status,
    initial_risk_level: risk,
    reviewed_risk_level: null,
    response_mode: responseMode,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  };
}

test('지금 목록에 없는 상태 필터는 전체로 접힌다', () => {
  assert.equal(clampOption('RESOLVED', ['AWAITING_APPROVAL']), ALL);
});

test('목록에 있는 값과 전체는 그대로 둔다', () => {
  assert.equal(
    clampOption('AWAITING_APPROVAL', ['AWAITING_APPROVAL', 'RESOLVED']),
    'AWAITING_APPROVAL',
  );
  assert.equal(clampOption(ALL, ['AWAITING_APPROVAL']), ALL);
});

test('프리셋 전환 후에는 그 프리셋의 전량이 보인다 — 이전 필터가 0건을 만들지 않는다', () => {
  // 기본에서 `종료`를 고른 뒤 `승인 대기` 프리셋으로 넘어간 상황.
  const pending = [item('a', 'AWAITING_APPROVAL'), item('b', 'AWAITING_APPROVAL')];
  assert.equal(visibleIncidents(pending, 'PENDING', 'RESOLVED').length, pending.length);
});

test('유효한 상태 필터는 그대로 걸린다', () => {
  const mixed = [item('a', 'AWAITING_APPROVAL'), item('b', 'ANALYZING')];
  assert.deepEqual(
    visibleIncidents(mixed, 'ACTIVE', 'ANALYZING').map((i) => i.incident_id),
    ['b'],
  );
});

// ── v1.6 프리셋 (§4.4)

test('기본(ACTIVE)은 종료를 빼고 진행 중 4종만 담는다', () => {
  const items = [
    item('analyzing', 'ANALYZING'),
    item('pending', 'AWAITING_APPROVAL'),
    item('running', 'ACTION_IN_PROGRESS'),
    item('failed', 'FAILED'),
    item('done', 'RESOLVED'),
  ];
  assert.deepEqual(
    byPreset(items, 'ACTIVE').map((i) => i.incident_id),
    ['analyzing', 'pending', 'running', 'failed'],
  );
});

test('진행 불가(FAILED)는 히스토리로 가지 않는다 — 사람 개입이 남은 상태다', () => {
  const items = [item('failed', 'FAILED'), item('done', 'RESOLVED')];
  assert.deepEqual(
    byPreset(items, 'HISTORY').map((i) => i.incident_id),
    ['done'],
  );
});

test('선제차단은 승인 없이 이미 격리된 두 response_mode만 담는다', () => {
  assert.equal(isPreemptive(item('a', 'AWAITING_APPROVAL', 'PRE_MITIGATION_0_5S')), true);
  assert.equal(isPreemptive(item('b', 'ACTION_IN_PROGRESS', 'TIMEOUT_ISOLATION_1M')), true);
  // AGENT_WAIT은 아직 실행 전이라 들어가지 않는다.
  assert.equal(isPreemptive(item('c', 'AWAITING_APPROVAL', 'AGENT_WAIT')), false);
  assert.equal(isPreemptive(item('d', 'ANALYZING', null)), false);
});

test('선제차단 프리셋도 종료된 건은 담지 않는다 — 진행 중의 부분집합이다', () => {
  const items = [
    item('live', 'AWAITING_APPROVAL', 'PRE_MITIGATION_0_5S'),
    item('closed', 'RESOLVED', 'PRE_MITIGATION_0_5S'),
  ];
  assert.deepEqual(
    byPreset(items, 'PREEMPTIVE').map((i) => i.incident_id),
    ['live'],
  );
});

// ── 상태·위험도 셀렉트 (v1.6)

test('상태 셀렉트에 `승인 대기`를 두지 않는다 — 프리셋이 담당한다', () => {
  const items = [item('a', 'AWAITING_APPROVAL'), item('b', 'ANALYZING')];
  assert.deepEqual(statusOptionsOf(items), ['ANALYZING']);
});

test('위험도 옵션은 목록에 실제로 있는 값만, 계약 상수 순서로 세운다', () => {
  const items = [
    item('low', 'ANALYZING', null, 'LOW'),
    item('high', 'ANALYZING', null, 'HIGH'),
  ];
  assert.deepEqual(riskOptionsOf(items), ['HIGH', 'LOW']);
});

test('FINOPS만 담긴 목록은 위험도 옵션이 비어 셀렉트가 사라진다', () => {
  const items = [item('a', 'AWAITING_APPROVAL', null, null)];
  assert.deepEqual(riskOptionsOf(items), []);
});

test('위험도 필터는 initial_risk_level로 건다 — 정렬 축과 같은 불변 키다', () => {
  const items = [
    item('high', 'ANALYZING', null, 'HIGH'),
    item('med', 'ANALYZING', null, 'MEDIUM'),
  ];
  assert.deepEqual(
    visibleIncidents(items, 'ACTIVE', ALL, 'MEDIUM').map((i) => i.incident_id),
    ['med'],
  );
});

test('목록에 없는 위험도 필터도 전체로 접힌다 — 상태 쪽과 같은 규칙이다', () => {
  const items = [item('a', 'ANALYZING', null, 'HIGH'), item('b', 'ANALYZING', null, 'HIGH')];
  assert.equal(visibleIncidents(items, 'ACTIVE', ALL, 'LOW').length, items.length);
});
