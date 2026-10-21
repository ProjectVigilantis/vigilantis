// 고객(테넌트) 관리 화면(Post-MVP T-4)의 표시 사전 · 동작 가능 여부 · 입력 검사.
// 원천은 packages/schemas/api/tenants.py(PR #489)다. 상태 관문의 최종 판정은 서버가 하고(409
// TENANT_STATE_CONFLICT), 여기서는 서버가 받지 않을 요청의 버튼을 미리 막는 데만 쓴다.

import type { EnumLabel } from './enum-labels.ts';
import {
  TENANT_DELETABLE_STATUSES,
  TENANT_VERIFIABLE_STATUSES,
  type ErrorCode,
  type TenantCreateRequest,
  type TenantListItem,
  type TenantStatus,
  type TenantVerificationResult,
} from '../types/api.ts';

export const TENANT_STATUS_LABELS: Record<TenantStatus, EnumLabel> = {
  PENDING_CONNECTION: { label: '연결 대기', tone: 'yellow' },
  ACTIVE: { label: '관제 중', tone: 'green' },
  // 원격 회수. 고객 계정의 역할을 지운 것이 아니라 플랫폼이 더 들어가지 않는 상태다.
  // 관리자가 일부러 내린 결정이라 빨강(실패)이 아니라 중립 회색이다(DESIGN.md §상태 색).
  SUSPENDED: { label: '정지', tone: 'gray' },
};

/** 연결 확인 결과: 확인한 시각의 관측값이다. `hint`는 관리자가 다음에 할 일이다. */
export const VERIFICATION_LABELS: Record<TenantVerificationResult, EnumLabel & { hint: string | null }> = {
  OK: { label: '연결 확인됨', tone: 'green', hint: null },
  ASSUME_DENIED: {
    label: '역할 접근 거절',
    tone: 'red',
    hint: '고객 계정에 역할이 없거나, 신뢰 정책이 플랫폼 또는 ExternalId를 허용하지 않습니다. 온보딩 링크로 스택을 다시 만들게 안내하세요.',
  },
  ACCOUNT_MISMATCH: {
    label: '계정 불일치',
    tone: 'red',
    hint: '역할은 열렸지만 다른 계정입니다. 계정 ID를 잘못 넣었다면 이 고객을 지우고 새로 만드세요.',
  },
  READ_DENIED: {
    label: '읽기 권한 없음',
    tone: 'orange',
    hint: '역할에 권한 정책이 붙지 않았습니다. 고객에게 스택의 권한 정책을 확인하게 하세요.',
  },
  UNREACHABLE: {
    label: '확인 불가',
    tone: 'gray',
    hint: 'AWS에 묻지 못했습니다(네트워크 · 요청 제한). 연결 여부와 무관하니 잠시 뒤 다시 확인하세요.',
  },
};

export interface TenantActions {
  verify: boolean;
  suspend: boolean;
  resume: boolean;
  remove: boolean;
}

/**
 * 상태별로 열리는 동작. 정지 중 고객 계정에 들어가는 길은 resume 하나다(verify 불가).
 * 정지는 관제 중(ACTIVE)에서만 연다. 연결 대기는 들어간 적이 없어 회수할 것이 없다.
 */
export function tenantActions(status: TenantStatus): TenantActions {
  return {
    verify: TENANT_VERIFIABLE_STATUSES.includes(status),
    suspend: status === 'ACTIVE',
    resume: status === 'SUSPENDED',
    remove: TENANT_DELETABLE_STATUSES.includes(status),
  };
}

/**
 * 요청이 실패한 이유가 "그 사이 고객 상태가 바뀌어 이 요청이 더 이상 맞지 않는다"인지. 상태 충돌 · 이미 정지 ·
 * 없는 고객이 그렇다. 이때는 확인창을 닫고 화면을 다시 읽어 지금 가능한 동작을 보인다(#503 리뷰). 그 밖의 실패
 * (네트워크 · 일시 오류)는 입력한 정지 사유를 잃지 않게 확인창을 남기고 그 안에 오류를 보인다.
 */
const STALE_REQUEST_CODES: readonly ErrorCode[] = ['TENANT_STATE_CONFLICT', 'TENANT_SUSPENDED', 'TENANT_NOT_FOUND'];

export function isStaleTenantRequest(code: ErrorCode | null): boolean {
  return code !== null && STALE_REQUEST_CODES.includes(code);
}

/* ─────────────────────────── 목록 요약(그래프) ─────────────────────────── */
/* 전부 GET /tenants 한 번의 응답에서 계산한다. 시계열은 계약에 없어 여기서 만들지 않는다. */

/**
 * 연결 확인이 이보다 오래됐으면 낡은 것으로 본다. 확인 주기가 아직 정해지지 않아 둔 제안값이다 —
 * 주기가 정해지면 그 값으로 맞춘다.
 */
export const VERIFICATION_STALE_HOURS = 24;

export interface TenantSummary {
  total: number;
  byStatus: Record<TenantStatus, number>;
  /** 관제 중 고객의 자산 합. 정지 고객의 자산은 지난 기록이라 세지 않는다. */
  activeAssets: number;
  openIncidents: number;
  /** 마지막 연결 확인이 OK가 아닌 고객 수와 그 내역. */
  verificationProblems: { failed: number; unreachable: number };
}

