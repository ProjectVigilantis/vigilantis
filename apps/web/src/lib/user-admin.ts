// 사용자 관리(#492 PR 3) 화면의 표시 문구 · 오류 문구. 화면(components/users/users-view.tsx)은 이 결과를 그리기만 한다.

import type {
  ErrorCode,
  UserEventAction,
  UserEventItem,
  UserRole,
  UserStatus,
} from '../types/api.ts';

export const ROLE_LABELS: Record<UserRole, string> = {
  ADMIN: '관리자',
  OPERATOR: '운영자',
  VIEWER: '조회 전용',
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
const statusLabel = (status: UserStatus | null | undefined) => (status ? STATUS_LABELS[status] : NONE);

/** 이력 1건의 변경 내용 한 줄. 이력에 실린 닫힌 값만 쓴다. 덧붙일 것이 없으면 빈 문자열. */
export function eventSummary(event: UserEventItem): string {
  const { before_state: before, after_state: after } = event;
  switch (event.action) {
    case 'ROLE_CHANGED':
      return `${roleLabel(before.role)} → ${roleLabel(after.role)}`;
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

/** 사용자 관리 403 — 조치 화면의 "조회 전용 계정" 문구와 다르다(역할이 무엇이든 ADMIN이 아니면 같은 거절). */
export const USER_ADMIN_FORBIDDEN = '관리자만 사용자를 관리할 수 있습니다.';

/** 초대 메일은 DB 기록보다 먼저 나갈 수 있다 — 결과를 모르는 실패에서 "보내지 않았다"고 말하지 않는다. */
export const INVITE_UNCERTAIN =
  '처리가 완료되지 않았습니다. 초대 메일이 발송되었을 수 있습니다. 목록을 새로고침해 확인한 뒤 다시 시도해 주세요.';
const UNCERTAIN = '처리 결과를 확인하지 못했습니다. 목록을 새로고침해 확인해 주세요.';
export const INVITE_RECOVER_NOT_FOUND =
  '연결할 기존 인증 계정을 찾지 못했습니다. 이메일을 확인하거나, 기존 계정 연결을 해제하고 초대해 주세요.';

const MESSAGES: Partial<Record<ErrorCode, string>> = {
  AUTH_FORBIDDEN: USER_ADMIN_FORBIDDEN,
  USER_NOT_FOUND: '사용자를 찾을 수 없습니다. 목록을 새로고침해 주세요.',
  USER_STATE_CONFLICT:
    '지금 상태에서는 처리할 수 없습니다. 마지막 활성 관리자이거나, 이미 초대된 이메일이거나, 그 사이 상태가 바뀌었을 수 있습니다.',
  AUTH_RATE_LIMITED: '요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.',
  AUTH_PROVIDER_UNAVAILABLE: '인증 서비스에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.',
  AUTH_ORIGIN_REJECTED: '허용되지 않은 주소에서 보낸 요청입니다.',
  REQUEST_VALIDATION_FAILED: '입력값을 다시 확인해 주세요.',
};

/**
 * 조치 실패 문구. `code`가 null이면 응답을 받지 못한 실패(네트워크)다.
 * 초대 · 재발송의 500 · 네트워크 실패는 메일이 이미 나갔을 수 있어 별도 문구다.
 */
export function userAdminErrorMessage(action: UserAdminAction, code: ErrorCode | null): string {
  // 초대의 404는 "기존 계정 연결(복구)"을 골랐는데 연결할 인증 계정이 없다는 뜻이다(목록의 대상 없음과 다르다).
  if (action === 'invite' && code === 'USER_NOT_FOUND') return INVITE_RECOVER_NOT_FOUND;
  const known = code === null ? undefined : MESSAGES[code];
  if (known) return known;
  return action === 'invite' || action === 'resend' ? INVITE_UNCERTAIN : UNCERTAIN;
}
