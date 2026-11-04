import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { AssetItem, IncidentListItem, ThreatEventAxis } from '../types/api.ts';
import { byPreset, parsePreset } from './incident-filter.ts';
import {
  assetTypeRows,
  awaitingLinks,
  coverageCounts,
  doneNote,
  postureOf,
  recentDone,
  savingsTotal,
  threatHours,
  timeAgo,
} from './customer-dashboard.ts';

const NOW = Date.parse('2026-10-20T05:30:00Z');

function incident(over: Partial<IncidentListItem> & Pick<IncidentListItem, 'incident_id'>): IncidentListItem {
  return {
    title: null,
    subject_arn: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-1',
    category: 'SECOPS',
    status: 'AWAITING_APPROVAL',
    initial_risk_level: 'MEDIUM',
    reviewed_risk_level: null,
    response_mode: null,
    threat_context: null,
    analysis_result: null,
    created_at: '2026-10-20T05:00:00Z',
    updated_at: '2026-10-20T05:00:00Z',
    ...over,
  };
}

test('위험도 높음 보안 건이 있으면 위험, 보안 건만 있으면 주의다', () => {
  const high = postureOf({ queue: [incident({ incident_id: 'a', initial_risk_level: 'HIGH' })], threatAssets: 1, collection: 'READY' });
  assert.equal(high.level, 'critical');
  assert.match(high.headline, /1건/);
  const medium = postureOf({ queue: [incident({ incident_id: 'b' })], threatAssets: 0, collection: 'READY' });
  assert.equal(medium.level, 'warning');
});

test('비용 건만 있으면 상태를 올리지 않는다', () => {
  const p = postureOf({
    queue: [incident({ incident_id: 'c', category: 'FINOPS', initial_risk_level: null })],
    threatAssets: 0,
    collection: 'READY',
  });
  assert.equal(p.level, 'good');
  assert.match(p.detail, /비용 절감 제안 1건/);
});

test('인시던트를 못 읽으면 정상이라 하지 않는다', () => {
  assert.equal(postureOf({ queue: null, threatAssets: 0, collection: 'READY' }).level, 'unknown');
});

test('수집 일부 실패는 주의다', () => {
  assert.equal(postureOf({ queue: [], threatAssets: 0, collection: 'PARTIAL' }).level, 'warning');
});

test('수집 전 · 수집 중에는 처리할 건과 위협 자산이 0이어도 정상이라 하지 않는다', () => {
  for (const collection of ['NOT_COLLECTED', 'COLLECTING'] as const) {
    const p = postureOf({ queue: [], threatAssets: 0, collection });
    assert.equal(p.level, 'unknown', collection);
    assert.doesNotMatch(p.headline, /정상/, collection);
  }
  assert.match(postureOf({ queue: [], threatAssets: 0, collection: 'NOT_COLLECTED' }).headline, /수집하지 않았습니다/);
  assert.match(postureOf({ queue: [], threatAssets: 0, collection: 'COLLECTING' }).headline, /수집하는 중/);
});

test('수집 중이어도 보안 건은 그대로 알린다', () => {
  const p = postureOf({ queue: [incident({ incident_id: 'h', initial_risk_level: 'HIGH' })], threatAssets: 0, collection: 'COLLECTING' });
  assert.equal(p.level, 'critical');
});

test('절감액은 추정 있는 비용 제안만 더하고 추정 없음은 따로 센다', () => {
  const rows = [
    { incident: incident({ incident_id: 'f1', category: 'FINOPS' }), savings: 62.05 },
    { incident: incident({ incident_id: 'f2', category: 'FINOPS' }), savings: null },
    { incident: incident({ incident_id: 's1' }), savings: null },
  ];
  assert.deepEqual(savingsTotal(rows), { amount: 62.05, counted: 1, unknown: 1 });
});

