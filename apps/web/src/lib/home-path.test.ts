import assert from 'node:assert/strict';
import { test } from 'node:test';

import { CUSTOMER_HOME, PLATFORM_HOME, USERS_PATH, homePathFor, navPathsFor } from './home-path.ts';

const platform = { account_type: 'PLATFORM', role: 'ADMIN' } as const;
const tenant = (role: 'ADMIN' | 'OPERATOR' | 'VIEWER') => ({ account_type: 'TENANT', role }) as const;

test('첫 화면 — 플랫폼은 고객 관리, 고객사 ADMIN은 사용자 관리, 고객사 그 밖의 역할은 고객 첫 화면', () => {
  assert.equal(homePathFor(platform), PLATFORM_HOME);
  // 고객사 ADMIN은 ADMIN 역할이어도 고객 관리로 가지 않는다(역할만으로 플랫폼을 가르지 않는다).
  assert.equal(homePathFor(tenant('ADMIN')), USERS_PATH);
  assert.equal(homePathFor(tenant('OPERATOR')), CUSTOMER_HOME);
  assert.equal(homePathFor(tenant('VIEWER')), CUSTOMER_HOME);
});

test('메뉴 — 고객 관리는 플랫폼에게만, 사용자 관리는 플랫폼과 고객사 ADMIN에게', () => {
  assert.deepEqual(navPathsFor(platform), ['/', '/assets', '/security', '/tenants', '/users']);
  assert.deepEqual(navPathsFor(tenant('ADMIN')), ['/users']);
  assert.deepEqual(navPathsFor(tenant('OPERATOR')), []);
  assert.deepEqual(navPathsFor(tenant('VIEWER')), []);
});

test('메뉴 — 계정을 아직 모르면 업무 화면만 보이고 관리 메뉴는 열지 않는다', () => {
  assert.deepEqual(navPathsFor(null), ['/', '/assets', '/security']);
});
