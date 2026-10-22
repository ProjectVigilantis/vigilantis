// 사용자 관리 문구 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { VIEWER_ACTION_BLOCKED } from './roles.ts';
import {
  INVITE_RECOVER_NOT_FOUND,
  INVITE_UNCERTAIN,
  USER_ADMIN_FORBIDDEN,
  eventSummary,
  formatUserTime,
  userAdminErrorMessage,
  type UserAdminAction,
} from './user-admin.ts';
import type { UserEventItem } from '../types/api.ts';

const ACTIONS: UserAdminAction[] = ['invite', 'resend', 'role', 'status', 'revoke'];

test('403은 "관리자만" 문구다 — 조치 화면의 조회 전용 문구가 아니다', () => {
  for (const action of ACTIONS) {
    assert.equal(userAdminErrorMessage(action, 'AUTH_FORBIDDEN'), USER_ADMIN_FORBIDDEN, action);
  }
  assert.notEqual(USER_ADMIN_FORBIDDEN, VIEWER_ACTION_BLOCKED);
  assert.doesNotMatch(USER_ADMIN_FORBIDDEN, /조회 전용/);
});

test('초대 · 재발송의 500 · 네트워크 실패는 메일이 나갔을 수 있다고 말한다', () => {
  for (const action of ['invite', 'resend'] as const) {
    for (const code of ['INTERNAL_ERROR', null] as const) {
      const message = userAdminErrorMessage(action, code);
      assert.equal(message, INVITE_UNCERTAIN);
      assert.match(message, /처리가 완료되지 않았습니다/);
      assert.match(message, /초대 메일이 발송되었을 수 있습니다/);
    }
  }
});

test('그 밖의 조치의 결과 불명 실패는 메일을 말하지 않고 재조회를 안내한다', () => {
  for (const action of ['role', 'status', 'revoke'] as const) {
    const message = userAdminErrorMessage(action, 'INTERNAL_ERROR');
    assert.doesNotMatch(message, /메일/);
    assert.match(message, /새로고침/);
  }
});

test('대상 없음 · 상태 충돌', () => {
  assert.match(userAdminErrorMessage('role', 'USER_NOT_FOUND'), /찾을 수 없습니다/);
  // 초대의 404는 복구 연결 대상(기존 인증 계정)이 없다는 뜻이다.
  assert.equal(userAdminErrorMessage('invite', 'USER_NOT_FOUND'), INVITE_RECOVER_NOT_FOUND);
  assert.match(userAdminErrorMessage('status', 'USER_STATE_CONFLICT'), /마지막 활성 관리자/);
  assert.match(userAdminErrorMessage('invite', 'USER_STATE_CONFLICT'), /이미 초대된 이메일/);
});

test('사용자 관리 시각은 KST 24시간제 고정 문자열이다 — 오전/AM 표기가 없다', () => {
  // 01:39:51Z = KST 10:39:51(Lead 브라우저 재현 값). 오후 · 날짜 넘김 · 연 넘김 · 초 이하 버림.
  assert.equal(formatUserTime('2026-10-21T01:39:51Z'), '2026-10-21 10:39:51 KST');
  assert.equal(formatUserTime('2026-10-21T05:00:00Z'), '2026-10-21 14:00:00 KST');
  assert.equal(formatUserTime('2026-10-21T15:00:00Z'), '2026-10-22 00:00:00 KST');
  assert.equal(formatUserTime('2026-12-31T16:05:09.999Z'), '2027-01-01 01:05:09 KST');
  assert.equal(formatUserTime('2026-10-21T10:39:51+09:00'), '2026-10-21 10:39:51 KST');
  assert.doesNotMatch(formatUserTime('2026-10-21T05:00:00Z'), /AM|PM|오전|오후/);
});

const event = (overrides: Partial<UserEventItem>): UserEventItem => ({
  event_id: 'e1',
  actor_user_id: 'a1',
  target_user_id: 't1',
  action: 'INVITE_RESENT',
  before_state: {},
  after_state: {},
  occurred_at: '2026-10-20T00:00:00Z',
  ...overrides,
});

test('이력 한 줄 요약', () => {
  assert.equal(
    eventSummary(event({ action: 'ROLE_CHANGED', before_state: { role: 'VIEWER' }, after_state: { role: 'ADMIN' } })),
    '조회 전용 → 관리자',
  );
  assert.equal(
    eventSummary(
      event({ action: 'STATUS_CHANGED', before_state: { status: 'ACTIVE' }, after_state: { status: 'DISABLED' } }),
    ),
    '활성 → 비활성',
  );
  assert.equal(eventSummary(event({ action: 'SESSIONS_REVOKED', after_state: { revoked_session_count: 2 } })), '2개 종료');
  assert.equal(eventSummary(event({ action: 'SESSIONS_REVOKED', after_state: {} })), '');
  assert.equal(eventSummary(event({ action: 'USER_INVITED', after_state: { role: 'OPERATOR' } })), '운영자');
  assert.equal(
    eventSummary(event({ action: 'USER_INVITED', after_state: { role: 'OPERATOR', recover_existing: true } })),
    '운영자 · 기존 계정 연결',
  );
  assert.equal(eventSummary(event({ action: 'INVITE_RESENT' })), '');
  assert.equal(eventSummary(event({ action: 'ROLE_CHANGED', before_state: { role: null }, after_state: {} })), '— → —');
});
