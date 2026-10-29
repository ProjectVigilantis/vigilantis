// 플랫폼 운영자 대시보드의 셈(2026-10-29 초안). 운영자가 여기서 알아야 할 것은 둘이다.
//   1. 수동 처리가 필요한 문제: 플랫폼 자동 기능만으로 풀리지 않아 운영자가 직접 처리해야 하는 고객 계정 문제
//   2. 어느 고객사에 어떤 관제 직원(로그인 계정)이 있는지
// 고객이 할 일(연결 대기 중 스택 생성, 승인 대기 조치)이나 다시 시도하면 풀리는 일(확인 불가)은 1에 넣지 않는다.

import { VERIFICATION_LABELS, VERIFICATION_STALE_HOURS, hoursSince } from './tenants.ts';
import type { IncidentListItem, TenantListItem, TenantVerificationResult, UserListItem } from '../types/api.ts';

/** 다시 시도로 풀리지 않는 연결 확인 결과 — 고객 계정의 역할 · 권한을 사람이 고쳐야 한다. */
const BLOCKING_RESULTS: ReadonlySet<TenantVerificationResult> = new Set(['ASSUME_DENIED', 'READ_DENIED', 'ACCOUNT_MISMATCH']);

/** ARN의 계정 칸(`arn:partition:service:region:account:resource`). 계정이 없는 ARN(S3 등)은 null이다. */
export function arnAccount(arn: string): string | null {
  const account = arn.split(':')[4];
  return account ? account : null;
}

export interface ManualIssue {
  tenantId: string;
  tenantName: string;
  /** 연결 확인이 다시 시도로 풀리지 않는 결과로 실패했으면 그 표시명. 아니면 null. */
  connection: string | null;
  /** 자동 흐름이 멈춘(진행 불가 `FAILED`) 인시던트. 최근 갱신 순. */
  stuckIncidents: IncidentListItem[];
}

/**
 * 수동 처리가 필요한 문제. 관제 중(ACTIVE) 고객만 본다 — 연결 대기는 온보딩 중이고, 정지는 더 들어가지 않는 고객이다.
 * 인시던트는 목록 계약에 고객 칸이 없어 대상 ARN의 계정 ID로 고객을 찾는다(고객 하나 = AWS 계정 하나, ADR-0012).
 * `incidents`가 null(조회 실패)이면 연결 확인 실패만 센다 — 그 사실은 화면이 따로 알린다.
 */
