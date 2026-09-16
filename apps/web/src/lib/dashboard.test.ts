// DSH-001 집계 회귀 — `npm test`. PR #299 리뷰가 막은 표시 오류(all/null · 헬스 분모)와 같은 종류의 분모를 고정합니다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  actionQueue,
  dashboardMetrics,
  exposureRows,
  healthSummary,
  inventoryCounts,
  openPortLabel,
  verdictCounts,
} from './dashboard.ts';
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

function ec2(id: string, health: number | null, over: Partial<Common> = {}): AssetItem {
  return {
    ...common(id, { health_score: health, ...over }),
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

function incident(id: string, status: IncidentStatus): IncidentListItem {
  return {
    incident_id: id,
    title: null,
    subject_arn: 'arn:test:x',
    category: 'FINOPS',
    status,
    initial_risk_level: null,
    reviewed_risk_level: null,
    response_mode: null,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
  };
}

const SSH: OpenPortRule = { protocol: 'tcp', from_port: 22, to_port: 22, ipv6: false };

// ── 개방 규칙 표기

test('전체 트래픽 규칙(-1)은 null 포트를 찍지 않고 전체로 적는다', () => {
  const all: OpenPortRule = { protocol: 'all', from_port: null, to_port: null, ipv6: false };
  assert.equal(openPortLabel(all), '0.0.0.0/0 전체 트래픽');
  assert.doesNotMatch(openPortLabel(all), /null/);
});

test('단일 포트·범위·IPv6·포트 없는 규칙을 가른다', () => {
  assert.equal(openPortLabel(SSH), '0.0.0.0/0 tcp/22');
  assert.equal(
    openPortLabel({ protocol: 'tcp', from_port: 1024, to_port: 2048, ipv6: true }),
    '::/0 tcp/1024-2048',
  );
  assert.equal(
    openPortLabel({ protocol: 'udp', from_port: null, to_port: null, ipv6: false }),
    '0.0.0.0/0 udp/전체 포트',
  );
});

test('개방 SG의 영향 EC2는 SECURED_BY 역조인으로 센다', () => {
  const open = sg('web-sg', [SSH]);
  const rows = exposureRows([
    open,
    sg('closed-sg', []),
    ec2('a', 50, { relationships: [{ relation_type: 'SECURED_BY', target_arn: open.arn }] }),
    ec2('b', 50),
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].sg.arn, open.arn);
  assert.deepEqual(rows[0].rules, ['0.0.0.0/0 tcp/22']);
  assert.equal(rows[0].affectedEc2, 1);
});

// ── 헬스 스코어

test('확인 불가는 점수 없는 EC2만 센다 — 비EC2의 null은 해당 없음이다', () => {
  const summary = healthSummary([ec2('low', 3), ec2('unknown', null), ec2('ok', 62), sg('s', []), nacl('n')]);
  assert.equal(summary.ec2Total, 3);
  assert.equal(summary.unknown, 1);
  // 낮은 순 — 조치 대상이 위로 온다
  assert.deepEqual(
    summary.scored.map((a) => a.resource_id),
    ['low', 'ok'],
  );
});

// ── 판정 현황

test('판정 현황의 분모는 판정 대상뿐이다 — NOT_APPLICABLE의 null은 미판정이 아니다', () => {
  const counts = verdictCounts([
    ec2('t', 1, { verdict: 'THREAT' }),
    sg('pending', [], { evaluation_status: 'PENDING' }),
    nacl('n1'),
    nacl('n2'),
  ]);
  assert.equal(counts.judged, 2);
  assert.equal(counts.pending, 1);
  assert.equal(counts.byVerdict.find((v) => v.verdict === 'THREAT')?.count, 1);
  assert.equal(counts.undecidable, 1);
});

// ── 인벤토리·지표

test('인벤토리는 0건 유형까지 7종 전부 낸다', () => {
  const inv = inventoryCounts([ec2('a', 1), sg('s', [])]);
  assert.equal(inv.length, 7);
  assert.equal(inv.find((r) => r.type === 'EC2')?.count, 1);
  assert.equal(inv.find((r) => r.type === 'ALB_TARGET_GROUP')?.count, 0);
});

test('미조치는 분석·승인 대기·조치 중 3종이고, 인시던트 조회 실패는 0이 아니라 null이다', () => {
  const statuses: IncidentStatus[] = [
    'ANALYZING',
    'AWAITING_APPROVAL',
    'ACTION_IN_PROGRESS',
    'AWAITING_CLOSURE',
    'FAILED',
    'RESOLVED',
  ];
  const items = [ec2('a', 3, { verdict: 'COST_CANDIDATE' }), sg('s', [SSH], { verdict: 'THREAT' })];
  const metrics = dashboardMetrics(
    items,
    statuses.map((s, i) => incident(`i${i}`, s)),
  );
  assert.equal(metrics.unhandled, 3);
  assert.equal(metrics.openSg, 1);
  assert.equal(metrics.threat, 1);
  assert.equal(metrics.waste, 1);
  assert.equal(dashboardMetrics(items, null).unhandled, null);
});

// ── AI 조치 제안 카드의 큐(§4.1) ──────────────────────────────────────────────

test('조치 큐는 미조치 지표와 같은 집합이다 — 한 화면이 같은 것을 두 숫자로 말하지 않는다', () => {
  const items = [
    incident('resolved', 'RESOLVED'),
    incident('analyzing', 'ANALYZING'),
    incident('closure', 'AWAITING_CLOSURE'),
    incident('pending', 'AWAITING_APPROVAL'),
    incident('failed', 'FAILED'),
    incident('running', 'ACTION_IN_PROGRESS'),
  ];
  const queue = actionQueue(items);
  assert.deepEqual(
    queue.map((i) => i.incident_id).sort(),
    ['analyzing', 'pending', 'running'],
  );
  assert.equal(queue.length, dashboardMetrics([], items).unhandled);
});

test('큐는 위험도 순, 동점은 오래 기다린 건이 먼저다 — INC-001과 같은 셀렉터다', () => {
  const at = (id: string, risk: 'HIGH' | 'LOW' | null, created: string): IncidentListItem => ({
    ...incident(id, 'AWAITING_APPROVAL'),
    // FINOPS는 계약이 위험도를 null로 강제하므로 위험도가 붙는 건은 SECOPS다.
    ...(risk === null ? {} : { category: 'SECOPS' as const, initial_risk_level: risk }),
    created_at: created,
  });
  const queue = actionQueue([
    at('finops-old', null, '2026-09-01T00:00:00Z'),
    at('low', 'LOW', '2026-09-01T00:00:00Z'),
    at('high-late', 'HIGH', '2026-09-02T00:00:00Z'),
    at('high-early', 'HIGH', '2026-09-01T00:00:00Z'),
  ]);
  assert.deepEqual(queue.map((i) => i.incident_id), [
    'high-early',
    'high-late',
    'low',
    'finops-old',
  ]);
});

test('인시던트 조회 실패(null)면 큐가 비어 카드가 대기 0건으로 그린다', () => {
  assert.deepEqual(actionQueue(null), []);
});
