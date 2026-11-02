// 상단바 계정 정보 항목 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { accountFields } from './account-summary.ts';
import { accountKind } from './roles.ts';

const TENANT_ID = '123e4567-e89b-12d3-a456-426614174000';
const view = (fields: ReturnType<typeof accountFields>) => fields.map(({ label, value }) => [label, value]);

test('고객사 직원계정 — 이메일 · 계정 종류 · 권한과 자기 고객사 ID를 보인다', () => {
  const fields = accountFields({
    email: 'viewer@acme.test',
    role: 'VIEWER',
    account_type: 'TENANT',
    tenant_id: TENANT_ID,
    platform_role: null,
    assigned_tenant_ids: [],
  });
  assert.deepEqual(view(fields), [
    ['이메일', 'viewer@acme.test'],
    ['계정 종류', '고객사 직원계정'],
    ['권한', '조회 전용'],
    ['고객사 ID', TENANT_ID],
  ]);
});

test('고객사 계정의 권한은 계정 관리와 같은 이름이고, "관리자"와 겹치지 않는다', () => {
  const roleOf = (role: 'ADMIN' | 'OPERATOR' | 'VIEWER') =>
    accountFields({
      email: 'a@b.c',
      role,
      account_type: 'TENANT',
      tenant_id: TENANT_ID,
      platform_role: null,
      assigned_tenant_ids: [],
    }).find((f) => f.label === '권한')?.value;
  assert.equal(roleOf('ADMIN'), '직원 관리');
  assert.equal(roleOf('OPERATOR'), '조치');
  assert.equal(roleOf('VIEWER'), '조회 전용');
});

const platformFields = (platform_role: 'CENTRAL_ADMIN' | 'TENANT_MANAGER', assigned_tenant_ids: string[]) =>
  accountFields({
    email: 'op@vigilantis.test',
    role: 'ADMIN',
    account_type: 'PLATFORM',
    tenant_id: null,
    platform_role,
    assigned_tenant_ids,
  });

test('중앙 관리계정 — 고객사 범위는 전체이고, 소속 고객사가 없어 권한 · 고객사 ID 항목을 만들지 않는다', () => {
  assert.deepEqual(view(platformFields('CENTRAL_ADMIN', [])), [
    ['이메일', 'op@vigilantis.test'],
    ['계정 종류', '중앙 관리계정'],
    ['고객사 범위', '전체 고객사'],
  ]);
});

test('관리자 직원계정 — 고객사 범위는 담당 고객사 수, 배정이 없으면 "배정 없음"이다(전체로 보이지 않는다)', () => {
  const scopeOf = (ids: string[]) => platformFields('TENANT_MANAGER', ids).find((f) => f.label === '고객사 범위')?.value;
  assert.equal(scopeOf([TENANT_ID, '223e4567-e89b-12d3-a456-426614174000']), '담당 고객사 2곳');
  assert.equal(scopeOf([]), '배정 없음');
  // 담당 고객사 ID는 나열하지 않는다.
  for (const field of platformFields('TENANT_MANAGER', [TENANT_ID])) assert.doesNotMatch(field.value, /123e4567/);
});

test('계정 종류 — 넷이다: CENTRAL_ADMIN · TENANT_MANAGER는 platform_role로, 고객사 계정은 role로 가른다', () => {
  assert.equal(accountKind({ account_type: 'PLATFORM', platform_role: 'CENTRAL_ADMIN', role: 'ADMIN' }), 'CENTRAL_ADMIN');
  assert.equal(accountKind({ account_type: 'PLATFORM', platform_role: 'TENANT_MANAGER', role: 'ADMIN' }), 'TENANT_MANAGER');
  assert.equal(accountKind({ account_type: 'TENANT', platform_role: null, role: 'ADMIN' }), 'TENANT_ADMIN');
  assert.equal(accountKind({ account_type: 'TENANT', platform_role: null, role: 'OPERATOR' }), 'TENANT_STAFF');
  assert.equal(accountKind({ account_type: 'TENANT', platform_role: null, role: 'VIEWER' }), 'TENANT_STAFF');
  // 플랫폼 계정의 호환 역할 role=ADMIN은 종류를 가르지 않는다 — 관리자 직원계정은 "중앙 관리계정"으로 보이지 않는다.
  const kindOf = platformFields('TENANT_MANAGER', []).find((f) => f.label === '계정 종류')?.value;
  assert.equal(kindOf, '관리자 직원계정');
});
