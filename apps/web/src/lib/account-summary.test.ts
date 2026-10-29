// 상단바 계정 정보 항목 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { accountFields } from './account-summary.ts';
import { accountKind } from './roles.ts';

const TENANT_ID = '123e4567-e89b-12d3-a456-426614174000';
const view = (fields: ReturnType<typeof accountFields>) => fields.map(({ label, value }) => [label, value]);

test('고객사 직원 — 이메일 · 계정 종류 · 권한과 자기 고객사 ID를 보인다', () => {
  const fields = accountFields({ email: 'viewer@acme.test', role: 'VIEWER', account_type: 'TENANT', tenant_id: TENANT_ID });
  assert.deepEqual(view(fields), [
    ['이메일', 'viewer@acme.test'],
    ['계정 종류', '고객사 직원'],
    ['권한', '조회 전용'],
    ['고객사 ID', TENANT_ID],
  ]);
});

test('고객사 직원의 권한은 계정 관리와 같은 이름이고, "관리자"와 겹치지 않는다', () => {
  const roleOf = (role: 'ADMIN' | 'OPERATOR' | 'VIEWER') =>
    accountFields({ email: 'a@b.c', role, account_type: 'TENANT', tenant_id: TENANT_ID }).find((f) => f.label === '권한')?.value;
  assert.equal(roleOf('ADMIN'), '직원 관리');
  assert.equal(roleOf('OPERATOR'), '조치');
  assert.equal(roleOf('VIEWER'), '조회 전용');
});

test('관리자 — 소속 고객사가 없어 권한 · 고객사 ID 항목을 만들지 않는다', () => {
  const fields = accountFields({ email: 'op@vigilantis.test', role: 'ADMIN', account_type: 'PLATFORM', tenant_id: null });
  assert.deepEqual(view(fields), [
    ['이메일', 'op@vigilantis.test'],
    ['계정 종류', '관리자'],
  ]);
});

test('계정 종류 — PLATFORM + ADMIN은 관리자, PLATFORM + 그 밖은 관리자 직원, TENANT는 고객사 직원', () => {
  assert.equal(accountKind({ account_type: 'PLATFORM', role: 'ADMIN' }), 'PLATFORM_ADMIN');
  assert.equal(accountKind({ account_type: 'PLATFORM', role: 'OPERATOR' }), 'PLATFORM_STAFF');
  assert.equal(accountKind({ account_type: 'TENANT', role: 'ADMIN' }), 'TENANT_STAFF');
});
