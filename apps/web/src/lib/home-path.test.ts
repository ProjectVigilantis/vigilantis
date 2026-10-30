import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  CUSTOMER_HOME,
  PLATFORM_HOME,
  USERS_PATH,
  businessOpen,
  confirmedAccount,
  homePathFor,
  navPathsFor,
  type SessionView,
} from './home-path.ts';
import type { AccessSubject } from './roles.ts';

// 기존 전역 플랫폼 계정은 중앙 관리자다(배정 목록은 계약상 빈 목록). 고객 담당자는 배정을 명시한다.
const platform: AccessSubject = {
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
const assignedManager = manager('123e4567-e89b-12d3-a456-426614174000');
const unassignedManager = manager();
const tenant = (role: 'ADMIN' | 'OPERATOR' | 'VIEWER'): AccessSubject => ({
  account_type: 'TENANT',
  role,
  platform_role: null,
  assigned_tenant_ids: [],
});
const TENANT_ROLES = ['ADMIN', 'OPERATOR', 'VIEWER'] as const;
const UNCONFIRMED: SessionView[] = [{ kind: 'loading' }, { kind: 'unknown' }, { kind: 'anonymous' }];

test('첫 화면 — 플랫폼은 고객 관리, 고객 ADMIN은 사용자 관리, 그 밖은 기존 루트', () => {
  assert.equal(homePathFor(platform), PLATFORM_HOME);
  assert.equal(homePathFor(assignedManager), PLATFORM_HOME);
  // 배정 없는 담당자도 고객 관리로 간다 — 거기서 배정 없음 안내를 본다.
  assert.equal(homePathFor(unassignedManager), PLATFORM_HOME);
  // 고객사 ADMIN은 ADMIN 역할이어도 고객 관리로 가지 않는다(역할만으로 플랫폼을 가르지 않는다).
  assert.equal(homePathFor(tenant('ADMIN')), USERS_PATH);
  assert.equal(homePathFor(tenant('OPERATOR')), CUSTOMER_HOME);
  assert.equal(homePathFor(tenant('VIEWER')), CUSTOMER_HOME);
});

test('메뉴 — 고객 ADMIN만 사용자 관리를 받고, 고객은 업무 화면 · 고객 관리를 받지 않는다', () => {
  assert.deepEqual(navPathsFor(platform), ['/', '/assets', '/security', '/tenants', '/users']);
  // 배정된 담당자: 중앙 관리자와 같은 메뉴다 — 각 화면의 데이터는 서버가 담당 고객으로 거른다.
  assert.deepEqual(navPathsFor(assignedManager), ['/', '/assets', '/security', '/tenants', '/users']);
  // 배정 없는 담당자: 업무 화면 · 사용자 관리 없이 고객 관리(배정 없음 안내)만 — 전역 접근처럼 보이지 않는다.
  assert.deepEqual(navPathsFor(unassignedManager), ['/tenants']);
  assert.deepEqual(navPathsFor(tenant('ADMIN')), ['/users']);
  assert.deepEqual(navPathsFor(tenant('OPERATOR')), []);
  assert.deepEqual(navPathsFor(tenant('VIEWER')), []);
});

test('세션 확인 전 · 조회 실패 · 미로그인 — 메뉴를 열지 않고 자산 조회 · 실시간 연결도 열지 않는다', () => {
  for (const session of UNCONFIRMED) {
    const account = confirmedAccount(session);
    assert.equal(account, null, session.kind);
    assert.deepEqual(navPathsFor(account), [], session.kind);
    assert.equal(businessOpen(account), false, session.kind);
  }
});

test('업무 기능(자산 조회 · 실시간 연결)은 고객 범위가 있는 확인된 플랫폼 계정에게만 열린다 — 사용자 관리 권한과 별개', () => {
  assert.equal(businessOpen(confirmedAccount({ kind: 'user', user: platform })), true);
  assert.equal(businessOpen(confirmedAccount({ kind: 'user', user: assignedManager })), true);
  // 담당 고객이 없는 담당자는 업무 화면 · 실시간 연결이 닫힌다(전역 접근으로 보이지 않는다).
  assert.equal(businessOpen(confirmedAccount({ kind: 'user', user: unassignedManager })), false);
  // 고객사 ADMIN은 사용자 관리 권한이 있어도 업무 기능은 격리 전까지 닫혀 있다.
  for (const role of TENANT_ROLES) {
    assert.equal(businessOpen(confirmedAccount({ kind: 'user', user: tenant(role) })), false, role);
  }
});
