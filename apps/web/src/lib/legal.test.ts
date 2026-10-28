// 처리방침 · 약관 시행일 표기 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { formatLegalDate, LEGAL_EFFECTIVE_DATE } from './legal.ts';

test('시행일은 앞자리 0 없이 연 · 월 · 일로 적는다', () => {
  assert.equal(formatLegalDate('2026-11-02'), '2026년 11월 2일');
  assert.equal(formatLegalDate('2027-01-09'), '2027년 1월 9일');
});

test('시행일 상수는 실제 날짜다', () => {
  assert.match(LEGAL_EFFECTIVE_DATE, /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(new Date(`${LEGAL_EFFECTIVE_DATE}T00:00:00Z`).toISOString().slice(0, 10), LEGAL_EFFECTIVE_DATE);
});
