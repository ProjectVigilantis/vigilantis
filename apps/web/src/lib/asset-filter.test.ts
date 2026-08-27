// 목록에 담기는 자산 판별 회귀 — `npm test`.
// `resource_role`로 걸러 미연결 EBS가 통째로 사라진 적이 있다(2026-08-27).

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { isJudgedAsset } from './asset-filter.ts';

test('EBS는 resource_role이 RUNBOOK_SUPPORT여도 목록에 남는다 — 판정·과금 대상이다', () => {
  // 계약이 EBS의 resource_role을 RUNBOOK_SUPPORT로 강제하면서(_PRIMARY_TYPES = {EC2, SG})
  // 동시에 Rule 판정 대상으로 둔다(_RULE_TARGET_TYPES = {EC2, SG, EBS}).
  assert.equal(isJudgedAsset({ evaluation_status: 'COMPLETED' }), true);
});

test('판정 비대상(NACL·ASG·LT·TG)만 빠진다', () => {
  assert.equal(isJudgedAsset({ evaluation_status: 'NOT_APPLICABLE' }), false);
});

test('수집 중·실패도 목록에 남는다 — 판정이 아직 없을 뿐 대상이다', () => {
  assert.equal(isJudgedAsset({ evaluation_status: 'PENDING' }), true);
  assert.equal(isJudgedAsset({ evaluation_status: 'FAILED' }), true);
});