test('위협 이벤트는 24칸으로 펴고 빈 칸은 0이다', () => {
  const axis: ThreatEventAxis = {
    status: 'READY',
    reason_code: null,
    points: [
      { at: '2026-10-20T05:10:00Z', total: 2, counts: { SSH_BRUTE_FORCE: 2 } },
      { at: '2026-10-19T07:00:00Z', total: 1, counts: { OPEN_IP: 1 } },
      { at: '2026-10-18T00:00:00Z', total: 9, counts: { OPEN_IP: 9 } },
    ],
  };
  const hours = threatHours(axis, '2026-10-20T05:30:00Z');
  assert.ok(hours);
  assert.equal(hours.length, 24);
  assert.equal(hours[23].at, '2026-10-20T05:00:00.000Z');
  assert.equal(hours[23].total, 2);
  assert.equal(hours[1].total, 1);
  assert.equal(hours.reduce((s, h) => s + h.total, 0), 3);
});

test('축을 못 읽었으면 0건 그래프가 아니라 null이다', () => {
  assert.equal(threatHours({ status: 'UNAVAILABLE', reason_code: 'X', points: [] }, '2026-10-20T05:30:00Z'), null);
});

test('최근 끝난 건은 7일 안의 종료 · 종료 대기만 최신순으로', () => {
  const list = [
    incident({ incident_id: 'old', status: 'RESOLVED', updated_at: '2026-10-10T00:00:00Z' }),
    incident({ incident_id: 'r1', status: 'RESOLVED', updated_at: '2026-10-19T00:00:00Z' }),
    incident({ incident_id: 'c1', status: 'AWAITING_CLOSURE', updated_at: '2026-10-20T03:00:00Z' }),
    incident({ incident_id: 'open', status: 'AWAITING_APPROVAL', updated_at: '2026-10-20T04:00:00Z' }),
  ];
  assert.deepEqual(recentDone(list, NOW)?.map((i) => i.incident_id), ['c1', 'r1']);
});

test('자동 격리가 거절된 뒤 「추가 조치 없음」으로 닫힌 건을 격리 성공처럼 적지 않는다', () => {
  // core-api test_rejected_timeout_closes_the_wait_without_retry: 시간 초과 자동 격리가 가드레일에 거절돼
  // FAILED가 되고, 관제자가 NO_FURTHER_ACTION으로 닫는다. 목록에는 RESOLVED · TIMEOUT_ISOLATION_1M만 남는다.
  const closed = incident({ incident_id: 'rej', status: 'RESOLVED', response_mode: 'TIMEOUT_ISOLATION_1M' });
  assert.equal(doneNote(closed), '종료 · 자동 격리 시도');
  assert.equal(doneNote({ status: 'RESOLVED', response_mode: 'PRE_MITIGATION_0_5S' }), '종료 · 선제 차단 시도');
});

test('사람이 승인한 경로는 상태만 적는다', () => {
  assert.equal(doneNote({ status: 'AWAITING_CLOSURE', response_mode: 'AGENT_WAIT' }), '종료 판단 대기');
  assert.equal(doneNote({ status: 'RESOLVED', response_mode: null }), '종료');
});

test('판정 대기 · 실패는 정상 · 제외에 넣지 않고 합계는 판정 대상 수와 같다', () => {
  const items = [
    { evaluation_status: 'COMPLETED', verdict: 'THREAT' },
    { evaluation_status: 'COMPLETED', verdict: 'COST_CANDIDATE' },
    { evaluation_status: 'COMPLETED', verdict: 'UNUSED' },
    { evaluation_status: 'COMPLETED', verdict: 'SKIP' },
    { evaluation_status: 'PENDING', verdict: null },
    { evaluation_status: 'PENDING', verdict: null },
    { evaluation_status: 'FAILED', verdict: null },
    { evaluation_status: 'NOT_APPLICABLE', verdict: null },
  ] as const;
  const c = coverageCounts(items);
  assert.deepEqual(c, { judged: 7, threat: 1, waste: 2, ok: 1, pending: 2, failed: 1 });
  assert.equal(c.threat + c.waste + c.ok + c.pending + c.failed, c.judged);
});

test('상대 시각', () => {
  assert.equal(timeAgo('2026-10-20T05:12:00Z', NOW), '18분 전');
  assert.equal(timeAgo('2026-10-20T02:30:00Z', NOW), '3시간 전');
  assert.equal(timeAgo('2026-10-18T05:30:00Z', NOW), '2일 전');
});

