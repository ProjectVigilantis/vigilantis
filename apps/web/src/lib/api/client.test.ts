// 네트워크 실패 회귀 — `npm test`. BE 미기동 시 조회 화면 전부가 멈추던 원인(React Flight dev 직렬화)을 고정합니다.

import assert from 'node:assert/strict';
import { createServer } from 'node:net';
import { test } from 'node:test';

import { getAssets } from './client.ts';

/** 방금 비운 포트 — 연결하면 실제 Node 연결 거부 오류(`cause.code = ECONNREFUSED`)가 난다. */
async function closedPort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address !== null && typeof address === 'object');
  await new Promise<void>((resolve) => server.close(() => resolve()));
  return address.port;
}

test('연결 거부는 원인 코드를 문구에 남기고, 앱 코드에서 새로 만든 오류로 올린다', async (t) => {
  const saved = process.env.NEXT_PUBLIC_API_BASE_URL;
  t.after(() => {
    if (saved === undefined) delete process.env.NEXT_PUBLIC_API_BASE_URL;
    else process.env.NEXT_PUBLIC_API_BASE_URL = saved;
  });
  process.env.NEXT_PUBLIC_API_BASE_URL = `http://127.0.0.1:${await closedPort()}`;

  await assert.rejects(getAssets(), (error: unknown) => {
    assert.ok(error instanceof Error);
    assert.equal(error.message, '요청이 실패했습니다 (네트워크 오류: ECONNREFUSED)');
    // cause가 붙으면 스택이 `node:` 내부 프레임뿐인 원래 오류가 서버 컴포넌트 prop 직렬화로 그대로 넘어간다.
    assert.equal('cause' in error, false);
    // 앱 코드 프레임이 남아야 dev 직렬화가 깨지지 않는다 — fetch 오류를 되던지지 않고 새로 만들었다는 증거.
    assert.match(String(error.stack), /client\.ts/);
    return true;
  });
});

test('원인 코드가 없는 실패(브라우저의 Failed to fetch)는 코드 없이 접는다', async (t) => {
  const realFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = realFetch;
  });
  globalThis.fetch = () => Promise.reject(new TypeError('Failed to fetch'));

  await assert.rejects(getAssets(), {
    message: '요청이 실패했습니다 (네트워크 오류)',
  });
});
