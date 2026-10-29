import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { IncidentListItem, TenantListItem, TenantVerificationResult, UserListItem } from '../types/api.ts';
import {
  arnAccount,
  connectionPoints,
  formatAge,
  issueCountsByTenant,
  issueRows,
  manualIssues,
  staffByTenant,
  staffRows,
  staffCountDistribution,
  staffSummary,
} from './admin-dashboard.ts';

const NOW = Date.parse('2026-10-29T12:00:00Z');

function tenant(id: string, account: string, status: TenantListItem['status'], extra: Partial<TenantListItem> = {}): TenantListItem {
  return {
    tenant_id: id, name: `고객 ${id}`, status, aws_account_id: account,
    last_verification: null, asset_count: 0, open_incident_count: 0, ...extra,
  };
}

const verified = (result: TenantVerificationResult) => ({ result, verified_at: '2026-10-29T11:00:00Z', reason_code: null });

function incident(id: string, account: string, status: IncidentListItem['status'], updated = '2026-10-29T10:00:00Z'): IncidentListItem {
  return {
    incident_id: id, title: id, subject_arn: `arn:aws:ec2:ap-northeast-2:${account}:instance/i-${id}`,
    category: 'FINOPS', status, initial_risk_level: null, reviewed_risk_level: null, response_mode: null,
    threat_context: null, analysis_result: null, created_at: updated, updated_at: updated,
  };
}

function user(id: string, tenantId: string | null, role: UserListItem['role'] = 'VIEWER'): UserListItem {
  return {
    account_type: tenantId === null ? 'PLATFORM' : 'TENANT', tenant_id: tenantId, user_id: id,
    email: `${id}@example.com`, role, status: 'ACTIVE', last_login_at: null,
  } as UserListItem;
}

test('ARN의 계정 칸을 읽고, 계정이 없는 ARN은 null이다', () => {
  assert.equal(arnAccount('arn:aws:ec2:ap-northeast-2:111122223333:instance/i-1'), '111122223333');
  assert.equal(arnAccount('arn:aws:s3:::bucket'), null);
});

test('다시 시도로 풀리지 않는 연결 실패와 진행 불가 인시던트만 수동 처리 대상이다', () => {
  const result = manualIssues(
    [
      tenant('ok', '111111111111', 'ACTIVE', { last_verification: verified('OK') }),
      tenant('unreach', '222222222222', 'ACTIVE', { last_verification: verified('UNREACHABLE') }),
      tenant('denied', '333333333333', 'ACTIVE', { last_verification: verified('ASSUME_DENIED') }),
      tenant('stuck', '444444444444', 'ACTIVE', { last_verification: verified('OK') }),
    ],
    [
      incident('a', '111111111111', 'AWAITING_APPROVAL'),
      incident('b', '444444444444', 'FAILED', '2026-10-29T08:00:00Z'),
      incident('c', '444444444444', 'FAILED', '2026-10-29T09:00:00Z'),
    ],
  );
  assert.deepEqual(result.map((r) => r.tenantId), ['denied', 'stuck']);
  assert.equal(result[0].connection, '역할 접근 거절');
  assert.deepEqual(result[1].stuckIncidents.map((i) => i.incident_id), ['c', 'b']);
});

test('연결 대기 · 정지 고객은 수동 처리 대상에서 빠진다', () => {
  const result = manualIssues(
    [
      tenant('pending', '111111111111', 'PENDING_CONNECTION', { last_verification: verified('ASSUME_DENIED') }),
      tenant('susp', '222222222222', 'SUSPENDED'),
    ],
    [incident('x', '222222222222', 'FAILED')],
  );
  assert.deepEqual(result, []);
});

test('인시던트 조회 실패(null)면 연결 실패만 센다', () => {
  const result = manualIssues([tenant('d', '1', 'ACTIVE', { last_verification: verified('READ_DENIED') })], null);
  assert.deepEqual(result.map((r) => [r.tenantId, r.stuckIncidents.length]), [['d', 0]]);
});

