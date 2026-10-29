// 상단바 계정 정보 항목 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { accountFields } from './account-summary.ts';

const TENANT_ID = '123e4567-e89b-12d3-a456-426614174000';
const view = (fields: ReturnType<typeof accountFields>) => fields.map(({ label, value }) => [label, value]);

test('고객사 계정 — 이메일 · 역할 · 계정 종류와 자기 고객 ID를 보인다', () => {
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
    ['역할', '조회 전용'],
    ['계정 종류', '고객사'],
    ['고객 ID', TENANT_ID],
  ]);
});

test('역할은 사용자 관리와 같은 이름으로 보인다', () => {
  const roleOf = (role: 'ADMIN' | 'OPERATOR' | 'VIEWER') =>
    accountFields({
      email: 'a@b.c',
      role,
      account_type: 'TENANT',
      tenant_id: TENANT_ID,
      platform_role: null,
      assigned_tenant_ids: [],
    }).find((f) => f.label === '역할')?.value;
  assert.equal(roleOf('ADMIN'), '관리자');
  assert.equal(roleOf('OPERATOR'), '운영자');
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

test('중앙 관리자 — 역할은 플랫폼 등급, 고객 범위는 전체이고 고객 ID 항목을 만들지 않는다', () => {
  assert.deepEqual(view(platformFields('CENTRAL_ADMIN', [])), [
    ['이메일', 'op@vigilantis.test'],
    ['역할', '중앙 관리자'],
    ['계정 종류', '플랫폼 운영자'],
    ['고객 범위', '전체 고객'],
  ]);
});

test('고객 담당자 — 고객 범위는 담당 고객 수, 배정이 없으면 "배정 없음"이다(전체로 보이지 않는다)', () => {
  const scopeOf = (ids: string[]) => platformFields('TENANT_MANAGER', ids).find((f) => f.label === '고객 범위')?.value;
  assert.equal(scopeOf([TENANT_ID, '223e4567-e89b-12d3-a456-426614174000']), '담당 고객 2곳');
  assert.equal(scopeOf([]), '배정 없음');
  // 담당 고객 ID는 나열하지 않는다.
  for (const field of platformFields('TENANT_MANAGER', [TENANT_ID])) assert.doesNotMatch(field.value, /123e4567/);
});

test('플랫폼 계정은 호환 역할 ADMIN이어도 "관리자"로 보이지 않는다', () => {
  const roleOf = (platformRole: 'CENTRAL_ADMIN' | 'TENANT_MANAGER') =>
    platformFields(platformRole, []).find((f) => f.label === '역할')?.value;
  assert.equal(roleOf('TENANT_MANAGER'), '고객 담당자');
  for (const platformRole of ['CENTRAL_ADMIN', 'TENANT_MANAGER'] as const) {
    assert.notEqual(roleOf(platformRole), '관리자', platformRole);
  }
});
