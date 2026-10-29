// 사용자 관리(#492 PR 3) 화면의 표시 문구 · 오류 문구. 화면(components/users/users-view.tsx)은 이 결과를 그리기만 한다.

import { canAccessTenant, hasCustomerScope, type PlatformSubject } from './roles.ts';
import type {
  ErrorCode,
  PlatformRole,
  UserEventAction,
  UserEventItem,
  UserInviteRequest,
  UserRole,
  UserStatus,
} from '../types/api.ts';

export const ROLE_LABELS: Record<UserRole, string> = {
  ADMIN: '관리자',
  OPERATOR: '운영자',
  VIEWER: '조회 전용',
};

/** 플랫폼 계정의 권한 단계(`platform_role`). 플랫폼 계정의 호환 역할(ADMIN) 대신 이 이름을 보인다. */
export const PLATFORM_ROLE_LABELS: Record<PlatformRole, string> = {
  CENTRAL_ADMIN: '중앙 관리자',
  TENANT_MANAGER: '고객 담당자',
};

export const STATUS_LABELS: Record<UserStatus, string> = {
  ACTIVE: '활성',
  DISABLED: '비활성',
};

export const EVENT_LABELS: Record<UserEventAction, string> = {
  USER_INVITED: '초대',
  INVITE_RESENT: '초대 메일 재발송',
  ROLE_CHANGED: '역할 변경',
  STATUS_CHANGED: '상태 변경',
  SESSIONS_REVOKED: '로그인 세션 종료',
  PLATFORM_ROLE_CHANGED: '플랫폼 권한 변경',
  TENANT_ASSIGNMENTS_CHANGED: '담당 고객 변경',
};

const KST_OFFSET_MS = 9 * 60 * 60_000;
const pad2 = (n: number) => String(n).padStart(2, '0');

/**
 * 사용자 관리 화면의 시각(마지막 로그인 · 이력) — `YYYY-MM-DD HH:mm:ss KST`(24시간제).
 * Intl · toLocaleString을 쓰지 않는다: 서버(Node)와 브라우저의 ICU가 오전/AM 같은 표기를 다르게 내
 * 하이드레이션이 어긋났다. KST는 일광절약이 없어 UTC+9 고정 산술로 같은 문자열을 만든다.
 */
export function formatUserTime(iso: string): string {
  const kst = new Date(new Date(iso).getTime() + KST_OFFSET_MS);
  const date = `${kst.getUTCFullYear()}-${pad2(kst.getUTCMonth() + 1)}-${pad2(kst.getUTCDate())}`;
  const time = `${pad2(kst.getUTCHours())}:${pad2(kst.getUTCMinutes())}:${pad2(kst.getUTCSeconds())}`;
  return `${date} ${time} KST`;
}

const NONE = '—';
const roleLabel = (role: UserRole | null | undefined) => (role ? ROLE_LABELS[role] : NONE);
const platformRoleLabel = (role: PlatformRole | null | undefined) => (role ? PLATFORM_ROLE_LABELS[role] : NONE);
const statusLabel = (status: UserStatus | null | undefined) => (status ? STATUS_LABELS[status] : NONE);
/** 담당 고객 목록은 개수만 보인다 — 고객 ID를 이력 한 줄에 나열하지 않는다. */
const tenantCountLabel = (ids: string[] | null | undefined) => (ids ? `${ids.length}곳` : NONE);

/** 이력 1건의 변경 내용 한 줄. 이력에 실린 닫힌 값만 쓴다. 덧붙일 것이 없으면 빈 문자열. */
export function eventSummary(event: UserEventItem): string {
  const { before_state: before, after_state: after } = event;
  switch (event.action) {
    case 'ROLE_CHANGED':
      return `${roleLabel(before.role)} → ${roleLabel(after.role)}`;
    case 'PLATFORM_ROLE_CHANGED':
      return `${platformRoleLabel(before.platform_role)} → ${platformRoleLabel(after.platform_role)}`;
    case 'TENANT_ASSIGNMENTS_CHANGED':
      return `${tenantCountLabel(before.tenant_ids)} → ${tenantCountLabel(after.tenant_ids)}`;
    case 'STATUS_CHANGED':
      return `${statusLabel(before.status)} → ${statusLabel(after.status)}`;
    case 'SESSIONS_REVOKED':
      return typeof after.revoked_session_count === 'number' ? `${after.revoked_session_count}개 종료` : '';
    case 'USER_INVITED':
      return [after.role ? ROLE_LABELS[after.role] : '', after.recover_existing ? '기존 계정 연결' : '']
        .filter(Boolean)
        .join(' · ');
    case 'INVITE_RESENT':
      return '';
  }
}

export type UserAdminAction = 'invite' | 'resend' | 'role' | 'status' | 'revoke';

