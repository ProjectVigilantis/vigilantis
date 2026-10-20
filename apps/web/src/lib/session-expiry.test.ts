// 세션 만료 안내 시각 계산 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { expirySchedule } from './session-expiry.ts';

const MIN = 60_000;
const T0 = Date.parse('2026-10-20T00:00:00Z');
const iso = (ms: number) => new Date(ms).toISOString();
/** 유휴 만료 = T0 + idleMin분, 절대 만료 = T0 + absMin분. */
const session = (idleMin: number, absMin: number) => ({
  idle_expires_at: iso(T0 + idleMin * MIN),
  absolute_expires_at: iso(T0 + absMin * MIN),
});

test('유휴가 먼저면 5분 전까지는 아무것도 띄우지 않고, 5분 전이 다음 변경 시각이다', () => {
  const s = expirySchedule(session(30, 480), T0);
  assert.deepEqual(s, {
    expired: false,
    idleMinutesLeft: null,
    absoluteNotice: false,
    nextChangeAt: T0 + 25 * MIN,
  });
});

test('유휴 5분 전부터 남은 분(올림)과 연장을 띄운다', () => {
  const at5 = expirySchedule(session(30, 480), T0 + 25 * MIN);
  assert.equal(at5.idleMinutesLeft, 5);
  assert.equal(at5.absoluteNotice, false);
  assert.equal(at5.nextChangeAt, T0 + 26 * MIN, '분 표시가 4로 줄어드는 순간');

  const at4m30 = expirySchedule(session(30, 480), T0 + 25 * MIN + 30_000);
  assert.equal(at4m30.idleMinutesLeft, 5, '4분 30초는 5분으로 올림');
  assert.equal(at4m30.nextChangeAt, T0 + 26 * MIN);

  const last = expirySchedule(session(30, 480), T0 + 29 * MIN + 30_000);
  assert.equal(last.idleMinutesLeft, 1);
  assert.equal(last.nextChangeAt, T0 + 30 * MIN, '마지막 1분의 다음 변경은 만료');
});

test('절대가 먼저거나 같으면 연장을 띄우지 않는다', () => {
  // 유휴 5분 전 · 절대 10분 전 창 안의 같은 시각(만료 2분 전).
  for (const s of [session(480, 470), session(470, 470)]) {
    const near = expirySchedule(s, T0 + 468 * MIN);
    assert.equal(near.idleMinutesLeft, null);
    assert.equal(near.absoluteNotice, true);
  }
});

test('절대 10분 전부터 최대 사용 시간 안내를 띄운다', () => {
  const before = expirySchedule(session(470, 480), T0 + 469 * MIN);
  assert.equal(before.absoluteNotice, false);
  assert.equal(before.nextChangeAt, T0 + 470 * MIN, '유휴 만료와 절대 10분 전 중 이른 쪽');

  const s = expirySchedule(session(500, 480), T0 + 470 * MIN);
  assert.equal(s.absoluteNotice, true);
  assert.equal(s.idleMinutesLeft, null);
  assert.equal(s.nextChangeAt, T0 + 480 * MIN, '안내 중 다음 변경은 만료');
});

test('유휴가 먼저인데 절대도 10분 안이면 둘 다 띄운다', () => {
  const s = expirySchedule(session(476, 480), T0 + 472 * MIN);
  assert.equal(s.idleMinutesLeft, 4);
  assert.equal(s.absoluteNotice, true);
});

test('이른 쪽 만료에 닿으면 expired', () => {
  assert.deepEqual(expirySchedule(session(30, 480), T0 + 30 * MIN), {
    expired: true,
    idleMinutesLeft: null,
    absoluteNotice: false,
    nextChangeAt: null,
  });
  assert.equal(expirySchedule(session(500, 480), T0 + 481 * MIN).expired, true);
});

test('해석할 수 없는 시각이면 아무것도 띄우지 않는다', () => {
  const s = expirySchedule({ idle_expires_at: 'x', absolute_expires_at: iso(T0) }, T0);
  assert.deepEqual(s, { expired: false, idleMinutesLeft: null, absoluteNotice: false, nextChangeAt: null });
});
