// 공개 경로 분류 — proxy(로그인 이동 제외)와 AppShell(세션 조회 · 소켓 · GNB 폴링 제외)이 같이 쓴다. `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { isPublicPath } from './public-paths.ts';
import { ENROLL_PATH, safeReturnTo } from './return-to.ts';

test('로그인 · 랜딩 · 초대 링크 등록만 공개다', () => {
  assert.equal(ENROLL_PATH, '/team/enroll');
  for (const path of ['/login', '/landing', '/team/enroll']) assert.equal(isPublicPath(path), true, path);
});

test('정확히 일치할 때만 공개다 — 상위 · 하위 · 비슷한 경로는 보호된다', () => {
  for (const path of ['/', '/team', '/team/', '/team/enroll/', '/team/enroll/x', '/team/enrollx', '/TEAM/ENROLL', '/users', '/tenants']) {
    assert.equal(isPublicPath(path), false, path);
  }
});

test('초대 링크 등록은 로그인 뒤 돌아갈 곳이 아니다', () => {
  assert.equal(safeReturnTo('/team/enroll'), '/');
  assert.equal(safeReturnTo('/team/enroll?x=1'), '/');
  assert.equal(safeReturnTo('/team/enroll#token=abc'), '/');
});
