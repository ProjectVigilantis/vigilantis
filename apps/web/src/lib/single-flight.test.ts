// 동시 호출 공유 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { singleFlight } from './single-flight.ts';

/** 밖에서 끝낼 수 있는 요청 흉내. `calls`는 실제로 나간 요청 수다. */
function deferredRun<T>() {
  let calls = 0;
  let settle: { resolve: (value: T) => void; reject: (error: unknown) => void } | null = null;
  const run = () => {
    calls += 1;
    return new Promise<T>((resolve, reject) => {
      settle = { resolve, reject };
    });
  };
  return {
    run,
    calls: () => calls,
    resolve: (value: T) => settle!.resolve(value),
    reject: (error: unknown) => settle!.reject(error),
  };
}

test('진행 중에 다시 부르면 요청 하나를 나눠 받는다 — 클릭 한 번 = POST 한 번', async () => {
  const req = deferredRun<string>();
  const extend = singleFlight(req.run);
  const fromInput = extend();
  const fromButton = extend();
  assert.equal(fromInput, fromButton);
  assert.equal(req.calls(), 1);
  req.resolve('ok');
  assert.deepEqual(await Promise.all([fromInput, fromButton]), ['ok', 'ok']);
});

test('끝난 뒤의 호출은 새 요청을 보낸다', async () => {
  const req = deferredRun<number>();
  const extend = singleFlight(req.run);
  const first = extend();
  req.resolve(1);
  await first;
  const second = extend();
  assert.notEqual(second, first);
  assert.equal(req.calls(), 2);
  req.resolve(2);
  assert.equal(await second, 2);
});

test('실패는 기다리던 호출 모두에 전해지고, 다음 호출은 다시 시도한다', async () => {
  const req = deferredRun<void>();
  const extend = singleFlight(req.run);
  const failure = new Error('HTTP 500');
  const a = extend();
  const b = extend();
  req.reject(failure);
  await assert.rejects(a, (e) => e === failure);
  await assert.rejects(b, (e) => e === failure);
  const retry = extend();
  assert.equal(req.calls(), 2);
  req.resolve();
  await retry;
});