test('고객사 직원은 고객사마다 묶고(0명 고객사 포함) 권한 순으로, 관리자 계정(tenant_id 없음)은 넣지 않는다', () => {
  const groups = staffByTenant(
    [tenant('a', '1', 'ACTIVE'), tenant('b', '2', 'PENDING_CONNECTION')],
    [user('v', 'a', 'VIEWER'), user('ad', 'a', 'ADMIN'), user('p', null, 'ADMIN'), user('x', 'gone')],
  );
  assert.deepEqual(
    groups.map((g) => [g.name, g.users.map((u) => u.user_id)]),
    [['고객 a', ['ad', 'v']], ['고객 b', []], ['gone', ['x']]],
  );
});

test('경과 시간은 분 · 시간 · 일로 읽는다', () => {
  assert.equal(formatAge('2026-10-29T11:50:00Z', NOW), '10분 전');
  assert.equal(formatAge('2026-10-29T02:00:00Z', NOW), '10시간 전');
  assert.equal(formatAge('2026-10-26T12:00:00Z', NOW), '3일 전');
});

test('수동 처리 문제는 한 건 한 줄로 펴고, 연결 실패 먼저 · 같은 분류는 오래된 순이다', () => {
  const tenants = [
    tenant('denied', '333333333333', 'ACTIVE', { last_verification: verified('ASSUME_DENIED') }),
    tenant('stuck', '444444444444', 'ACTIVE', { last_verification: verified('OK') }),
  ];
  const rows = issueRows(
    manualIssues(tenants, [
      incident('new', '444444444444', 'FAILED', '2026-10-29T09:00:00Z'),
      incident('old', '444444444444', 'FAILED', '2026-10-28T09:00:00Z'),
    ]),
    tenants,
  );
  assert.deepEqual(rows.map((r) => r.key), ['c:denied', 'i:old', 'i:new']);
  assert.equal(rows[1].href, '/incidents/old');
});

test('연결 상태는 다섯 분류이고 정지 고객은 뺀다', () => {
  const points = connectionPoints(
    [
      tenant('ok', '1', 'ACTIVE', { last_verification: verified('OK') }),
      tenant('stale', '2', 'ACTIVE', { last_verification: { result: 'OK', verified_at: '2026-10-27T00:00:00Z', reason_code: null } }),
      tenant('fail', '3', 'ACTIVE', { last_verification: verified('READ_DENIED') }),
      tenant('unreach', '4', 'ACTIVE', { last_verification: verified('UNREACHABLE') }),
      tenant('pending', '5', 'PENDING_CONNECTION'),
      tenant('susp', '6', 'SUSPENDED', { last_verification: verified('OK') }),
    ],
    NOW,
  );
  assert.deepEqual(
    points.map((p) => [p.tenantId, p.state]),
    [['ok', 'OK'], ['stale', 'STALE'], ['fail', 'FAILED'], ['unreach', 'UNREACHABLE'], ['pending', 'UNCHECKED']],
  );
  assert.equal(points[4].hours, null);
});

test('관제 직원 표 한 줄은 역할별 · 비활성 · 미로그인 수와 가장 최근 로그인을 센다', () => {
  const [row] = staffRows(
    [tenant('a', '1', 'ACTIVE')],
    [
      { ...user('ad', 'a', 'ADMIN'), last_login_at: '2026-10-29T08:00:00Z' },
      { ...user('op', 'a', 'OPERATOR'), last_login_at: '2026-10-29T10:00:00Z' },
      user('v1', 'a'),
      { ...user('v2', 'a'), status: 'DISABLED' },
    ],
  );
  assert.deepEqual(row.byRole, { ADMIN: 1, OPERATOR: 1, VIEWER: 2 });
  assert.equal(row.disabled, 1);
  assert.equal(row.neverLoggedIn, 1);
  assert.equal(row.lastLoginAt, '2026-10-29T10:00:00Z');
});

