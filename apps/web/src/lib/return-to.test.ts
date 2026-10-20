// 로그인 후 돌아갈 경로 검증 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { safeReturnTo } from './return-to.ts';

test('같은 사이트의 상대 경로는 query · hash까지 그대로 돌려준다', () => {
  assert.equal(safeReturnTo('/incidents/abc?x=1#y'), '/incidents/abc?x=1#y');
  assert.equal(safeReturnTo('/'), '/');
  assert.equal(safeReturnTo('/security?tab=incidents&preset=PENDING'), '/security?tab=incidents&preset=PENDING');
});

test('인코딩된 슬래시는 경로 글자일 뿐이라 같은 오리진에 머문다', () => {
  assert.equal(safeReturnTo('/%2F%2Fevil.com'), '/%2F%2Fevil.com');
});

test('다른 오리진 · 스킴 · 프로토콜 상대 · 역슬래시는 / 로 접는다', () => {
  for (const raw of [
    'https://evil.com',
    '//evil.com',
    '/\\evil.com',
    '\\\\evil.com',
    '/foo\\bar',
    'javascript:alert(1)',
    'JAVASCRIPT:alert(1)',
    'data:text/html,x',
    'evil.com',
    'incidents/abc',
  ]) {
    assert.equal(safeReturnTo(raw), '/', raw);
  }
});

test('점 세그먼트 제거가 만든 // 는 / 로 접는다 (열린 리다이렉트 회귀)', () => {
  for (const raw of [
    '/a/..//evil.example',
    '/%2e%2e//evil.example',
    '/%2E%2E//evil.example',
    '/.//evil.example',
    '/a/b/../..//evil.example',
    '/./ /evil',
    '/a/../../\\evil',
  ]) {
    assert.equal(safeReturnTo(raw), '/', raw);
  }
});

test('인코딩된 슬래시 · 점은 경로 글자로 남아 단일 / 경로다', () => {
  assert.equal(safeReturnTo('/%2F%2Fevil'), '/%2F%2Fevil');
  assert.equal(safeReturnTo('/a/..%2F/evil'), '/a/..%2F/evil');
  assert.equal(safeReturnTo('/a/./b/../c?x=1'), '/a/c?x=1');
});

test('제어 · 공백 문자가 섞이면 / 로 접는다', () => {
  for (const raw of ['/\t/evil.com', '/\n/evil.com', '/\r\n', '/ foo', '/\u0000x', '/\u007fx', '/\u00a0x']) {
    assert.equal(safeReturnTo(raw), '/', JSON.stringify(raw));
  }
});

test('로그인 화면 자신은 / 로 접는다', () => {
  for (const raw of ['/login', '/login?next=%2Fassets', '/login/', '/./login', '/x/../login']) {
    assert.equal(safeReturnTo(raw), '/', raw);
  }
});

test('모든 입력에서 결과는 / 이거나 // 로 시작하지 않는 같은 오리진 경로다', () => {
  const base = 'https://dashboard.example';
  const cases = [
    '/incidents/abc?x=1#y', '/', '/security?tab=incidents', '/%2F%2Fevil.com', '/%2F%2Fevil',
    'https://evil.com', '//evil.com', '/\\evil.com', '\\\\evil.com', '/foo\\bar', 'javascript:alert(1)',
    'data:text/html,x', 'evil.com', 'incidents/abc', '/login', '/login/', '/x/../login',
    '/a/..//evil.example', '/%2e%2e//evil.example', '/%2E%2E//evil.example', '/.//evil.example',
    '/a/b/../..//evil.example', '/./ /evil', '/a/../../\\evil', '/a/..%2F/evil', '/a/%2e%2e/%2e%2e//evil',
    '/..//evil', '/../evil', '/a/..', '/#//evil', '/?//evil', '/\t/evil.com', '', '///evil', '/%5Cevil',
  ];
  for (const raw of cases) {
    const out = safeReturnTo(raw);
    if (out === '/') continue;
    assert.ok(out.startsWith('/') && !out.startsWith('//') && !out.includes('\\'), `${raw} → ${out}`);
    assert.equal(new URL(out, base).origin, base, `${raw} → ${out}`);
    assert.equal(safeReturnTo(out), out, `재검사해도 같다: ${raw} → ${out}`);
  }
});

test('비어 있거나 값이 없으면 /', () => {
  assert.equal(safeReturnTo(''), '/');
  assert.equal(safeReturnTo(null), '/');
  assert.equal(safeReturnTo(undefined), '/');
});
