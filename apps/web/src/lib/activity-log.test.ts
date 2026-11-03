import assert from 'node:assert/strict';
import { test } from 'node:test';

import { actorLabel, actorNote, buildLog, logKindCounts } from './activity-log.ts';
import type { ThreatEventItem } from './threat-events.ts';
import type { AuditEventItem, IncidentListItem } from '../types/api.ts';

const VM = 'arn:aws:ec2:ap-northeast-2:310987654321:instance/i-1';

const incident = {
  incident_id: 'inc-1',
  title: 'web-01 노출',
  subject_arn: VM,
  category: 'SECOPS',
  status: 'RESOLVED',
  initial_risk_level: 'HIGH',
  reviewed_risk_level: null,
  response_mode: null,
  threat_context: null,
  analysis_result: null,
  created_at: '2026-11-03T01:00:00Z',
  updated_at: '2026-11-03T04:00:00Z',
} as unknown as IncidentListItem;

const audit: AuditEventItem[] = [
  {
    action: 'ACTION_EXECUTION_STARTED',
    event_id: 'a1',
    occurred_at: '2026-11-03T02:00:00Z',
    actor: { kind: 'TENANT_USER', email: 'infra@hanbit.example' },
    incident_id: 'inc-1',
    tenant_id: 'd2',
    trigger_source: 'USER_APPROVAL',
    execution_id: 'ex-1',
    runbook_id: 'RUNBOOK_EC2_ISOLATE',
    target_arn: VM,
  },
  {
    action: 'INCIDENT_RESOLVED',
    event_id: 'a2',
    occurred_at: '2026-11-03T04:00:00Z',
    actor: { kind: 'PLATFORM_USER', email: null },
    incident_id: 'inc-1',
    tenant_id: 'd2',
    resolution: 'JUSTIFIED',
  },
];

const threat: ThreatEventItem = {
  threat_event_id: 't1',
  event_type: 'SSH_BRUTE_FORCE',
  target_arn: VM,
  occurred_at: '2026-11-03T03:00:00Z',
  payload: { source_ip: '203.0.113.7', failed_attempt_count: 42, window_seconds: 300 },
};

test('세 원천을 최신순으로 합치고 기간 밖 인시던트는 뺀다', () => {
  const log = buildLog({ audit, threats: [threat], incidents: [incident], assetNames: { [VM]: 'web-01' }, sinceMs: Date.parse('2026-11-03T00:00:00Z') });
  assert.deepEqual(
    log.map((e) => [e.kind, e.title, e.actor, e.target]),
    [
      ['ACTION', '인시던트 종료', '플랫폼 운영자', 'web-01 노출'],
      ['THREAT', 'SSH 무차별 대입', '출발지 203.0.113.7', 'web-01'],
      ['ACTION', 'EC2 격리 실행', 'infra@hanbit.example', 'web-01'],
      ['INCIDENT', '인시던트 발생', '시스템 탐지', 'web-01 노출'],
    ],
  );
  assert.deepEqual(logKindCounts(log), { ACTION: 2, THREAT: 1, INCIDENT: 1 });

  const later = buildLog({ audit: [], threats: [], incidents: [incident], assetNames: {}, sinceMs: Date.parse('2026-11-03T02:00:00Z') });
  assert.equal(later.length, 0);
});

test('시스템 실행은 발동 사유로 적는다', () => {
  assert.equal(actorLabel({ kind: 'SYSTEM', email: null }, 'TIMEOUT_ISOLATION_1M'), '시스템 · 1분 미응답 자동 격리');
  assert.equal(actorLabel({ kind: 'PLATFORM_USER', email: 'ops@vigilantis.example' }), 'ops@vigilantis.example');
});

test('직원은 계정 목록의 권한을 붙이고, 못 읽었으면 직원으로만 적는다', () => {
  const infra = { kind: 'TENANT_USER', email: 'infra@hanbit.example' } as const;
  assert.equal(actorNote(infra, { 'infra@hanbit.example': '조치' }), '직원 · 조치 권한');
  assert.equal(actorNote(infra, {}), '직원');
  assert.equal(actorNote({ kind: 'PLATFORM_USER', email: null }, {}), '플랫폼 운영자');
  assert.equal(actorNote({ kind: 'SYSTEM', email: null }, {}), null);
});
