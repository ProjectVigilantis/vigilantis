// WS 이벤트 매핑 회귀 — `npm test`. 소켓 없이 검증되는 부분을 전부 여기서 잡습니다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { SeenEvents, actionFor, backoffMs, websocketUrl } from './realtime-events.ts';
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