export function manualIssues(
  tenants: readonly TenantListItem[],
  incidents: readonly IncidentListItem[] | null,
): ManualIssue[] {
  const stuckByAccount = new Map<string, IncidentListItem[]>();
  for (const i of incidents ?? []) {
    if (i.status !== 'FAILED') continue;
    const account = arnAccount(i.subject_arn);
    if (account === null) continue;
    (stuckByAccount.get(account) ?? stuckByAccount.set(account, []).get(account)!).push(i);
  }
  const result: ManualIssue[] = [];
  for (const t of tenants) {
    if (t.status !== 'ACTIVE') continue;
    const v = t.last_verification;
    const connection = v !== null && BLOCKING_RESULTS.has(v.result) ? VERIFICATION_LABELS[v.result].label : null;
    const stuckIncidents = [...(stuckByAccount.get(t.aws_account_id) ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    if (connection === null && stuckIncidents.length === 0) continue;
    result.push({ tenantId: t.tenant_id, tenantName: t.name, connection, stuckIncidents });
  }
  // 고객 계정에 아예 못 들어가는 쪽이 먼저다. 그다음 멈춘 인시던트가 많은 순.
  return result.sort(
    (a, b) => Number(b.connection !== null) - Number(a.connection !== null) || b.stuckIncidents.length - a.stuckIncidents.length,
  );
}

export interface StaffGroup {
  tenantId: string;
  name: string;
  /** 고객사 목록에 없는 `tenant_id`의 묶음은 null이다. */
  tenantStatus: TenantListItem['status'] | null;
  users: UserListItem[];
}

const ROLE_ORDER: Record<UserListItem['role'], number> = { ADMIN: 0, OPERATOR: 1, VIEWER: 2 };

/**
 * 고객사별 고객사 직원. 등록된 고객사는 직원이 0명이어도 묶음을 만든다(아무도 없다는 것도 알아야 한다).
 * 고객사 목록에 없는 `tenant_id`의 직원은 버리지 않고 ID를 이름으로 한 묶음에 담는다.
 * 관리자 · 관리자 직원(`tenant_id` 없음)은 고객사 직원이 아니라 넣지 않는다 — 실서버 `GET /users`도 그 계정을 내려 주지 않는다.
 */
export function staffByTenant(tenants: readonly TenantListItem[], users: readonly UserListItem[]): StaffGroup[] {
  const groups = new Map<string, StaffGroup>(
    tenants.map((t) => [t.tenant_id, { tenantId: t.tenant_id, name: t.name, tenantStatus: t.status, users: [] }]),
  );
  for (const u of users) {
    if (u.tenant_id === null) continue;
    const group = groups.get(u.tenant_id) ?? { tenantId: u.tenant_id, name: u.tenant_id, tenantStatus: null, users: [] };
    group.users.push(u);
    groups.set(u.tenant_id, group);
  }
  const byRole = (a: UserListItem, b: UserListItem) => ROLE_ORDER[a.role] - ROLE_ORDER[b.role] || a.email.localeCompare(b.email);
  const list = [...groups.values()];
  for (const g of list) g.users.sort(byRole);
  return list;
}

/** 경과 시간을 사람이 읽는 말로. 1시간 안은 분, 48시간 안은 시간, 그 뒤는 일. */
export function formatAge(iso: string, nowMs: number): string {
  const hours = hoursSince(iso, nowMs);
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}분 전`;
  if (hours < 48) return `${Math.floor(hours)}시간 전`;
  return `${Math.floor(hours / 24)}일 전`;
}

/* ─────────────────────────── 고객이 많아도 높이가 고정되는 셈 ─────────────────────────── */
/* 고객 수가 늘면 고객마다 한 덩이를 쌓는 배치는 끝없이 길어진다. 아래 셈은 화면이 "분류별 건수 → 상위 몇 건 →
   전체는 스크롤되는 표"로 그리게 한 줄 단위로 편다(AWS Security Hub 요약 대시보드의 심각도 건수 · 상위 N 위젯과 같은 틀). */

export type IssueKind = 'CONNECTION' | 'STUCK';

/** 수동 처리 문제 한 건 = 표 한 줄. */
export interface IssueRow {
  key: string;
  kind: IssueKind;
  tenantId: string;
  tenantName: string;
  title: string;
  href: string;
  /** 문제가 기록된 시각(연결 확인 · 인시던트 갱신). 경과 열이 쓴다. */
  at: string;
}

/** 수동 처리 문제를 한 건 한 줄로 편다. 연결 실패가 먼저, 같은 분류 안에서는 오래된 순이다(오래 묵은 것부터 처리). */
export function issueRows(issues: readonly ManualIssue[], tenants: readonly TenantListItem[]): IssueRow[] {
  const verifiedAt = new Map(tenants.map((t) => [t.tenant_id, t.last_verification?.verified_at ?? '']));
  const rows: IssueRow[] = [];
  for (const issue of issues) {
    if (issue.connection !== null) {
      rows.push({
        key: `c:${issue.tenantId}`,
        kind: 'CONNECTION',
        tenantId: issue.tenantId,
        tenantName: issue.tenantName,
        title: `연결 확인 실패 · ${issue.connection}`,
        href: `/tenants/${encodeURIComponent(issue.tenantId)}`,
        at: verifiedAt.get(issue.tenantId) ?? '',
      });
    }
    for (const i of issue.stuckIncidents) {
      rows.push({
        key: `i:${i.incident_id}`,
        kind: 'STUCK',
        tenantId: issue.tenantId,
        tenantName: issue.tenantName,
        title: `진행 불가 · ${i.title ?? i.incident_id}`,
        href: `/incidents/${encodeURIComponent(i.incident_id)}`,
        at: i.updated_at,
      });
    }
  }
  const order: Record<IssueKind, number> = { CONNECTION: 0, STUCK: 1 };
  return rows.sort((a, b) => order[a.kind] - order[b.kind] || a.at.localeCompare(b.at));
}

/** 연결 상태 분류. 연결 상태 막대 그래프의 다섯 줄이다. */
export type ConnectionState = 'FAILED' | 'UNREACHABLE' | 'STALE' | 'OK' | 'UNCHECKED';

export const CONNECTION_STATE_ORDER: readonly ConnectionState[] = ['FAILED', 'UNREACHABLE', 'STALE', 'OK', 'UNCHECKED'];

export interface ConnectionPoint {
  tenantId: string;
  name: string;
  state: ConnectionState;
  /** 마지막 연결 확인 후 지난 시간. 확인 전이면 null. */
  hours: number | null;
  verifiedAt: string | null;
}

/**
 * 고객 연결 상태. 정지 고객은 더 들어가지 않으니 뺀다. 확인 기록이 없는 고객(연결 대기 등)은 `UNCHECKED`다.
 * 확인 불가(UNREACHABLE)는 실패와 따로 센다 — 다시 시도하면 풀릴 수 있어 수동 처리 대상이 아니다.
 */
export function connectionPoints(tenants: readonly TenantListItem[], nowMs: number): ConnectionPoint[] {
  const points: ConnectionPoint[] = [];
  for (const t of tenants) {
    if (t.status === 'SUSPENDED') continue;
    const v = t.last_verification;
    if (v === null) {
      points.push({ tenantId: t.tenant_id, name: t.name, state: 'UNCHECKED', hours: null, verifiedAt: null });
      continue;
    }
    const hours = hoursSince(v.verified_at, nowMs);
    const state: ConnectionState =
      v.result === 'UNREACHABLE' ? 'UNREACHABLE'
      : v.result !== 'OK' ? 'FAILED'
      : hours > VERIFICATION_STALE_HOURS ? 'STALE'
      : 'OK';
    points.push({ tenantId: t.tenant_id, name: t.name, state, hours, verifiedAt: v.verified_at });
  }
  return points;
}

export interface StaffRow {
  tenantId: string;
  name: string;
  tenantStatus: TenantListItem['status'] | null;
  users: UserListItem[];
  byRole: Record<UserListItem['role'], number>;
  disabled: number;
  /** 활성인데 로그인한 적이 없는 사용자 수(초대 메일을 못 받았을 수 있다). */
  neverLoggedIn: number;
  /** 이 고객사 사용자의 가장 최근 로그인. 아무도 없으면 null. */
  lastLoginAt: string | null;
}

/** 고객사별 관제 직원을 표 한 줄씩으로 센다(`staffByTenant`의 묶음 순서 그대로). */
export function staffRows(tenants: readonly TenantListItem[], users: readonly UserListItem[]): StaffRow[] {
  return staffByTenant(tenants, users).map((g) => {
    const byRole: Record<UserListItem['role'], number> = { ADMIN: 0, OPERATOR: 0, VIEWER: 0 };
    let disabled = 0;
    let neverLoggedIn = 0;
    let lastLoginAt: string | null = null;
    for (const u of g.users) {
      byRole[u.role] += 1;
      if (u.status === 'DISABLED') disabled += 1;
      else if (u.last_login_at === null) neverLoggedIn += 1;
      if (u.last_login_at !== null && (lastLoginAt === null || u.last_login_at > lastLoginAt)) lastLoginAt = u.last_login_at;
    }
    return { ...g, byRole, disabled, neverLoggedIn, lastLoginAt };
  });
}

export interface TenantIssueCount {
  tenantId: string;
  tenantName: string;
  /** 연결 실패 1건 + 진행 불가 인시던트 수. */
  total: number;
  connection: boolean;
  stuck: number;
}

/** 고객사별 수동 처리 문제 건수. 많은 순, 같으면 연결 실패가 있는 쪽이 먼저다(대시보드 막대 그래프의 줄 순서). */
export function issueCountsByTenant(issues: readonly ManualIssue[]): TenantIssueCount[] {
  return issues
    .map((i) => {
      const connection = i.connection !== null;
      const stuck = i.stuckIncidents.length;
      return { tenantId: i.tenantId, tenantName: i.tenantName, total: Number(connection) + stuck, connection, stuck };
    })
    .sort((a, b) => b.total - a.total || Number(b.connection) - Number(a.connection) || a.tenantName.localeCompare(b.tenantName));
}

export interface StaffSummary {
  total: number;
  byRole: Record<UserListItem['role'], number>;
  /** 활성이고 로그인한 적이 있는 직원. */
  loggedIn: number;
  /** 활성인데 로그인한 적이 없는 직원(초대 메일을 못 받았을 수 있다). */
  neverLoggedIn: number;
  disabled: number;
  /** 관제 중인데 직원이 한 명도 없는 고객사 수. */
  activeTenantsWithoutStaff: number;
}

/** 고객사 직원 전체를 센다. 관리자 · 관리자 직원(`tenant_id` 없음)은 넣지 않는다(`staffByTenant`와 같다). */
export function staffSummary(tenants: readonly TenantListItem[], users: readonly UserListItem[]): StaffSummary {
  const staff = users.filter((u) => u.tenant_id !== null);
  const byRole: Record<UserListItem['role'], number> = { ADMIN: 0, OPERATOR: 0, VIEWER: 0 };
  let loggedIn = 0;
  let neverLoggedIn = 0;
  let disabled = 0;
  for (const u of staff) {
    byRole[u.role] += 1;
    if (u.status === 'DISABLED') disabled += 1;
    else if (u.last_login_at === null) neverLoggedIn += 1;
    else loggedIn += 1;
  }
  const withStaff = new Set(staff.map((u) => u.tenant_id));
  const activeTenantsWithoutStaff = tenants.filter((t) => t.status === 'ACTIVE' && !withStaff.has(t.tenant_id)).length;
  return { total: staff.length, byRole, loggedIn, neverLoggedIn, disabled, activeTenantsWithoutStaff };
}

export interface StaffCountBin {
  /** 이 칸에 드는 직원 수의 범위(양 끝 포함). 칸 너비가 1이면 min === max다. */
  min: number;
  max: number;
  /** 직원 수가 이 범위인 고객사 수. */
  tenants: number;
}

export interface StaffCountDistribution {
  bins: StaffCountBin[];
  /** 센 고객사 수(관제 중). */
  n: number;
  /** 고객사당 평균 직원 수. 고객사가 없으면 null. */
  mean: number | null;
  /**
   * 가로축 범위(직원 수). 평균을 가운데에 두고 앞뒤 폭을 같게 잡는다 — 데이터가 한쪽으로 치우쳐도 평균에서 얼마나 벗어나는지가
   * 좌우 같은 눈금으로 읽힌다. 칸 하나는 [min - 0.5, max + 0.5]를 차지한다. 고객사가 없으면 null.
   */
  domain: [number, number] | null;
}

/** 칸이 이보다 많아지면 직원 수 여러 개를 한 칸으로 묶는다. */
const MAX_BINS = 10;

/**
 * 고객사마다 직원이 몇 명인지의 분포(히스토그램). 관제 중 고객사만 센다 — 연결 대기는 아직 직원을 들이는 중이고,
 * 정지는 더 쓰지 않는 고객사다. 직원 0명도 한 칸이다(직원 없음도 알아야 한다). 비활성 직원도 계정은 있으니 센다.
 */
export function staffCountDistribution(tenants: readonly TenantListItem[], users: readonly UserListItem[]): StaffCountDistribution {
  const perTenant = new Map<string, number>();
  for (const t of tenants) if (t.status === 'ACTIVE') perTenant.set(t.tenant_id, 0);
  for (const u of users) {
    if (u.tenant_id === null || !perTenant.has(u.tenant_id)) continue;
    perTenant.set(u.tenant_id, perTenant.get(u.tenant_id)! + 1);
  }
  const counts = [...perTenant.values()];
  if (counts.length === 0) return { bins: [], n: 0, mean: null, domain: null };
  const top = Math.max(...counts);
  const width = Math.max(1, Math.ceil((top + 1) / MAX_BINS));
  const bins: StaffCountBin[] = [];
  for (let min = 0; min <= top; min += width) bins.push({ min, max: min + width - 1, tenants: 0 });
  for (const c of counts) bins[Math.floor(c / width)].tenants += 1;
  const mean = counts.reduce((s, c) => s + c, 0) / counts.length;
  const half = Math.max(mean - (bins[0].min - 0.5), bins.at(-1)!.max + 0.5 - mean);
  const domain: [number, number] = [mean - half, mean + half];
  // 평균 오른쪽이 데이터보다 넓어지면 빈 칸(0곳)을 이어 붙여 눈금과 곡선이 범위 끝까지 간다. 왼쪽은 0명 아래라 칸이 없다.
  // 가운데가 범위 안에 드는 칸까지만 붙인다(끝 칸은 범위 끝에서 잘린다).
  while (bins.at(-1)!.max + 1 + (width - 1) / 2 <= domain[1]) {
    const min = bins.at(-1)!.max + 1;
    bins.push({ min, max: min + width - 1, tenants: 0 });
  }
  return { bins, n: counts.length, mean, domain };
}