/**
 * 사용자 관리 403 — 조치 화면의 "조회 전용 계정" 문구와 다르다. 중앙 관리자, 담당 고객이 배정된 고객 담당자,
 * 고객사 ADMIN이 아니면 같은 거절이다 — 담당 고객이 없는 고객 담당자도 여기에 든다.
 */
export const USER_ADMIN_FORBIDDEN =
  '중앙 관리자, 담당 고객이 배정된 고객 담당자 또는 고객사 관리자만 사용자를 관리할 수 있습니다.';

/** 초대 메일은 DB 기록보다 먼저 나갈 수 있다 — 결과를 모르는 실패에서 "보내지 않았다"고 말하지 않는다. */
export const INVITE_UNCERTAIN =
  '처리가 완료되지 않았습니다. 초대 메일이 발송되었을 수 있습니다. 목록을 새로고침해 확인한 뒤 다시 시도해 주세요.';
const UNCERTAIN = '처리 결과를 확인하지 못했습니다. 목록을 새로고침해 확인해 주세요.';
export const INVITE_RECOVER_NOT_FOUND =
  '연결할 기존 인증 계정을 찾지 못했습니다. 이메일을 확인하거나, 기존 계정 연결을 해제하고 초대해 주세요.';

const INVITE_STATE_CONFLICT_REASON =
  '지금 상태에서는 초대할 수 없습니다. 이미 초대된 이메일이거나 그 사이 상태가 바뀌었을 수 있습니다.';
/** 고객사 ADMIN의 초대 409 — 기존 계정 연결(복구)은 고객 범위가 있는 플랫폼 계정만 고를 수 있어 안내하지 않는다. */
export const INVITE_STATE_CONFLICT = `${INVITE_STATE_CONFLICT_REASON} 목록에서 같은 이메일의 사용자를 확인해 주세요.`;
/** 고객 범위가 있는 플랫폼 계정의 초대 409 — 같은 이메일의 기존 인증 계정이면 기존 계정 연결(복구)로 다시 시도할 수 있다. */
export const INVITE_STATE_CONFLICT_PLATFORM = `${INVITE_STATE_CONFLICT_REASON} 같은 이메일의 기존 인증 계정이 있고 계정 주인을 확인했다면 "기존 인증 계정을 연결 (복구)"를 선택해 다시 시도할 수 있습니다.`;

/**
 * 역할 · 활성 상태 변경 가능 여부. 본인 행은 서버가 거절하고, 세션 확인 전(actor 미상)에는 본인인지 알 수 없어 막는다.
 * 세션 종료는 본인도 할 수 있으므로 이 판정을 쓰지 않는다.
 */
export const canChangeRow = (rowUserId: string, actorUserId: string | null) =>
  actorUserId !== null && rowUserId !== actorUserId;

/*
 * 고객 상태 거절 3종은 고객 쪽 사유(없음 · 비활성 · 정지)를 말한다. 서버는 인증 서비스 호출 뒤에도 고객 · 요청자를
 * 다시 확인하므로, 이 코드만으로 메일이 나가지 않았다고 단정할 수 없다 — 문구는 발송 여부를 말하지 않는다.
 */
export const INVITE_TENANT_NOT_FOUND = '입력한 고객 ID의 고객을 찾을 수 없습니다. 고객 ID를 확인해 주세요.';
export const TENANT_NOT_ACTIVE = '활성 상태인 고객의 사용자만 초대하거나 초대 메일을 다시 보낼 수 있습니다.';
export const TENANT_SUSPENDED_BLOCKED = '정지된 고객의 계정이라 지금은 사용자를 변경할 수 없습니다.';

const MESSAGES: Partial<Record<ErrorCode, string>> = {
  AUTH_FORBIDDEN: USER_ADMIN_FORBIDDEN,
  // 관리 범위 밖(다른 고객사) 사용자도 서버는 같은 404로 답한다 — 있는지 여부를 말하지 않는다.
  USER_NOT_FOUND: '사용자를 찾을 수 없습니다. 목록을 새로고침해 주세요.',
  USER_STATE_CONFLICT:
    '지금 상태에서는 처리할 수 없습니다. 고객사의 마지막 활성 관리자이거나, 이미 초대된 이메일이거나, 그 사이 상태가 바뀌었을 수 있습니다.',
  TENANT_NOT_FOUND: '고객을 찾을 수 없습니다. 목록을 새로고침해 주세요.',
  TENANT_STATE_CONFLICT: TENANT_NOT_ACTIVE,
  TENANT_SUSPENDED: TENANT_SUSPENDED_BLOCKED,
  AUTH_RATE_LIMITED: '요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.',
  AUTH_PROVIDER_UNAVAILABLE: '인증 서비스에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.',
  AUTH_ORIGIN_REJECTED: '허용되지 않은 주소에서 보낸 요청입니다.',
  REQUEST_VALIDATION_FAILED: '입력값을 다시 확인해 주세요.',
};

