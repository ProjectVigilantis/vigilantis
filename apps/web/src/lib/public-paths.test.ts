// 공개 경로 분류 — proxy(로그인 이동 제외)와 AppShell(세션 조회 · 소켓 · GNB 폴링 제외)이 같이 쓴다. `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { isPublicPath } from './public-paths.ts';
import { safeReturnTo } from './return-to.ts';

test('로그인 · 랜딩 · 처리방침 · 약관만 공개다', () => {
  for (const path of ['/login', '/landing', '/privacy', '/terms']) assert.equal(isPublicPath(path), true, path);
});

test('정확히 일치할 때만 공개다 — 하위 · 비슷한 경로와 제거된 초대 링크 등록 경로는 보호된다', () => {
  for (const path of ['/', '/login/', '/login/x', '/landing/', '/privacy/', '/terms/x', '/LOGIN', '/team', '/team/enroll', '/users', '/tenants']) {
    assert.equal(isPublicPath(path), false, path);
  }
});

test('제거된 초대 링크 등록 경로는 더 이상 돌아갈 곳 예외가 아니다 — 보호 경로처럼 그대로 돌아간다', () => {
  assert.equal(safeReturnTo('/team/enroll'), '/team/enroll');
  assert.equal(safeReturnTo('/users?x=1'), '/users?x=1');
});
