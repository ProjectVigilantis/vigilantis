// AST-001 절감 예상 집계 회귀 — `npm test`. 금액을 부풀리거나 미산출을 0원으로 덮는 것을 막는다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { formatUsd, savingsSummary } from './savings.ts';
import type { AiSavingsEstimate, AssetItem, IncidentResponse, RecommendationItem } from '@/types/api';

const ARN_A = 'arn:aws:ec2:ap-northeast-2:1:instance/i-0aaa';
const ARN_B = 'arn:aws:ec2:ap-northeast-2:1:instance/i-0bbb';

function estimate(amount: string, over: Partial<AiSavingsEstimate> = {}): AiSavingsEstimate {
  return {
    status: 'ESTIMATED',
    currency: 'USD',
    period: 'MONTH',
    amount,
    reason: null,
    basis: {
      target_arn: ARN_A,
      region: 'ap-northeast-2',
      current_instance_type: 't3.xlarge',
      target_instance_type: 't3.large',
      current_hourly_rate: '0.208000',
      target_hourly_rate: '0.104000',
      assumptions: {
        hours: 730,
        operating_system: 'LINUX',
        tenancy: 'SHARED',
        purchase_option: 'ON_DEMAND',
        included_cost: 'INSTANCE_COMPUTE_ONLY',
        pricing_source: 'MODEL_KNOWLEDGE',
      },
      explanation: '현재 단가와 목표 단가의 차이에 730시간을 곱했습니다.',
      explanation_source: 'SERVER_TEMPLATE',
    },
    ...over,
  };
}

function rec(targetArn: string, ai: AiSavingsEstimate | null): RecommendationItem {
  return {
    runbook_id: 'RUNBOOK_EC2_RIGHTSIZING',
    target_arn: targetArn,
    display_parameters: { target_instance_type: 't3.large' },
    ai_savings_estimate: ai,
  };
}

function incident(id: string, recs: RecommendationItem[]): IncidentResponse {
  return {
    incident_id: id,
    title: null,
    subject_arn: ARN_A,
    category: 'FINOPS',
    status: 'AWAITING_APPROVAL',
    initial_risk_level: null,
    reviewed_risk_level: null,
    response_mode: null,
    created_at: '2026-09-18T00:00:00Z',
    updated_at: '2026-09-18T00:00:00Z',
    summary_lines: [],
    evidence_ids: [],
    recommendations: recs,
    executions: [],
    resolution: null,
    resolved_at: null,
  };
}

const ASSETS = [
  { arn: ARN_A, resource_id: 'i-0aaa', name: 'seed-idle' },
  { arn: ARN_B, resource_id: 'i-0bbb', name: null },
] as unknown as AssetItem[];

test('금액이 나온 것만 큰 순으로 세우고 합계를 낸다', () => {
  const summary = savingsSummary(
    [incident('a', [rec(ARN_A, estimate('75.92'))]), incident('b', [rec(ARN_B, estimate('120.00'))])],
    ASSETS,
  );
  assert.deepEqual(
    summary.rows.map((r) => [r.arn, r.amount]),
    [
      [ARN_B, 120],
      [ARN_A, 75.92],
    ],
  );
  assert.equal(summary.total, 195.92);
  assert.equal(summary.unestimated, 0);
});

test('미산출은 0원으로 합치지 않고 따로 센다', () => {
  // UNAVAILABLE/INVALID 를 0으로 더하면 합계가 "이만큼이 전부"로 읽힌다.
  const summary = savingsSummary(
    [
      incident('a', [rec(ARN_A, estimate('10.00'))]),
      incident('b', [
        rec(ARN_B, { ...estimate('0'), status: 'UNAVAILABLE', amount: null, basis: null, reason: 'MODEL_UNAVAILABLE' }),
      ]),
    ],
    ASSETS,
  );
  assert.equal(summary.total, 10);
  assert.equal(summary.rows.length, 1);
  assert.equal(summary.unestimated, 1);
});

test('추정이 없는 후보(절감 대상 아님)는 미산출로 세지 않는다', () => {
  // NACL 차단 같은 후보는 계약상 필드가 null이다 — 실패가 아니라 해당 없음이다.
  const summary = savingsSummary([incident('a', [rec(ARN_A, null)])], ASSETS);
  assert.equal(summary.unestimated, 0);
  assert.equal(summary.rows.length, 0);
});

test('같은 자산에 추정이 둘이면 큰 쪽만 남는다', () => {
  // 인시던트가 여러 번 열린 자산에서 같은 다운사이징이 두 번 세어지면 합계가 부푼다.
  const summary = savingsSummary(
    [incident('a', [rec(ARN_A, estimate('10.00'))]), incident('b', [rec(ARN_A, estimate('30.00'))])],
    ASSETS,
  );
  assert.equal(summary.rows.length, 1);
  assert.equal(summary.total, 30);
});

test('이름이 없는 자산은 resource_id로, 목록에 없으면 ARN 꼬리로 적는다', () => {
  const summary = savingsSummary(
    [incident('a', [rec(ARN_B, estimate('1.00'))]), incident('b', [rec('arn:aws:ec2:x:1:instance/i-0zzz', estimate('2.00'))])],
    ASSETS,
  );
  assert.deepEqual(
    summary.rows.map((r) => r.label),
    ['i-0zzz', 'i-0bbb'],
  );
});

test('금액 표기는 소수 둘째 자리까지 고정이다', () => {
  assert.equal(formatUsd(1234.5), '$1,234.50');
});
