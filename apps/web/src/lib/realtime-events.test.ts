// WS 이벤트 매핑 회귀 — `npm test`. 소켓 없이 검증되는 부분을 전부 여기서 잡습니다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  SeenEvents,
  actionFor,
  agentWaitTimes,
  appendTransition,
  backoffMs,
  latchAgentWaitAt,
  websocketUrl,
} from './realtime-events.ts';
import type { WsEvent } from '../types/api.ts';

const incidentEvent = (type: 'INCIDENT_CREATED' | 'INCIDENT_UPDATED'): WsEvent =>
  ({
    event_id: 'e1',
    event_type: type,
    occurred_at: '2026-08-26T00:00:00Z',
    data: { incident_id: 'inc-1' },
  }) as WsEvent;

const executionEvent = (status: string): WsEvent =>
  ({
    event_id: 'e2',
    event_type: 'EXECUTION_UPDATED',
    occurred_at: '2026-08-26T00:00:00Z',
    data: {
      incident_id: 'inc-1',
      execution_id: 'exe-1',
      status,
      updated_at: '2026-08-26T00:00:01Z',
    },
  }) as WsEvent;

test('INCIDENT_CREATED는 Toast를 띄우고 재조회시킨다', () => {
  assert.deepEqual(actionFor(incidentEvent('INCIDENT_CREATED')), {
    kind: 'refresh',
    incidentId: 'inc-1',
    occurredAt: '2026-08-26T00:00:00Z',
    toast: true,
  });
});

test('INCIDENT_UPDATED는 Toast 없이 재조회만 — 상태 변화마다 띄우면 소음이 된다', () => {
  assert.equal(actionFor(incidentEvent('INCIDENT_UPDATED')).toast, false);
});

test('EXECUTION_UPDATED Toast는 최종 상태에서만', () => {
  for (const s of ['SUCCESS', 'FAILED', 'ROLLED_BACK', 'ROLLBACK_FAILED']) {
    assert.equal(actionFor(executionEvent(s)).toast, true, `${s}는 최종이다`);
  }
  for (const s of ['IN_PROGRESS', 'ROLLBACK_INITIATED']) {
    assert.equal(actionFor(executionEvent(s)).toast, false, `${s}는 비최종이다`);
  }
});

test('EXECUTION_UPDATED는 status를 data에서 그대로 옮긴다', () => {
  const action = actionFor(executionEvent('IN_PROGRESS'));
  assert.deepEqual(action, {
    kind: 'execution',
    incidentId: 'inc-1',
    executionId: 'exe-1',
    status: 'IN_PROGRESS',
    updatedAt: '2026-08-26T00:00:01Z',
    toast: false,
  });
});

test('인시던트 계열은 봉투의 occurred_at을 싣는다 — B-Medium 대기 기준 시각이다', () => {
  // 여기서 버리면 상세가 기준 시각을 못 받아 항상 fallback 안내문으로 떨어진다(PR #181 리뷰).
  assert.deepEqual(actionFor(incidentEvent('INCIDENT_UPDATED')), {
    kind: 'refresh',
    incidentId: 'inc-1',
    occurredAt: '2026-08-26T00:00:00Z',
    toast: false,
  });
});

// ── 대기 기준 시각 래치 — 리셋되면 서버 자동 격리보다 시간을 더 남았다고 말한다 ──────

test('AGENT_WAIT 창에 들어가면 마지막 이벤트 시각을 기준으로 잡는다', () => {
  assert.equal(latchAgentWaitAt(null, true, '2026-08-26T00:00:00Z'), '2026-08-26T00:00:00Z');
});

test('창 안에서 후속 이벤트가 와도 기준 시각은 바뀌지 않는다', () => {
  // 정밀 평가 도착 등으로 INCIDENT_UPDATED가 한 번 더 오는 경로다 — 여기서 갈아치우면 60초가 리셋된다.
  assert.equal(
    latchAgentWaitAt('2026-08-26T00:00:00Z', true, '2026-08-26T00:00:40Z'),
    '2026-08-26T00:00:00Z',
  );
});

test('창을 벗어나면 비워 다음 진입이 새로 잡게 한다', () => {
  assert.equal(latchAgentWaitAt('2026-08-26T00:00:00Z', false, '2026-08-26T00:00:40Z'), null);
});