export function summarizeTenants(tenants: readonly TenantListItem[]): TenantSummary {
  const byStatus: Record<TenantStatus, number> = { PENDING_CONNECTION: 0, ACTIVE: 0, SUSPENDED: 0 };
  let activeAssets = 0;
  let openIncidents = 0;
  let failed = 0;
  let unreachable = 0;
  for (const t of tenants) {
    byStatus[t.status] += 1;
    if (t.status === 'ACTIVE') activeAssets += t.asset_count;
    openIncidents += t.open_incident_count;
    const result = t.last_verification?.result;
    if (result === 'UNREACHABLE') unreachable += 1;
    else if (result !== undefined && result !== 'OK') failed += 1;
  }
  return {
    total: tenants.length,
    byStatus,
    activeAssets,
    openIncidents,
    verificationProblems: { failed, unreachable },
  };
}

/** 확인 시각부터 `nowMs`까지 지난 시간(시간 단위). 시계가 어긋나 음수가 나오면 0이다. */
export function hoursSince(iso: string, nowMs: number): number {
  return Math.max(0, (nowMs - Date.parse(iso)) / 3_600_000);
}

export interface VerificationAge {
  tenantId: string;
  name: string;
  result: TenantVerificationResult;
  verifiedAt: string;
  hours: number;
}

/** 연결 확인 기록이 있는 고객만, 최근에 확인한 순서로. */
export function verificationAges(tenants: readonly TenantListItem[], nowMs: number): VerificationAge[] {
  const ages: VerificationAge[] = [];
  for (const t of tenants) {
    const v = t.last_verification;
    if (v === null) continue;
    ages.push({ tenantId: t.tenant_id, name: t.name, result: v.result, verifiedAt: v.verified_at, hours: hoursSince(v.verified_at, nowMs) });
  }
  return ages.sort((a, b) => a.hours - b.hours);
}

/* ─────────────────────────── 입력 검사 ─────────────────────────── */
/* 패턴은 tenants.py의 Annotated 타입과 같은 값이다. 서버가 422로 거절하기 전에 칸 옆에 알린다. */

const ACCOUNT_ID = /^\d{12}$/;
const REGION = /^[a-z]{2}(-[a-z]+)+-\d+$/;
const VPC_ID = /^vpc-[0-9a-f]+$/;
// IAM 역할 이름은 ASCII만 받는다(tenants.py RoleArn).
const ROLE_ARN = /^arn:aws:iam::(\d{12}):role\/[A-Za-z0-9_+=,.@/-]+$/;
export const TENANT_NAME_MAX = 100;
export const SUSPEND_REASON_MAX = 500;

/** 쉼표 · 공백 · 줄바꿈으로 나눈 목록. 빈 칸은 버린다. */
export function splitList(raw: string): string[] {
  return raw.split(/[\s,]+/).filter((v) => v !== '');
}

export interface TenantForm {
  name: string;
  awsAccountId: string;
  /** 비우면 서버가 `vigilantis-access` 기본 역할로 채운다. */
  accessRoleArn: string;
  regions: string;
  collectVpcIds: string;
}

export type TenantFormErrors = Partial<Record<keyof TenantForm, string>>;

export type TenantFormResult =
  | { ok: true; request: TenantCreateRequest }
  | { ok: false; errors: TenantFormErrors };

function duplicate(values: string[]): string | null {
  const seen = new Set<string>();
  for (const v of values) {
    if (seen.has(v)) return v;
    seen.add(v);
  }
  return null;
}

export function parseRegions(raw: string): { values: string[]; error: string | null } {
  const values = splitList(raw);
  if (values.length === 0) return { values, error: '리전을 하나 이상 넣으세요.' };
  const bad = values.find((v) => !REGION.test(v));
  if (bad !== undefined) return { values, error: `리전 형식이 아닙니다: ${bad}` };
  const dup = duplicate(values);
  if (dup !== null) return { values, error: `같은 리전이 두 번 있습니다: ${dup}` };
  return { values, error: null };
}

/** 빈 목록은 "계정 안 VPC 전체"라 오류가 아니다. */
export function parseVpcIds(raw: string): { values: string[]; error: string | null } {
  const values = splitList(raw);
  const bad = values.find((v) => !VPC_ID.test(v));
  if (bad !== undefined) return { values, error: `VPC ID 형식이 아닙니다: ${bad}` };
  const dup = duplicate(values);
  if (dup !== null) return { values, error: `같은 VPC가 두 번 있습니다: ${dup}` };
  return { values, error: null };
}

export function parseTenantForm(form: TenantForm): TenantFormResult {
  const errors: TenantFormErrors = {};
  const name = form.name.trim();
  if (name === '') errors.name = '이름을 넣으세요.';
  else if (name.length > TENANT_NAME_MAX) errors.name = `이름은 ${TENANT_NAME_MAX}자 이하입니다.`;

  const awsAccountId = form.awsAccountId.trim();
  if (!ACCOUNT_ID.test(awsAccountId)) errors.awsAccountId = '계정 ID는 숫자 12자리입니다.';

  const roleArn = form.accessRoleArn.trim();
  if (roleArn !== '') {
    const match = ROLE_ARN.exec(roleArn);
    if (match === null) errors.accessRoleArn = '역할 ARN 형식이 아닙니다(arn:aws:iam::<계정>:role/<이름>).';
    else if (ACCOUNT_ID.test(awsAccountId) && match[1] !== awsAccountId)
      errors.accessRoleArn = '역할 ARN의 계정이 위 계정 ID와 다릅니다.';
  }

  const regions = parseRegions(form.regions);
  if (regions.error !== null) errors.regions = regions.error;
  const vpcs = parseVpcIds(form.collectVpcIds);
  if (vpcs.error !== null) errors.collectVpcIds = vpcs.error;

  if (Object.keys(errors).length > 0) return { ok: false, errors };
  const request: TenantCreateRequest = {
    name,
    aws_account_id: awsAccountId,
    regions: regions.values,
    collect_vpc_ids: vpcs.values,
  };
  if (roleArn !== '') request.access_role_arn = roleArn;
  return { ok: true, request };
}