test('자산 현황: 유형 순서대로, 없는 유형은 빼고 수집 실패 유형은 남긴다', () => {
  const base = { resource_role: 'PRIMARY', name: null, account_id: '1', region: 'r', relationships: [], evaluation_status: 'COMPLETED', health_score: null, verdict: null, skip_reason_code: null } as const;
  const items = [
    { ...base, arn: 'v1', resource_id: 'v1', state: 'in-use', asset_type: 'EBS', collected_at: '2026-11-03T01:00:00Z', spec: { volume_type: 'gp3', size_gib: 100, availability_zone: null, encrypted: true, attached_instance_ids: [] } },
    { ...base, arn: 'i1', resource_id: 'i1', state: 'running', asset_type: 'EC2', collected_at: '2026-11-03T02:00:00Z', spec: { instance_type: null, availability_zone: null, vpc_id: null, subnet_id: null, private_ip: null, tags: {} } },
    { ...base, arn: 'i2', resource_id: 'i2', state: 'stopped', asset_type: 'EC2', collected_at: '2026-11-03T03:00:00Z', spec: { instance_type: null, availability_zone: null, vpc_id: null, subnet_id: null, private_ip: null, tags: {} } },
  ] as unknown as AssetItem[];
  const rows = assetTypeRows(items, [{ asset_type: 'NACL', reason_code: 'AccessDenied' }]);
  assert.deepEqual(rows.map((r) => r.type), ['EC2', 'EBS', 'NACL']);
  assert.equal(rows[0].detail, '실행 1 · 중지 1');
  assert.equal(rows[0].lastCollectedAt, '2026-11-03T03:00:00Z');
  assert.equal(rows[1].detail, '합계 100 GiB · 미연결 1');
  assert.deepEqual([rows[2].count, rows[2].uncollectedReason, rows[2].lastCollectedAt], [0, 'AccessDenied', null]);
});


// PR #578 리뷰: 카드가 센 승인 대기는 이동한 목록에 전부 있어야 한다. 이동한 주소를 관제 화면과 같은 규칙(축 → 프리셋)으로 다시 걸러 본다.
function destination(href: string, queue: IncidentListItem[]): IncidentListItem[] {
  const params = new URL(href, 'http://x').searchParams;
  const category = params.get('axis') === 'security' ? 'SECOPS' : 'FINOPS';
  return byPreset(queue.filter((i) => i.category === category), parsePreset(params.get('preset') ?? undefined));
}

test('AI 추천 조치: 축마다 목록 하나, 센 건은 그 목록에 전부 있다', () => {
  const sec = incident({ incident_id: 's' });
  const fin = incident({ incident_id: 'f', category: 'FINOPS', initial_risk_level: null });
  const pre = incident({ incident_id: 'p', response_mode: 'PRE_MITIGATION_0_5S' });
  const analyzing = incident({ incident_id: 'a', status: 'ANALYZING' });
  const cases: [string, IncidentListItem[], Record<string, string>][] = [
    // 보안 1 + 자산 1: 예전에는 카드 2건이 보안 목록 1건으로 줄었다.
    ['보안 + 자산', [sec, fin, analyzing], { security: 'pending', assets: 'pending' }],
    // 선제차단 뒤 추가 승인만: 예전에는 카드 1건인데 승인 대기 목록은 0건이었다.
    ['선제차단 뒤 승인만', [pre], { security: 'preemptive' }],
    ['보통 승인 + 선제차단 뒤 승인', [sec, pre, analyzing], { security: '' }],
    ['없음', [analyzing], {}],
  ];
  for (const [name, queue, expected] of cases) {
    const links = awaitingLinks(queue);
    assert.deepEqual(
      Object.fromEntries(links.map((l) => [l.axis, new URL(l.href, 'http://x').searchParams.get('preset') ?? ''])),
      expected,
      name,
    );
    for (const link of links) {
      const counted = queue.filter(
        (i) => i.status === 'AWAITING_APPROVAL' && i.category === (link.axis === 'security' ? 'SECOPS' : 'FINOPS'),
      );
      assert.equal(link.count, counted.length, name);
      const shown = new Set(destination(link.href, queue).map((i) => i.incident_id));
      for (const i of counted) assert.ok(shown.has(i.incident_id), `${name}: ${i.incident_id}`);
    }
  }
});
