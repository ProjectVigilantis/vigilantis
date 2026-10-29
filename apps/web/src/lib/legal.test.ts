// 처리방침 · 약관 운영자 정보 · 시행일 표기 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { formatLegalDate, LEGAL_EFFECTIVE_DATE, PRIVACY_OFFICER, ROLE_NAMES } from './legal.ts';

test('보호책임자 이메일은 자리표시자가 아닌 실제 주소다', () => {
  assert.match(PRIVACY_OFFICER.email, /^[^\s@_]+@[^\s@]+\.[a-z]+$/);
});

test('문서의 역할 이름은 사용자 관리 화면과 같다', () => {
  assert.equal(ROLE_NAMES, '관리자 · 운영자 · 조회 전용');
});

test('시행일은 앞자리 0 없이 연 · 월 · 일로 적는다', () => {
  assert.equal(formatLegalDate('2026-11-02'), '2026년 11월 2일');
  assert.equal(formatLegalDate('2027-01-09'), '2027년 1월 9일');
});

test('시행일 상수는 실제 날짜다', () => {
  assert.match(LEGAL_EFFECTIVE_DATE, /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(new Date(`${LEGAL_EFFECTIVE_DATE}T00:00:00Z`).toISOString().slice(0, 10), LEGAL_EFFECTIVE_DATE);
});
