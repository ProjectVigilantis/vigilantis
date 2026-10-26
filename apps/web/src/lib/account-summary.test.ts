// 상단바 계정 정보 항목 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { accountFields } from './account-summary.ts';

const TENANT_ID = '123e4567-e89b-12d3-a456-426614174000';
const view = (fields: ReturnType<typeof accountFields>) => fields.map(({ label, value }) => [label, value]);

test('고객사 계정 — 이메일 · 역할 · 계정 종류와 자기 고객 ID를 보인다', () => {
  const fields = accountFields({ email: 'viewer@acme.test', role: 'VIEWER', account_type: 'TENANT', tenant_id: TENANT_ID });
  assert.deepEqual(view(fields), [
    ['이메일', 'viewer@acme.test'],
    ['역할', '조회 전용'],
    ['계정 종류', '고객사'],
    ['고객 ID', TENANT_ID],
  ]);
});

test('역할은 사용자 관리와 같은 이름으로 보인다', () => {
  const roleOf = (role: 'ADMIN' | 'OPERATOR' | 'VIEWER') =>
    accountFields({ email: 'a@b.c', role, account_type: 'TENANT', tenant_id: TENANT_ID }).find((f) => f.label === '역할')?.value;
  assert.equal(roleOf('ADMIN'), '관리자');
  assert.equal(roleOf('OPERATOR'), '운영자');
  assert.equal(roleOf('VIEWER'), '조회 전용');
});

test('플랫폼 계정 — 소속 고객이 없어 고객 ID 항목을 만들지 않는다', () => {
  const fields = accountFields({ email: 'op@vigilantis.test', role: 'ADMIN', account_type: 'PLATFORM', tenant_id: null });
  assert.deepEqual(view(fields), [
    ['이메일', 'op@vigilantis.test'],
    ['역할', '관리자'],
    ['계정 종류', '플랫폼 운영자'],
  ]);
});
