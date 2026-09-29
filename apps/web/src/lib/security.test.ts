// SEC-001 보안 관제 집계 검증 — `node --test`. 픽스처는 dashboard.test.ts와 같은 모양이다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { focusAssets, focusScope, secopsStatusCounts, securityMetrics } from './security.ts';
import type { AssetItem, IncidentListItem, IncidentStatus, OpenPortRule } from '../types/api.ts';

type Common = Omit<AssetItem, 'asset_type' | 'spec'>;

function common(id: string, over: Partial<Common> = {}): Common {
  return {
    arn: `arn:test:${id}`,
    resource_id: id,
    resource_role: 'PRIMARY',
    name: id,
    account_id: '1',
    region: 'ap-northeast-2',
    state: null,
    relationships: [],
    evaluation_status: 'COMPLETED',
    health_score: null,
    verdict: null,
    skip_reason_code: null,
    collected_at: '2026-09-01T00:00:00Z',
    ...over,
  };
}

function ec2(id: string, securedBy: string[], over: Partial<Common> = {}): AssetItem {
  return {
    ...common(id, {
      relationships: securedBy.map((sgId) => ({ relation_type: 'SECURED_BY', target_arn: `arn:test:${sgId}` })),
      ...over,
    }),
    asset_type: 'EC2',
    spec: { instance_type: 't3.small', availability_zone: null, vpc_id: null, subnet_id: null, private_ip: null },
  };
}

function sg(id: string, open: OpenPortRule[], over: Partial<Common> = {}): AssetItem {
  return {
    ...common(id, over),
    asset_type: 'SG',
    spec: { description: null, vpc_id: null, attached: true, open_to_world: open },
  };
}

function nacl(id: string): AssetItem {
  return {
    ...common(id, { resource_role: 'RUNBOOK_SUPPORT', evaluation_status: 'NOT_APPLICABLE' }),
    asset_type: 'NACL',
    spec: { vpc_id: null, is_default: false, associated_subnet_ids: [] },
  };
}

function incident(
  id: string,
  status: IncidentStatus,
  category: 'FINOPS' | 'SECOPS' = 'SECOPS',
  subject = 'x',
): IncidentListItem {
  return {
    incident_id: id,
    title: null,
    subject_arn: `arn:test:${subject}`,
    category,
    status,
    initial_risk_level: null,
    reviewed_risk_level: null,
    response_mode: null,
    threat_context: null,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
  };
}

const SSH: OpenPortRule = { protocol: 'tcp', from_port: 22, to_port: 22, ipv6: false };

test('영향 EC2는 개방 SG 뒤의 인스턴스를 중복 없이 센다 — 개방 SG 둘에 걸린 한 대는 한 대다', () => {
  const items = [
    sg('open-a', [SSH], { verdict: 'THREAT' }),
    sg('open-b', [SSH], { verdict: 'THREAT' }),
    sg('closed', []),
    ec2('both', ['open-a', 'open-b']),
    ec2('one', ['open-b']),
    ec2('safe', ['closed']),
    nacl('n'),
  ];
  const m = securityMetrics(items, []);
  assert.equal(m.sgTotal, 3);
  assert.equal(m.openSg, 2);
  assert.equal(m.affectedEc2, 2);
  assert.equal(m.threat, 2);
  assert.equal(m.nacl, 1);
});

test('미조치 보안 인시던트는 SECOPS만 세고, 조회 실패는 0이 아니라 null이다', () => {
  const incidents = [
    incident('s-wait', 'AWAITING_APPROVAL'),
    incident('s-done', 'RESOLVED'),
    incident('f-wait', 'AWAITING_APPROVAL', 'FINOPS'),
  ];
  assert.equal(securityMetrics([], incidents).unhandled, 1);
  assert.equal(securityMetrics([], null).unhandled, null);
});

// ── 지표 띠 칸 = 초점 ────────────────────────────────────────────────────────

const FLEET = [
  sg('open', [SSH], { verdict: 'THREAT' }),
  sg('closed', []),
  ec2('behind', ['open']),
  ec2('safe', ['closed'], { region: 'us-east-1' }),
  nacl('n'),
];
const arns = (list: AssetItem[]) => list.map((a) => a.resource_id).sort();

test('칸마다 가리키는 자산 집합이 갈린다 — 개방 SG와 그 뒤의 EC2는 다른 칸이다', () => {
  assert.deepEqual(arns(focusAssets(FLEET, [], 'SG')), ['closed', 'open']);
  assert.deepEqual(arns(focusAssets(FLEET, [], 'OPEN_SG')), ['open']);
  assert.deepEqual(arns(focusAssets(FLEET, [], 'AFFECTED_EC2')), ['behind']);
  assert.deepEqual(arns(focusAssets(FLEET, [], 'THREAT')), ['open']);
  assert.deepEqual(arns(focusAssets(FLEET, [], 'NACL')), ['n']);
});

test('미조치 칸은 미조치 SECOPS 건의 대상 자산이다 — 종료 건·FINOPS 건·조회 실패는 집합을 만들지 않는다', () => {
  const incidents = [
    incident('ssh', 'AWAITING_APPROVAL', 'SECOPS', 'behind'),
    incident('ssh-2', 'ANALYZING', 'SECOPS', 'behind'), // 같은 자산의 둘째 건 — 자산은 한 장이다
    incident('done', 'RESOLVED', 'SECOPS', 'open'),
    incident('cost', 'AWAITING_APPROVAL', 'FINOPS', 'safe'),
  ];
  assert.deepEqual(arns(focusAssets(FLEET, incidents, 'UNHANDLED')), ['behind']);
  assert.deepEqual(focusAssets(FLEET, null, 'UNHANDLED'), []);
});

test('기본 칸의 범위는 자산 전량이다 — 아무것도 안 걸면 초점이 없고, 리전만 걸면 그 리전 전량이다', () => {
  assert.equal(focusScope(FLEET, [], 'SG', null), null);
  assert.deepEqual(
    [...(focusScope(FLEET, [], 'SG', 'us-east-1') ?? [])],
    ['arn:test:safe'],
  );
});

test('다른 칸의 범위는 그 칸의 집합이고 리전이 그 위에 겹친다', () => {
  assert.deepEqual([...(focusScope(FLEET, [], 'AFFECTED_EC2', null) ?? [])], ['arn:test:behind']);
  // 개방 SG 뒤의 EC2는 ap-northeast-2에만 있다 — 다른 리전을 걸면 빈 집합이지 null(초점 없음)이 아니다
  assert.deepEqual([...(focusScope(FLEET, [], 'AFFECTED_EC2', 'us-east-1') ?? ['x'])], []);
});

test('상태 분포는 SECOPS만, 상태 6종을 0건까지 전부 낸다', () => {
  const counts = secopsStatusCounts([
    incident('a', 'AWAITING_APPROVAL'),
    incident('b', 'AWAITING_APPROVAL'),
    incident('c', 'FAILED'),
    incident('d', 'FAILED', 'FINOPS'),
  ]);
  assert.ok(counts !== null);
  assert.equal(counts.length, 6);
  assert.deepEqual(
    Object.fromEntries(counts.map((c) => [c.status, c.count])),
    { ANALYZING: 0, AWAITING_APPROVAL: 2, ACTION_IN_PROGRESS: 0, AWAITING_CLOSURE: 0, RESOLVED: 0, FAILED: 1 },
  );
  assert.equal(secopsStatusCounts(null), null);
});
