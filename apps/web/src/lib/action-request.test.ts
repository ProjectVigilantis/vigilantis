// ACT-001 승인 요청 스냅샷 회귀 — `npm test`. PR #351 리뷰 1: 모달을 연 사이 대시보드 1순위가 바뀌면
// A의 대상·파라미터를 보여 준 채 B의 `incident_id`를 보냈다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { executeBody, proposalRequest } from './action-request.ts';
import type { AssetItem, IncidentResponse, RecommendationItem } from '../types/api.ts';

function incident(id: string, recommendations: RecommendationItem[]): IncidentResponse {
  return {
    incident_id: id,
    title: null,
    subject_arn: `arn:aws:ec2:ap-northeast-2:1:security-group/sg-${id}`,
    category: 'SECOPS',
    status: 'AWAITING_APPROVAL',
    initial_risk_level: 'HIGH',
    reviewed_risk_level: null,
    response_mode: null,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    summary_lines: ['a', 'b', 'c'],
    evidence_ids: [],
    recommendations,
    executions: [],
    resolution: null,
    resolved_at: null,
  };
}

function naclDeny(naclId: string, ip: string): RecommendationItem {
  return {
    runbook_id: 'RUNBOOK_NACL_ADD_DENY',
    target_arn: `arn:aws:ec2:ap-northeast-2:1:network-acl/${naclId}`,
    display_parameters: { source_ip: ip },
    // 절감 예상은 EC2 RIGHTSIZING 후보에만 실린다 — NACL 차단 후보에서는 계약상 null이다.
    ai_savings_estimate: null,
  };
}

const NACL_A: AssetItem = {
  arn: 'arn:aws:ec2:ap-northeast-2:1:network-acl/acl-a',
  resource_id: 'acl-a',
  resource_role: 'RUNBOOK_SUPPORT',
  name: 'acl-a',
  account_id: '1',
  region: 'ap-northeast-2',
  state: null,
  relationships: [],
  evaluation_status: 'NOT_APPLICABLE',
  health_score: null,
  verdict: null,
  skip_reason_code: null,
  collected_at: '2026-09-01T00:00:00Z',
  asset_type: 'NACL',
  spec: { vpc_id: null, is_default: false, associated_subnet_ids: [] },
};

test('승인 요청은 연 순간의 인시던트와 후보를 한 스냅샷에 담는다', () => {
  const a = incident('a', [naclDeny('acl-a', '203.0.113.7/32')]);
  const request = proposalRequest(a, [NACL_A], 'key-1');

  assert.equal(request.incidentId, 'a');
  assert.equal(request.subjectArn, a.subject_arn);
  assert.equal(request.idempotencyKey, 'key-1');
  assert.equal(request.variant, 'ACTION');
  assert.deepEqual(
    request.candidates.map((c) => [c.runbookId, c.targetArn, c.displayParameters]),
    [['RUNBOOK_NACL_ADD_DENY', NACL_A.arn, { source_ip: '203.0.113.7/32' }]],
  );
  // 자산 조인 — 승인 모달의 `조치 대상` 사실값(#183)
  assert.equal(request.candidates[0].targetAsset?.arn, NACL_A.arn);
});

test('모달을 연 뒤 1순위가 같은 런북의 다른 건으로 바뀌어도 전송 본문은 연 건을 가리킨다', () => {
  const a = incident('a', [naclDeny('acl-a', '203.0.113.7/32')]);
  const request = proposalRequest(a, [NACL_A], 'key-1');

  // 대시보드 재조회로 1순위가 B가 됐다 — 같은 런북의 실행 가능한 후보가 있어 서버가 받아 주는 조합이다.
  const b = incident('b', [naclDeny('acl-b', '198.51.100.9/32')]);
  assert.equal(b.recommendations[0].runbook_id, request.candidates[0].runbookId);

  const body = executeBody(request, request.candidates[0].runbookId);
  assert.deepEqual(body, {
    incident_id: 'a',
    runbook_id: 'RUNBOOK_NACL_ADD_DENY',
    idempotency_key: 'key-1',
  });
  // 보여 준 대상(A의 NACL)과 보내는 인시던트가 같은 스냅샷에서 나왔다
  assert.equal(request.candidates[0].targetArn, a.recommendations[0].target_arn);
  assert.notEqual(body.incident_id, b.incident_id);
});

test('전송 본문은 3필드뿐이다 — 보이는 ARN·파라미터는 보내지 않는다(extra=forbid)', () => {
  const request = proposalRequest(incident('a', [naclDeny('acl-a', '203.0.113.7/32')]), [], 'k');
  assert.deepEqual(Object.keys(executeBody(request, 'RUNBOOK_NACL_ADD_DENY')).sort(), [
    'idempotency_key',
    'incident_id',
    'runbook_id',
  ]);
});

test('자산 조회가 실패해도(빈 배열) 요청은 만들어진다 — 조인만 비운다', () => {
  const request = proposalRequest(incident('a', [naclDeny('acl-a', '203.0.113.7/32')]), [], 'k');
  assert.equal(request.candidates.length, 1);
  assert.equal(request.candidates[0].targetAsset, null);
});
