import assert from 'node:assert/strict';
import { test } from 'node:test';
import { closureAction, closureBlockedReason } from './incident-closure.ts';
import type { IncidentResponse, ExecutionStatus, AnalysisResultStatus } from '../types/api.ts';

function incident(over: Partial<IncidentResponse> = {}): IncidentResponse {
  return {
    incident_id: 'closure', title: '테스트 사건', subject_arn: 'arn:test', category: 'SECOPS',
    status: 'AWAITING_APPROVAL', initial_risk_level: 'HIGH', reviewed_risk_level: null,
    response_mode: 'PRE_MITIGATION_0_5S', threat_context: null,
    summary_lines: [], evidence_ids: [], executions: [],
    analysis_result: { status: 'PROPOSALS_GENERATED' },
    recommendations: [{ runbook_id: 'RUNBOOK_NACL_ADD_DENY', target_arn: 'arn:test',
      display_parameters: {}, ai_savings_estimate: null }],
    resolution: null, resolution_note: null, resolved_at: null,
    created_at: '2026-09-30T00:00:00Z', updated_at: '2026-09-30T00:00:00Z', ...over,
  };
}

function execution(status: ExecutionStatus): IncidentResponse['executions'][number] {
  return { execution_id: 'execution', runbook_id: 'RUNBOOK_NACL_ADD_DENY', status,
    available_recovery_runbook_ids: [], updated_at: '2026-09-30T00:00:00Z' };
}

test('SecOps·FinOps는 실행 전 남은 제안을 거절하고 종료할 수 있다', () => {
  for (const category of ['SECOPS', 'FINOPS'] as const) {
    const row = incident({ category, analysis_result: category === 'FINOPS' ? null : { status: 'PROPOSALS_GENERATED' } });
    assert.equal(closureBlockedReason(row), null);
    assert.equal(closureAction(row).label, '제안 거절하고 종료');
  }
});

test('무제안·전체 거절·기록 부족·분석 실패는 실행 없이 종료할 수 있다', () => {
  for (const status of ['NO_PROPOSAL', 'GUARDRAIL_REJECTED', 'UNAVAILABLE', 'FAILED'] as AnalysisResultStatus[]) {
    assert.equal(closureBlockedReason(incident({ status: status === 'FAILED' ? 'FAILED' : 'AWAITING_CLOSURE',
      recommendations: [], analysis_result: { status } })), null);
  }
});

test('차단 후 해제 제안을 실행하지 않고 종료할 수 있다', () => {
  const row = incident({ executions: [execution('SUCCESS')],
    recommendations: [{ runbook_id: 'RUNBOOK_NACL_RESTORE', target_arn: 'arn:test',
      display_parameters: {}, ai_savings_estimate: null }] });
  assert.equal(closureBlockedReason(row), null);
  assert.equal(closureAction(row).label, '차단 유지하고 종료');
});

test('성공한 선행 조치가 있어도 분석 중에는 종료할 수 없다', () => {
  for (const status of ['PENDING', 'IN_PROGRESS'] as const) {
    assert.match(closureBlockedReason(incident({ executions: [execution('SUCCESS')],
      status: 'AWAITING_CLOSURE', analysis_result: { status } }))!, /분석/);
  }
  assert.match(closureBlockedReason(incident({ category: 'FINOPS', status: 'ANALYZING', analysis_result: null }))!, /분석/);
  assert.notEqual(closureBlockedReason(incident({ analysis_result: null })), null);
});

test('인시던트가 종료 대기로 보여도 진행 중 실행이 있으면 종료를 막는다', () => {
  for (const status of ['IN_PROGRESS', 'ROLLBACK_INITIATED'] as const) {
    assert.match(closureBlockedReason(incident({ status: 'AWAITING_CLOSURE', executions: [execution(status)] }))!, /실행/);
  }
});

test('실패·미검증은 조치 성공 판단으로 바꾸지 않고 추가 조치 없이 종료한다', () => {
  for (const status of ['FAILED', 'UNVERIFIED', 'ROLLBACK_FAILED', 'ROLLED_BACK'] as const) {
    const row = incident({ status: 'FAILED', recommendations: [], executions: [execution(status)] });
    assert.equal(closureBlockedReason(row), null);
    assert.equal(closureAction(row).label, '추가 조치 없이 종료');
  }
});

test('종료된 사건은 다시 종료 버튼을 활성화하지 않는다', () => {
  assert.notEqual(closureBlockedReason(incident({ status: 'RESOLVED' })), null);
});


test('제안·실행 없는 사건은 간단한 종료 버튼을 쓴다', () => {
  assert.equal(closureAction(incident({ recommendations: [] })).label, '종료');
});

test('차단→해제 뒤 종료에서 과거 차단 이력만 보고 차단 유지라고 하지 않는다', () => {
  const restored = { ...execution('SUCCESS'), runbook_id: 'RUNBOOK_NACL_RESTORE' as const,
    updated_at: '2026-09-30T00:01:00Z' };
  const row = incident({ recommendations: [], executions: [execution('SUCCESS'), restored] });
  assert.equal(closureAction(row).label, '추가 조치 없이 종료');
  // 낡은 해제 제안이 있더라도 최신 해제 성공을 무시하지 않는다. 배열 순서에도 의존하지 않는다.
  row.recommendations = [{ runbook_id: 'RUNBOOK_NACL_RESTORE', target_arn: 'arn:test',
    display_parameters: {}, ai_savings_estimate: null }];
  assert.equal(closureAction(row).label, '제안 거절하고 종료');
  row.executions.reverse();
  assert.equal(closureAction(row).label, '제안 거절하고 종료');
});

test('동시각 해제·차단, 실패한 차단은 차단 유지로 단정하지 않는다', () => {
  const recommendations = [{ runbook_id: 'RUNBOOK_NACL_RESTORE' as const, target_arn: 'arn:test',
    display_parameters: {}, ai_savings_estimate: null }];
  for (const executions of [
    [execution('FAILED')],
    [execution('SUCCESS'), { ...execution('SUCCESS'), runbook_id: 'RUNBOOK_NACL_RESTORE' as const }],
  ]) {
    assert.equal(closureAction(incident({ recommendations, executions })).label, '제안 거절하고 종료');
  }
});
