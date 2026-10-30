// 사용자 관리 문구 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { VIEWER_ACTION_BLOCKED, type PlatformSubject } from './roles.ts';
import {
  EVENT_LABELS,
  canChangeRow,
  INVITE_RECOVER_NOT_FOUND,
  INVITE_STATE_CONFLICT,
  INVITE_STATE_CONFLICT_PLATFORM,
  INVITE_TENANT_NOT_FOUND,
  INVITE_UNCERTAIN,
  TENANT_ID_INVALID,
  TENANT_ID_REQUIRED,
  TENANT_NOT_ACTIVE,
  TENANT_NOT_ASSIGNED,
  TENANT_SUSPENDED_BLOCKED,
  USER_ADMIN_FORBIDDEN,
  eventSummary,
  formatUserTime,
  inviteRequestFor,
  userAdminErrorMessage,
  type InviteForm,
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
  // 플랫폼 계정 전체가 아니라 등급 · 배정을 말한다 — 배정 없는 담당자가 "플랫폼 운영자라 되는데"로 읽지 않게.
  assert.doesNotMatch(USER_ADMIN_FORBIDDEN, /플랫폼 운영자/);
  assert.match(USER_ADMIN_FORBIDDEN, /중앙 관리자/);
  assert.match(USER_ADMIN_FORBIDDEN, /담당 고객이 배정된 고객 담당자/);
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

test('고객 상태 거절은 고객 사유를 말하고 결과 불명 문구로 접지 않는다', () => {
  assert.equal(userAdminErrorMessage('invite', 'TENANT_NOT_FOUND'), INVITE_TENANT_NOT_FOUND);
  for (const action of ['invite', 'resend'] as const) {
    assert.equal(userAdminErrorMessage(action, 'TENANT_STATE_CONFLICT'), TENANT_NOT_ACTIVE, action);
  }
  for (const action of ACTIONS) {
    assert.equal(userAdminErrorMessage(action, 'TENANT_SUSPENDED'), TENANT_SUSPENDED_BLOCKED, action);
    for (const code of ['TENANT_NOT_FOUND', 'TENANT_STATE_CONFLICT', 'TENANT_SUSPENDED'] as const) {
      assert.doesNotMatch(userAdminErrorMessage(action, code), /발송되었을 수 있습니다/, `${action} ${code}`);
    }
  }
});

test('범위 밖 사용자는 대상 없음과 같은 문구다', () => {
  for (const action of ['resend', 'role', 'status', 'revoke'] as const) {
    assert.match(userAdminErrorMessage(action, 'USER_NOT_FOUND'), /찾을 수 없습니다/, action);
  }
});

const TENANT_UUID = '123e4567-e89b-12d3-a456-426614174000';
const OTHER_TENANT_UUID = '223e4567-e89b-12d3-a456-426614174000';

// 플랫폼 계정은 호환 역할 ADMIN과 무관하게 platform_role과 담당 배정으로만 권한이 갈린다.
// 중앙 관리자의 배정 목록은 계약상 빈 목록이고, 고객 담당자는 배정을 명시한다(모든 고객을 배정하지 않는다).
const CENTRAL: PlatformSubject = { account_type: 'PLATFORM', platform_role: 'CENTRAL_ADMIN', assigned_tenant_ids: [] };
const MANAGER: PlatformSubject = {
  account_type: 'PLATFORM',
  platform_role: 'TENANT_MANAGER',
  assigned_tenant_ids: [TENANT_UUID],
};
const UNASSIGNED: PlatformSubject = { account_type: 'PLATFORM', platform_role: 'TENANT_MANAGER', assigned_tenant_ids: [] };
const TENANT: PlatformSubject = { account_type: 'TENANT', platform_role: null, assigned_tenant_ids: [] };
const form = (over: Partial<InviteForm> = {}): InviteForm => ({
  email: ' a@b.c ',
  role: 'OPERATOR',
  recoverExisting: false,
  tenantId: '',
  ...over,
});

test('초대 본문 — 플랫폼은 고객 ID를 반드시 싣는다', () => {
  const platform = CENTRAL;
  assert.deepEqual(inviteRequestFor(platform, form({ tenantId: ` ${TENANT_UUID.toUpperCase()} ` })), {
    ok: true,
    // 이메일은 다듬지 않는다. 고객 ID는 앞뒤 공백만 지우고 대소문자는 서버에 맡긴다.
    request: { email: ' a@b.c ', role: 'OPERATOR', recover_existing: false, tenant_id: TENANT_UUID.toUpperCase() },
  });
  assert.deepEqual(inviteRequestFor(platform, form({ tenantId: '  ' })), { ok: false, error: TENANT_ID_REQUIRED });
  for (const bad of ['tenant-a', TENANT_UUID.replaceAll('-', ''), `${TENANT_UUID}0`, `{${TENANT_UUID}}`]) {
    assert.deepEqual(inviteRequestFor(platform, form({ tenantId: bad })), { ok: false, error: TENANT_ID_INVALID }, bad);
  }
});

test('초대 본문 — 고객 범위가 있는 플랫폼 계정의 기존 계정 연결 선택은 그대로 싣는다', () => {
  for (const inviter of [CENTRAL, MANAGER]) {
    const result = inviteRequestFor(inviter, form({ tenantId: TENANT_UUID, recoverExisting: true }));
    assert.deepEqual(
      result,
      { ok: true, request: { email: ' a@b.c ', role: 'OPERATOR', recover_existing: true, tenant_id: TENANT_UUID } },
      String(inviter.platform_role),
    );
  }
});

test('초대 본문 — 고객 담당자는 담당 고객에만 보낸다(배정 밖 · 배정 없음은 보내지 않는다)', () => {
  // 중앙 관리자는 배정 목록과 무관하게 어떤 고객이든 보낸다.
  assert.equal(inviteRequestFor(CENTRAL, form({ tenantId: OTHER_TENANT_UUID })).ok, true);
  // UUID 표기의 대소문자는 같은 고객이다 — 입력은 다듬지 않고 그대로 싣는다.
  assert.deepEqual(inviteRequestFor(MANAGER, form({ tenantId: TENANT_UUID.toUpperCase() })), {
    ok: true,
    request: { email: ' a@b.c ', role: 'OPERATOR', recover_existing: false, tenant_id: TENANT_UUID.toUpperCase() },
  });
  for (const recoverExisting of [false, true]) {
    assert.deepEqual(
      inviteRequestFor(MANAGER, form({ tenantId: OTHER_TENANT_UUID, recoverExisting })),
      { ok: false, error: TENANT_NOT_ASSIGNED },
    );
    assert.deepEqual(
      inviteRequestFor(UNASSIGNED, form({ tenantId: TENANT_UUID, recoverExisting })),
      { ok: false, error: TENANT_NOT_ASSIGNED },
    );
  }
});

test('초대 본문 — 고객사 ADMIN은 일반 초대만 보낸다(tenant_id 없음 · 복구 선택이 와도 false)', () => {
  for (const recoverExisting of [false, true]) {
    const result = inviteRequestFor(TENANT, form({ tenantId: TENANT_UUID, recoverExisting }));
    assert.deepEqual(result, { ok: true, request: { email: ' a@b.c ', role: 'OPERATOR', recover_existing: false } });
  }
});

test('대상 없음 · 상태 충돌', () => {
  assert.match(userAdminErrorMessage('role', 'USER_NOT_FOUND'), /찾을 수 없습니다/);
  assert.match(userAdminErrorMessage('status', 'USER_STATE_CONFLICT'), /마지막 활성 관리자/);
  for (const action of ['resend', 'role', 'status', 'revoke'] as const) {
    for (const actor of [CENTRAL, MANAGER, UNASSIGNED, TENANT, null]) {
      assert.doesNotMatch(userAdminErrorMessage(action, 'USER_STATE_CONFLICT', actor), /복구/, action);
    }
  }
});

test('기존 계정 연결(복구) 안내는 고객 범위가 있는 플랫폼 계정의 초대에만 붙는다', () => {
  // 중앙 관리자 · 배정된 고객 담당자: 초대 404는 복구 연결 대상(기존 인증 계정)이 없다는 뜻이고, 409는 복구로 다시
  // 시도할 수 있다고 안내한다.
  for (const actor of [CENTRAL, MANAGER]) {
    const label = String(actor.platform_role);
    assert.equal(userAdminErrorMessage('invite', 'USER_NOT_FOUND', actor), INVITE_RECOVER_NOT_FOUND, label);
    assert.equal(userAdminErrorMessage('invite', 'USER_STATE_CONFLICT', actor), INVITE_STATE_CONFLICT_PLATFORM, label);
  }
  assert.match(INVITE_STATE_CONFLICT_PLATFORM, /이미 초대된 이메일/);
  assert.match(INVITE_STATE_CONFLICT_PLATFORM, /기존 인증 계정을 연결 \(복구\)/);

  // 배정 없는 고객 담당자 · 고객사 ADMIN(또는 계정 미상): 같은 거절 사유만 말하고 복구를 권하지 않는다.
  for (const actor of [UNASSIGNED, TENANT, null]) {
    const label = actor === null ? 'null' : `${actor.account_type}:${actor.platform_role}`;
    const conflict = userAdminErrorMessage('invite', 'USER_STATE_CONFLICT', actor);
    assert.equal(conflict, INVITE_STATE_CONFLICT, label);
    assert.match(conflict, /이미 초대된 이메일/);
    const notFound = userAdminErrorMessage('invite', 'USER_NOT_FOUND', actor);
    assert.notEqual(notFound, INVITE_RECOVER_NOT_FOUND, label);
    for (const message of [conflict, notFound]) assert.doesNotMatch(message, /복구|기존 (인증 )?계정/, label);
  }
  // 복구를 보내 서버가 막은 경우(403)도 일반 관리자 거절 문구다.
  for (const actor of [UNASSIGNED, TENANT]) {
    assert.equal(userAdminErrorMessage('invite', 'AUTH_FORBIDDEN', actor), USER_ADMIN_FORBIDDEN);
  }
});

test('역할 · 상태 변경은 세션 확인 후 다른 사용자 행에서만 가능하다', () => {
  assert.equal(canChangeRow('u2', 'u1'), true);
  assert.equal(canChangeRow('u1', 'u1'), false);
  assert.equal(canChangeRow('u1', null), false);
  assert.equal(canChangeRow('u2', null), false);
});

test('사용자 관리 시각은 KST 24시간제 고정 문자열이다 — 오전/AM 표기가 없다', () => {
  // 01:39:51Z = KST 10:39:51. 오후 · 날짜 넘김 · 연 넘김 · 초 이하 버림.
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
  assert.equal(
    eventSummary(
      event({
        action: 'PLATFORM_ROLE_CHANGED',
        before_state: { platform_role: 'TENANT_MANAGER' },
        after_state: { platform_role: 'CENTRAL_ADMIN' },
      }),
    ),
    '고객 담당자 → 중앙 관리자',
  );
  assert.equal(eventSummary(event({ action: 'PLATFORM_ROLE_CHANGED', before_state: {}, after_state: {} })), '— → —');
  // 담당 고객 배정 변경은 개수만 보인다 — 고객 ID를 이력 한 줄에 나열하지 않는다.
  const assignment = event({
    action: 'TENANT_ASSIGNMENTS_CHANGED',
    before_state: { tenant_ids: [] },
    after_state: { tenant_ids: [TENANT_UUID, OTHER_TENANT_UUID] },
  });
  assert.equal(EVENT_LABELS[assignment.action], '담당 고객 변경');
  assert.equal(eventSummary(assignment), '0곳 → 2곳');
  assert.doesNotMatch(eventSummary(assignment), /123e4567|223e4567/);
  assert.equal(
    eventSummary(event({ action: 'TENANT_ASSIGNMENTS_CHANGED', before_state: { tenant_ids: null }, after_state: {} })),
    '— → —',
  );
  assert.equal(eventSummary(event({ action: 'ROLE_CHANGED', before_state: { role: null }, after_state: {} })), '— → —');
});