test('고객사별 문제 건수는 많은 순이고, 같으면 연결 실패가 있는 고객사가 먼저다', () => {
  const counts = issueCountsByTenant([
    { tenantId: 'a', tenantName: '가', connection: null, stuckIncidents: [incident('1', '1', 'FAILED')] },
    { tenantId: 'b', tenantName: '나', connection: '권한 거부', stuckIncidents: [] },
    { tenantId: 'c', tenantName: '다', connection: '권한 거부', stuckIncidents: [incident('2', '3', 'FAILED'), incident('3', '3', 'FAILED')] },
  ]);
  assert.deepEqual(counts.map((c) => [c.tenantId, c.total]), [['c', 3], ['b', 1], ['a', 1]]);
});

test('고객사 직원 요약은 관리자 계정을 빼고 세며, 직원 없는 관제 중 고객사를 센다', () => {
  const s = staffSummary(
    [tenant('t1', '1', 'ACTIVE'), tenant('t2', '2', 'ACTIVE'), tenant('t3', '3', 'PENDING_CONNECTION')],
    [
      { ...user('a', 't1', 'ADMIN'), last_login_at: '2026-10-29T10:00:00Z' },
      user('b', 't1', 'OPERATOR'),
      { ...user('c', 't1'), status: 'DISABLED' },
      user('p', null, 'ADMIN'),
    ],
  );
  assert.equal(s.total, 3);
  assert.deepEqual(s.byRole, { ADMIN: 1, OPERATOR: 1, VIEWER: 1 });
  assert.deepEqual([s.loggedIn, s.neverLoggedIn, s.disabled], [1, 1, 1]);
  assert.equal(s.activeTenantsWithoutStaff, 1);
});

test('직원 수 분포는 관제 중 고객사만 세고, 0명도 한 칸이며, 칸이 많으면 묶는다', () => {
  const tenants = [tenant('a', '1', 'ACTIVE'), tenant('b', '2', 'ACTIVE'), tenant('c', '3', 'ACTIVE'), tenant('p', '4', 'PENDING_CONNECTION')];
  const users = [user('1', 'a'), user('2', 'a'), user('3', 'b'), user('4', 'p'), user('5', null)];
  const d = staffCountDistribution(tenants, users);
  assert.deepEqual(d.bins.map((b) => [b.min, b.max, b.tenants]), [[0, 0, 1], [1, 1, 1], [2, 2, 1]]);
  assert.equal(d.n, 3);
  assert.equal(d.mean, 1);

  const many = Array.from({ length: 25 }, (_, i) => user(`m${i}`, 'a'));
  const wide = staffCountDistribution([tenant('a', '1', 'ACTIVE'), tenant('b', '2', 'ACTIVE')], many);
  assert.equal(wide.bins.length <= 10, true);
  assert.equal(wide.bins[0].tenants, 1);
  assert.equal(wide.bins.at(-1)!.tenants, 1);

  assert.deepEqual(staffCountDistribution([], []), { bins: [], n: 0, mean: null, domain: null });
});

test('직원 수 분포의 가로축은 평균을 가운데에 두고 앞뒤 폭이 같다', () => {
  // 직원 0 · 0 · 0 · 4명 → 평균 1. 데이터 끝 4.5까지 3.5라 범위는 [-2.5, 4.5].
  const tenants = ['a', 'b', 'c', 'd'].map((id, i) => tenant(id, String(i), 'ACTIVE'));
  const users = Array.from({ length: 4 }, (_, i) => user(`u${i}`, 'd'));
  const d = staffCountDistribution(tenants, users);
  assert.deepEqual(d.domain, [-2.5, 4.5]);
  assert.equal(d.mean! - d.domain![0], d.domain![1] - d.mean!);

  // 직원 4 · 4 · 4 · 0명 → 평균 3. 왼쪽 끝 -0.5까지 3.5라 오른쪽을 6.5까지 늘리고 5 · 6명 빈 칸을 붙인다.
  const users2 = ['a', 'b', 'c'].flatMap((id) => Array.from({ length: 4 }, (_, i) => user(`${id}${i}`, id)));
  const d2 = staffCountDistribution(tenants, users2);
  assert.deepEqual(d2.domain, [-0.5, 6.5]);
  assert.deepEqual(d2.bins.map((b) => [b.min, b.tenants]), [[0, 1], [1, 0], [2, 0], [3, 0], [4, 3], [5, 0], [6, 0]]);
});
