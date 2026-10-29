// 인시던트 상세의 자동 발동 안내(#532). 서버의 `auto_isolation_block`이 Medium 자동 격리 예고를
// 승인 필요 안내로 바꾸는가, 그리고 High 대응 정책과 실행 기록을 따로 보여 주는가가 요점이다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  agentWaitNotice,
  inAgentWaitWindow,
  preMitigationRecordNote,
} from './auto-isolation-notice.ts';
import { RESPONSE_MODE_LABELS } from './enum-labels.ts';
import type { ExecutionStatus, IncidentResponse, IsoDateTime } from '../types/api.ts';

function incident(over: Partial<IncidentResponse> = {}): IncidentResponse {
  return {
    incident_id: 'notice', title: '테스트 사건', subject_arn: 'arn:test', category: 'SECOPS',
    status: 'AWAITING_APPROVAL', initial_risk_level: 'MEDIUM', reviewed_risk_level: null,
    response_mode: 'AGENT_WAIT', threat_context: null,
    summary_lines: [], evidence_ids: [], executions: [],
    analysis_result: { status: 'PROPOSALS_GENERATED', guardrail_rejections: [] },
    recommendations: [{ runbook_id: 'RUNBOOK_EC2_ISOLATE', target_arn: 'arn:test',
      display_parameters: {}, ai_savings_estimate: null }],
    resolution: null, resolution_note: null, resolved_at: null, auto_isolation_block: null,
    created_at: '2026-10-28T00:00:00Z', updated_at: '2026-10-28T00:00:00Z', ...over,
  };
}

function execution(status: ExecutionStatus): IncidentResponse['executions'][number] {
  return { execution_id: `execution-${status}`, runbook_id: 'RUNBOOK_NACL_ADD_DENY', status,
    available_recovery_runbook_ids: [], updated_at: '2026-10-28T00:00:01Z' };
}

const WAIT_AT = '2026-10-28T00:00:00Z' as IsoDateTime;
const blocked = incident({ auto_isolation_block: 'AUTO_ACTION_DISABLED' });

test('차단된 Medium은 시각이 있든 없든 실행 예고 대신 승인 필요 안내다', () => {
  assert.deepEqual(agentWaitNotice(blocked, WAIT_AT), { kind: 'BLOCKED' });
  // 목록에서 나중에 연 직접 진입: 전환 이벤트를 못 봐 기준 시각이 없다.
  assert.deepEqual(agentWaitNotice(blocked, null), { kind: 'BLOCKED' });
});

test('재활성화 뒤에도 서버가 차단을 주면 차단 안내를 유지한다', () => {
  // 재활성화 이전에 정한 발동은 소급 실행하지 않는다(#514). FE는 시각을 다시 계산하지 않고 값만 따른다.
  const reenabled = incident({ auto_isolation_block: 'AUTO_ACTION_DISABLED', updated_at: '2026-10-28T09:00:00Z' });
  assert.deepEqual(agentWaitNotice(reenabled, WAIT_AT), { kind: 'BLOCKED' });
});

test('차단 사유가 없는 Medium은 기존 안내를 유지한다', () => {
  assert.deepEqual(agentWaitNotice(incident(), WAIT_AT), {
    kind: 'SCHEDULED',
    startedAt: WAIT_AT,
    deadlineAt: '2026-10-28T00:01:00.000Z',
  });
  assert.deepEqual(agentWaitNotice(incident(), null), { kind: 'UNTIMED' });
});

test('상세를 다시 읽어 값이 바뀌면 안내도 바뀐다', () => {
  assert.equal(agentWaitNotice(incident(), WAIT_AT)?.kind, 'SCHEDULED');
  assert.equal(agentWaitNotice({ ...incident(), auto_isolation_block: 'AUTO_ACTION_DISABLED' }, WAIT_AT)?.kind, 'BLOCKED');
  assert.equal(agentWaitNotice({ ...blocked, auto_isolation_block: null }, WAIT_AT)?.kind, 'SCHEDULED');
});

test('Low·FINOPS·승인 대기 밖은 안내를 그리지 않는다', () => {
  const low = incident({ initial_risk_level: 'LOW' });
  const finops = incident({ category: 'FINOPS', initial_risk_level: null, response_mode: null });
  const running = incident({ status: 'ACTION_IN_PROGRESS' });
  for (const row of [low, finops, running]) {
    assert.equal(agentWaitNotice(row, WAIT_AT), null);
    assert.equal(agentWaitNotice({ ...row, auto_isolation_block: 'AUTO_ACTION_DISABLED' }, WAIT_AT), null);
  }
  // 기준 시각 래치 조건은 차단 여부와 상관없이 같다. 차단이 풀린 재조회에서 시각을 바로 쓴다.
  assert.equal(inAgentWaitWindow(blocked), true);
  assert.equal(inAgentWaitWindow(low), false);
});

test('High 대응 라벨은 결과가 아니라 정책 이름이다', () => {
  assert.equal(RESPONSE_MODE_LABELS.PRE_MITIGATION_0_5S?.label, '선차단 대응');
});

test('High는 실행 기록이 없으면 없다고 적고, 있으면 수행된 조치로 보낸다', () => {
  const high = { response_mode: 'PRE_MITIGATION_0_5S' as const };
  assert.equal(preMitigationRecordNote({ ...high, executions: [] }), '실행 기록 없음');
  const statuses: ExecutionStatus[] = ['IN_PROGRESS', 'SUCCESS', 'FAILED', 'ROLLED_BACK'];
  assert.equal(
    preMitigationRecordNote({ ...high, executions: statuses.map(execution) }),
    '4건 · 상태는 아래 「수행된 조치」에서 확인',
  );
});

test('선차단이 아닌 대응에는 조치 기록 줄을 붙이지 않는다', () => {
  for (const response_mode of ['AGENT_WAIT', 'TIMEOUT_ISOLATION_1M', null] as const) {
    assert.equal(preMitigationRecordNote({ response_mode, executions: [execution('SUCCESS')] }), null);
  }
});
