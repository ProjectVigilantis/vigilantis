// 역할별 조치 가능 여부 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  ACTION_FORBIDDEN,
  UNASSIGNED_ACTION_BLOCKED,
  VIEWER_ACTION_BLOCKED,
  canAccessTenant,
  canCreateTenant,
  canManageUsers,
  canManageUsersOf,
  canOperate,
  hasCustomerScope,
  isCentralAdmin,
  isPlatform,
  isUnassignedManager,
  operateBlockedReason,
  type AccessSubject,
  type UserAdminSubject,
} from './roles.ts';

const TENANT_A = '123e4567-e89b-12d3-a456-426614174000';
const TENANT_B = '223e4567-e89b-12d3-a456-426614174000';
const TENANT_C = '323e4567-e89b-12d3-a456-426614174000';

// 플랫폼 계정은 호환 역할 ADMIN을 그대로 싣는다 — 권한은 platform_role과 담당 배정에서만 나와야 한다.
// 중앙 관리자의 배정 목록은 계약상 언제나 빈 목록이다(빈 목록 = 전체가 아니라 등급으로 판정).
const central: AccessSubject = {
  account_type: 'PLATFORM',
  role: 'ADMIN',
  platform_role: 'CENTRAL_ADMIN',
  assigned_tenant_ids: [],
};
const manager = (...assigned: string[]): AccessSubject => ({
  account_type: 'PLATFORM',
  role: 'ADMIN',
  platform_role: 'TENANT_MANAGER',
  assigned_tenant_ids: assigned,
});
const unassigned = manager();
const tenant = (role: 'ADMIN' | 'OPERATOR' | 'VIEWER', tenant_id = TENANT_A): UserAdminSubject => ({
  account_type: 'TENANT',
  tenant_id,
  role,
  platform_role: null,
  assigned_tenant_ids: [],
});

test('중앙 관리자는 등급으로 판정한다 — 빈 배정 목록이어도 전체 고객 범위다', () => {
  assert.equal(isCentralAdmin(central), true);
  assert.equal(hasCustomerScope(central), true);
  for (const tenantId of [TENANT_A, TENANT_B, TENANT_C]) assert.equal(canAccessTenant(central, tenantId), true, tenantId);
  assert.equal(isUnassignedManager(central), false);
});

test('고객 담당자는 배정된 고객만 — 넓은 자격(하나 이상 배정)과 대상별 권한을 따로 판정한다', () => {
  const assigned = manager(TENANT_A, TENANT_B);
  assert.equal(isCentralAdmin(assigned), false);
  assert.equal(hasCustomerScope(assigned), true);
  assert.equal(canAccessTenant(assigned, TENANT_A), true);
  assert.equal(canAccessTenant(assigned, TENANT_B), true);
  assert.equal(canAccessTenant(assigned, TENANT_C), false);
  // UUID 표기의 대소문자는 같은 고객이다.
  assert.equal(canAccessTenant(assigned, TENANT_A.toUpperCase()), true);
  assert.equal(isUnassignedManager(assigned), false);
});

test('담당 고객이 없는 고객 담당자는 고객 범위가 없다 — 어떤 고객도 다루지 못한다', () => {
  assert.equal(isUnassignedManager(unassigned), true);
  assert.equal(hasCustomerScope(unassigned), false);
  assert.equal(canAccessTenant(unassigned, TENANT_A), false);
  assert.equal(canCreateTenant(unassigned), false);
});

test('고객사 계정 · 계정 미상은 플랫폼 고객 범위가 없다', () => {
  for (const subject of [tenant('ADMIN'), null, undefined]) {
    assert.equal(hasCustomerScope(subject), false);
    assert.equal(canAccessTenant(subject, TENANT_A), false);
    assert.equal(isCentralAdmin(subject), false);
    assert.equal(isUnassignedManager(subject), false);
  }
});

test('고객 추가는 중앙 관리자만', () => {
  assert.equal(canCreateTenant(central), true);
  assert.equal(canCreateTenant(manager(TENANT_A)), false);
  assert.equal(canCreateTenant(tenant('ADMIN')), false);
  assert.equal(canCreateTenant(null), false);
});

