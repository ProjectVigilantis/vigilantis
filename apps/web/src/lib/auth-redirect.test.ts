// 로그인 주소 조립 · 서버가 API로 싣는 쿠키 헤더 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { SESSION_COOKIE, loginUrl, sessionCookieHeader } from './auth-redirect.ts';

/** `cookies()` 저장소의 `get`만 흉내 낸다. */
function store(entries: Record<string, string>) {
  return {
    get: (name: string) => (name in entries ? { name, value: entries[name] } : undefined),
  };
}

test('401 로그인 주소는 지금 경로를 next로 인코딩해 싣는다', () => {
  assert.equal(loginUrl('/incidents/abc?x=1'), '/login?next=%2Fincidents%2Fabc%3Fx%3D1');
  assert.equal(loginUrl('/security?tab=incidents'), '/login?next=%2Fsecurity%3Ftab%3Dincidents');
});

test('돌아갈 곳이 / 이거나 안전하지 않으면 next 없이 /login', () => {
  for (const raw of [
    '/', '', null, undefined, '//evil.com', 'https://evil.com', '/login?next=%2Fassets',
    '/a/..//evil.example', '/%2e%2e//evil.example',
  ]) {
    assert.equal(loginUrl(raw), '/login', String(raw));
  }
});

test('세션 종료 표시는 이유 없이 ended=1 하나만 붙인다', () => {
  assert.equal(loginUrl('/assets?x=1', true), '/login?next=%2Fassets%3Fx%3D1&ended=1');
  assert.equal(loginUrl('/', true), '/login?ended=1');
  assert.equal(loginUrl('//evil.com', true), '/login?ended=1');
  // 기본값은 표시 없음 — 세션 쿠키 없이 처음 들어온 경우(proxy).
  assert.equal(loginUrl('/assets'), '/login?next=%2Fassets');
});

test('세션 쿠키 하나만 Cookie 헤더로 싣는다 — 다른 쿠키가 있어도', () => {
  const cookies = store({
    vg_login_flow: 'flow-secret',
    _ga: 'GA1.1.123',
    [SESSION_COOKIE]: 'sess-value',
    other: 'x',
  });
  assert.equal(sessionCookieHeader(cookies), '__Host-vg_session=sess-value');
});

test('세션 쿠키가 없거나 비어 있으면 null — 다른 쿠키를 대신 싣지 않는다', () => {
  assert.equal(sessionCookieHeader(store({ vg_login_flow: 'flow-secret' })), null);
  assert.equal(sessionCookieHeader(store({ [SESSION_COOKIE]: '' })), null);
});
