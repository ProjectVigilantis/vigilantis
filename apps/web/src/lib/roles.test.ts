// 역할별 조치 가능 여부 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { VIEWER_ACTION_BLOCKED, canOperate, operateBlockedReason } from './roles.ts';

test('ADMIN · OPERATOR만 조치할 수 있다', () => {
  assert.equal(canOperate('ADMIN'), true);
  assert.equal(canOperate('OPERATOR'), true);
  assert.equal(canOperate('VIEWER'), false);
});

test('역할을 아직 모르면 조치할 수 없다', () => {
  assert.equal(canOperate(null), false);
  assert.equal(canOperate(undefined), false);
});

test('막는 이유 — 조회 전용과 역할 미확인을 다른 문구로 말한다', () => {
  assert.equal(operateBlockedReason('ADMIN'), null);
  assert.equal(operateBlockedReason('VIEWER'), VIEWER_ACTION_BLOCKED);
  assert.equal(VIEWER_ACTION_BLOCKED, '조회 전용 계정은 조치할 수 없습니다.');
  const pending = operateBlockedReason(null);
  assert.ok(pending !== null && pending !== VIEWER_ACTION_BLOCKED);
});