test('이벤트를 못 본 진입은 null로 남는다 — 고정 안내문 fallback', () => {
  assert.equal(latchAgentWaitAt(null, true, null), null);
});

// ── 대기 화면의 두 시각 — 파싱 불가 입력이 화면을 죽이던 회귀를 여기서 막는다(PR #187 리뷰) ──

test('응답 기한은 대기 기준 시각 + 정확히 60초다', () => {
  const times = agentWaitTimes('2026-08-26T00:00:00.000Z');
  assert.deepEqual(times, {
    startedAt: '2026-08-26T00:00:00.000Z',
    deadlineAt: '2026-08-26T00:01:00.000Z',
  });
});

test('파싱 불가한 기준 시각은 null이다 — 화면은 고정 안내문으로 떨어진다', () => {
  // `new Date(NaN).toISOString()`은 RangeError를 던지고, route-level error.tsx가 없어
  // 앱 셸 전체가 대체된다. WS 프레임은 런타임 검증이 없어 이 값들이 실제로 흘러들 수 있다.
  for (const bad of [null, undefined, '', 'not-a-date']) {
    assert.equal(agentWaitTimes(bad), null, `${JSON.stringify(bad)}는 null이어야 한다`);
  }
});

test('같은 event_id 재수신은 한 번만 반영한다', () => {
  const seen = new SeenEvents();
  assert.equal(seen.accept('a'), true);
  assert.equal(seen.accept('a'), false);
  assert.equal(seen.accept('b'), true);
});

test('기억하는 id 수에 상한이 있다', () => {
  const seen = new SeenEvents(2);
  seen.accept('a');
  seen.accept('b');
  seen.accept('c'); // a가 밀려난다
  assert.equal(seen.accept('a'), true, '밀려난 id는 다시 새 것으로 본다');
  assert.equal(seen.accept('c'), false, '아직 기억하는 id는 막는다');
});

test('백오프는 1s에서 시작해 30s에서 멈춘다', () => {
  assert.deepEqual([0, 1, 2, 3].map(backoffMs), [1000, 2000, 4000, 8000]);
  assert.equal(backoffMs(10), 30_000);
});

test('mock 단계(base URL 미설정)에서는 연결하지 않는다', () => {
  assert.equal(websocketUrl(undefined), null);
  assert.equal(websocketUrl(''), null);
});

test('http·https를 ws·wss로 바꾸고 계약 경로를 붙인다', () => {
  assert.equal(websocketUrl('http://localhost:8000'), 'ws://localhost:8000/api/v1/ws');
  assert.equal(websocketUrl('https://api.example.com/'), 'wss://api.example.com/api/v1/ws');
});

// ── 진행 기록 누적 — PR #181 리뷰가 "배달 쪽이 빠졌다"고 짚은 경계다 ──────────────

const 접수 = { at: '2026-08-26T00:00:00Z', from: null, to: 'IN_PROGRESS' } as const;

test('접수 행 다음에 다른 status가 오면 from이 접수 상태로 이어진다', () => {
  const out = appendTransition([접수], { at: '2026-08-26T00:00:05Z', status: 'SUCCESS' });
  assert.equal(out.length, 2);
  assert.deepEqual(out[1], { at: '2026-08-26T00:00:05Z', from: 'IN_PROGRESS', to: 'SUCCESS' });
});

test('첫 이벤트가 접수와 같은 status면 줄을 늘리지 않는다', () => {
  // 접수 행이 prev에 없으면 from이 null이라 이 차단이 무력해진다 — 그게 원래 버그였다.
  const out = appendTransition([접수], { at: '2026-08-26T00:00:05Z', status: 'IN_PROGRESS' });
  assert.deepEqual(out, [접수]);
});

test('접수 행은 덮이지 않고 남는다', () => {
  let rows = appendTransition([접수], { at: '2026-08-26T00:00:05Z', status: 'ROLLBACK_INITIATED' });
  rows = appendTransition(rows, { at: '2026-08-26T00:00:09Z', status: 'ROLLED_BACK' });
  assert.deepEqual(rows[0], 접수);
  assert.deepEqual(
    rows.map((r) => r.to),
    ['IN_PROGRESS', 'ROLLBACK_INITIATED', 'ROLLED_BACK'],
  );
});

test('websocketUrl은 스킴 없는 값을 거른다', () => {
  assert.equal(websocketUrl('localhost:8000'), null);
});
