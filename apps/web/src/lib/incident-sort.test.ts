// byRisk 회귀 검사 — `node --test src/lib/incident-sort.test.ts` (의존성 없음, Node 타입 스트리핑).
// 정렬 키가 불변(initial_risk_level)이라는 것과 동점 규칙이 이 파일의 검사 대상입니다(§4.4).

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { sortByRisk } from './incident-sort.ts';
import type { IncidentListItem, RiskLevel } from '../types/api.ts';

function item(
  id: string,
  initial: RiskLevel | null,
  createdAt: string,
  reviewed: RiskLevel | null = null,
): IncidentListItem {
  return {
    incident_id: id,
    title: null,
    subject_arn: `arn:aws:ec2:ap-northeast-2:1:instance/${id}`,
    category: initial === null ? 'FINOPS' : 'SECOPS',
    status: 'AWAITING_APPROVAL',
    initial_risk_level: initial,
    reviewed_risk_level: reviewed,
    response_mode: null,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

const ids = (items: IncidentListItem[]) => items.map((i) => i.incident_id);

test('HIGH → MEDIUM → LOW → null(FinOps) 순서', () => {
  const out = sortByRisk([
    item('fin', null, '2026-08-01T00:00:00Z'),
    item('low', 'LOW', '2026-08-01T00:00:00Z'),
    item('high', 'HIGH', '2026-08-01T00:00:00Z'),
    item('mid', 'MEDIUM', '2026-08-01T00:00:00Z'),
  ]);
  assert.deepEqual(ids(out), ['high', 'mid', 'low', 'fin']);
});

test('동점은 created_at 오름차순 — 오래 기다린 건이 먼저', () => {
  const out = sortByRisk([
    item('new', 'HIGH', '2026-08-03T00:00:00Z'),
    item('old', 'HIGH', '2026-08-01T00:00:00Z'),
  ]);
  assert.deepEqual(ids(out), ['old', 'new']);
});

test('reviewed_risk_level이 바뀌어도 순서가 움직이지 않는다 (불변 키)', () => {
  const before = sortByRisk([
    item('a', 'HIGH', '2026-08-01T00:00:00Z', null),
    item('b', 'MEDIUM', '2026-08-02T00:00:00Z', null),
  ]);
  // 정밀평가가 a를 LOW로, b를 HIGH로 뒤집어도 정렬은 initial만 본다.
  const after = sortByRisk([
    item('a', 'HIGH', '2026-08-01T00:00:00Z', 'LOW'),
    item('b', 'MEDIUM', '2026-08-02T00:00:00Z', 'HIGH'),
  ]);
  assert.deepEqual(ids(after), ids(before));
  assert.deepEqual(ids(after), ['a', 'b']);
});

test('입력 배열을 변형하지 않는다', () => {
  const input = [item('mid', 'MEDIUM', '2026-08-01T00:00:00Z'), item('high', 'HIGH', '2026-08-01T00:00:00Z')];
  sortByRisk(input);
  assert.deepEqual(ids(input), ['mid', 'high']);
});