test('사용자 관리는 고객 범위가 있는 플랫폼 계정과 고객사 ADMIN — 배정 없는 담당자와 계정을 모르면 숨긴다', () => {
  assert.equal(canManageUsers(central), true);
  assert.equal(canManageUsers(manager(TENANT_A)), true);
  assert.equal(canManageUsers(unassigned), false);
  assert.equal(canManageUsers(tenant('ADMIN')), true);
  for (const role of ['OPERATOR', 'VIEWER'] as const) {
    assert.equal(canManageUsers(tenant(role)), false, role);
  }
  assert.equal(canManageUsers(null), false);
  assert.equal(canManageUsers(undefined), false);
});

test('고객사 사용자 대상별 관리 — 중앙 관리자 전체 · 담당자는 배정된 고객 · 고객사 ADMIN은 자기 고객사', () => {
  assert.equal(canManageUsersOf({ ...central, tenant_id: null }, TENANT_C), true);
  const assigned = { ...manager(TENANT_A), tenant_id: null };
  assert.equal(canManageUsersOf(assigned, TENANT_A), true);
  assert.equal(canManageUsersOf(assigned, TENANT_B), false);
  assert.equal(canManageUsersOf({ ...unassigned, tenant_id: null }, TENANT_A), false);
  assert.equal(canManageUsersOf(tenant('ADMIN', TENANT_A), TENANT_A), true);
  assert.equal(canManageUsersOf(tenant('ADMIN', TENANT_A), TENANT_B), false);
  assert.equal(canManageUsersOf(tenant('OPERATOR', TENANT_A), TENANT_A), false);
  // 대상의 고객을 모르거나 계정을 모르면 아니다.
  assert.equal(canManageUsersOf({ ...central, tenant_id: null }, null), false);
  assert.equal(canManageUsersOf(null, TENANT_A), false);
});

test('플랫폼 여부는 account_type으로만 가른다 — 고객사 ADMIN은 플랫폼이 아니다', () => {
  assert.equal(isPlatform({ account_type: 'PLATFORM' }), true);
  assert.equal(isPlatform({ account_type: 'TENANT' }), false);
  assert.equal(isPlatform(null), false);
});

test('고객사 계정은 역할로 — ADMIN · OPERATOR만 조치할 수 있다', () => {
  assert.equal(canOperate(tenant('ADMIN')), true);
  assert.equal(canOperate(tenant('OPERATOR')), true);
  assert.equal(canOperate(tenant('VIEWER')), false);
});

test('플랫폼 계정은 호환 역할 ADMIN이 아니라 등급 · 배정으로 조치 자격을 판정한다', () => {
  assert.equal(canOperate(central), true);
  assert.equal(canOperate(manager(TENANT_A)), true);
  assert.equal(canOperate(unassigned), false);
});

test('계정을 아직 모르면 조치할 수 없다', () => {
  assert.equal(canOperate(null), false);
  assert.equal(canOperate(undefined), false);
});

test('막는 이유 — 조회 전용 · 배정 없음 · 계정 미확인을 다른 문구로 말한다', () => {
  assert.equal(operateBlockedReason(tenant('ADMIN')), null);
  assert.equal(operateBlockedReason(central), null);
  assert.equal(operateBlockedReason(manager(TENANT_A)), null);
  assert.equal(operateBlockedReason(tenant('VIEWER')), VIEWER_ACTION_BLOCKED);
  assert.equal(VIEWER_ACTION_BLOCKED, '조회 전용 계정은 조치할 수 없습니다.');
  // 배정 없는 담당자에게 "조회 전용 계정"이라고 말하지 않는다.
  assert.equal(operateBlockedReason(unassigned), UNASSIGNED_ACTION_BLOCKED);
  assert.notEqual(UNASSIGNED_ACTION_BLOCKED, VIEWER_ACTION_BLOCKED);
  const pending = operateBlockedReason(null);
  assert.ok(pending !== null && pending !== VIEWER_ACTION_BLOCKED && pending !== UNASSIGNED_ACTION_BLOCKED);
});

test('서버 403 문구는 역할과 담당 범위를 함께 말한다 — 조회 전용으로 단정하지 않는다', () => {
  assert.notEqual(ACTION_FORBIDDEN, VIEWER_ACTION_BLOCKED);
  assert.match(ACTION_FORBIDDEN, /담당 고객/);
});
