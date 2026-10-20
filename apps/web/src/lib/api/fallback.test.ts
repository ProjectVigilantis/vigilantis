// 서버 조회 fallback이 401 redirect를 삼키지 않는지 — `npm test`.
// Next의 실제 `redirect` · `unstable_rethrow`를 쓴다(`node --test`는 확장자 없는 `next/navigation`을 못 푼다).

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { redirect, unstable_rethrow } from 'next/navigation.js';

import { fallbacks } from './fallback.ts';

const { orNull, orError } = fallbacks(unstable_rethrow);

/** `redirect()`가 던지는 NEXT_REDIRECT 오류 그 자체. */
function redirectError(): unknown {
  try {
    redirect('/login?next=%2Fassets');
  } catch (error) {
    return error;
  }
  throw new Error('redirect는 던져야 한다');
}

test('성공한 값은 그대로 지난다', async () => {
  assert.equal(await orNull(Promise.resolve(3)), 3);
  assert.deepEqual(await orError(Promise.resolve('x')), { value: 'x', error: null });
});

test('일반 실패는 null · 오류 값으로 접는다', async () => {
  const failure = new Error('HTTP 500');
  assert.equal(await orNull(Promise.reject(failure)), null);
  assert.deepEqual(await orError(Promise.reject(failure)), { value: null, error: failure });
});

test('redirect(401 → 로그인)는 접지 않고 같은 오류로 다시 던진다', async () => {
  const error = redirectError();
  await assert.rejects(orNull(Promise.reject(error)), (thrown) => thrown === error);
  await assert.rejects(orError(Promise.reject(error)), (thrown) => thrown === error);
});

test('Promise.all 안의 redirect도 전체를 던지게 한다', async () => {
  const error = redirectError();
  await assert.rejects(
    Promise.all([orNull(Promise.resolve(1)), orNull(Promise.reject(error))]),
    (thrown) => thrown === error,
  );
});

test('기다리지 않은 redirect 거부가 unhandled rejection으로 새지 않는다', async () => {
  const leaked: unknown[] = [];
  const onLeak = (reason: unknown) => leaked.push(reason);
  process.on('unhandledRejection', onLeak);
  try {
    void orNull(Promise.reject(redirectError()));
    void orError(Promise.reject(redirectError()));
    await new Promise((resolve) => setTimeout(resolve, 20));
  } finally {
    process.off('unhandledRejection', onLeak);
  }
  assert.deepEqual(leaked, []);
});