/**
 * 조치 실패 문구. `code`가 null이면 응답을 받지 못한 실패(네트워크)다.
 * 초대 · 재발송의 500 · 네트워크 실패는 메일이 이미 나갔을 수 있어 별도 문구다.
 * `actor`는 조치한 계정이다. 기존 계정 연결(복구)은 고객 범위가 있는 플랫폼 계정(중앙 관리자 · 배정된 고객 담당자)만
 * 고를 수 있어, 복구 안내는 그때만 붙는다(담당 고객 없는 담당자 · 고객사 계정 · 모르면 붙이지 않는다).
 */
export function userAdminErrorMessage(
  action: UserAdminAction,
  code: ErrorCode | null,
  actor: PlatformSubject | null = null,
): string {
  if (action === 'invite' && hasCustomerScope(actor)) {
    // 초대의 404는 "기존 계정 연결(복구)"을 골랐는데 연결할 인증 계정이 없다는 뜻이다(목록의 대상 없음과 다르다).
    if (code === 'USER_NOT_FOUND') return INVITE_RECOVER_NOT_FOUND;
    // 초대의 409는 이미 인증 서비스에 계정이 있는 이메일일 수 있다 — 계정 주인을 확인했다면 기존 계정 연결(복구)을 고를 수 있다.
    if (code === 'USER_STATE_CONFLICT') return INVITE_STATE_CONFLICT_PLATFORM;
  }
  // 초대의 고객 404는 플랫폼 운영자가 입력한 고객 ID가 없다는 뜻이다.
  if (action === 'invite' && code === 'TENANT_NOT_FOUND') return INVITE_TENANT_NOT_FOUND;
  if (action === 'invite' && code === 'USER_STATE_CONFLICT') return INVITE_STATE_CONFLICT;
  const known = code === null ? undefined : MESSAGES[code];
  if (known) return known;
  return action === 'invite' || action === 'resend' ? INVITE_UNCERTAIN : UNCERTAIN;
}

/* ─────────────────────────── 초대 요청 ─────────────────────────── */

/** 고객 ID(UUID) — 하이픈을 넣은 표준 표기 36자. 대소문자는 가리지 않는다. 최종 판정은 서버다. */
const TENANT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const TENANT_ID_REQUIRED = '초대할 고객의 고객 ID를 입력해 주세요.';
export const TENANT_ID_INVALID = '고객 ID는 UUID 형식입니다(예: 123e4567-e89b-12d3-a456-426614174000).';
/** 고객 담당자가 담당이 아닌 고객 ID를 넣었다 — 보내지 않는다(서버도 범위 밖으로 거절한다). */
export const TENANT_NOT_ASSIGNED = '담당 고객에만 사용자를 초대할 수 있습니다. 고객 ID를 확인해 주세요.';

export interface InviteForm {
  email: string;
  role: UserRole;
  /** 기존 계정 연결(복구) 선택. 고객 범위가 있는 플랫폼 계정만 고를 수 있다 — 그 밖의 계정은 무시하고 false로 보낸다. */
  recoverExisting: boolean;
  /** 플랫폼 계정이 입력한 고객 ID. 고객사 ADMIN은 입력하지 않는다(무시한다). */
  tenantId: string;
}

export type InviteRequestResult = { ok: true; request: UserInviteRequest } | { ok: false; error: string };

/**
 * 초대 본문. 플랫폼 계정은 고객 ID를 **반드시** 싣는다 — 스키마상 선택이어도 빠지면 서버가 거절한다.
 * 고객사 ADMIN은 `tenant_id`를 싣지 않는다(서버가 자기 소속으로 정한다 · 다른 고객을 고를 수 없다).
 * 플랫폼 계정은 그 고객을 다룰 수 있어야 보낸다(`canAccessTenant` — 중앙 관리자는 모든 고객, 고객 담당자는 배정된
 * 고객만). 배정 목록은 화면의 스냅샷이라 최종 판정은 서버다(범위 밖 403 · 404).
 * 기존 계정 연결은 그렇게 보내는 플랫폼 계정의 선택일 때만 true다 — 고객사 ADMIN은 언제나 false다(서버가 403으로 거절한다).
 * 계정 종류는 입력이 아니다. 이메일은 다듬지 않고 그대로 보내고(서버가 형식을 판정), 고객 ID만 앞뒤 공백을 지운다.
 */
export function inviteRequestFor(inviter: PlatformSubject, form: InviteForm): InviteRequestResult {
  const request: UserInviteRequest = { email: form.email, role: form.role, recover_existing: false };
  if (inviter.account_type === 'TENANT') return { ok: true, request };
  const tenantId = form.tenantId.trim();
  if (tenantId === '') return { ok: false, error: TENANT_ID_REQUIRED };
  if (!TENANT_ID.test(tenantId)) return { ok: false, error: TENANT_ID_INVALID };
  if (!canAccessTenant(inviter, tenantId)) return { ok: false, error: TENANT_NOT_ASSIGNED };
  return { ok: true, request: { ...request, tenant_id: tenantId, recover_existing: form.recoverExisting } };
}
