// 역할별 조치 가능 여부(#492 PR 2). **화면 편의일 뿐이다** — 실제 차단은 서버의 403 `AUTH_FORBIDDEN`이다.

import type { CurrentUserResponse, UserRole } from '../types/api.ts';

/** 계정 종류 · 역할 — 화면 판정에 쓰는 `/me`의 두 값. */
export type AccessSubject = Pick<CurrentUserResponse, 'account_type' | 'role'>;

/**
 * 화면 용어(2026-10-29 PM 확정). 역할은 넷이다.
 *   관리자        우리 회사 쪽 총 관리자 계정. 모든 고객사 · 고객사 직원 계정 연결과 서버 상태를 본다
 *   관리자 직원   관리자 아래의 우리 회사 직원 계정
 *   고객사        플랫폼에 등록된 회사(테넌트). 로그인 계정이 아니라 회사 자체다
 *   고객사 직원   고객사에 실제로 근무하는 직원의 로그인 계정
 * 로그인 계정은 관리자 · 관리자 직원 · 고객사 직원 셋이고, 고객사는 그 직원들이 속하는 단위다.
 *
 * 서버 계약과의 대응: 관리자 = `PLATFORM` + `ADMIN`, 관리자 직원 = `PLATFORM` + 그 밖의 역할, 고객사 직원 = `TENANT`.
 * 지금 서버는 `PLATFORM`을 운영 CLI로만 만들고 언제나 `ADMIN`이라(auth/README.md §플랫폼 계정 CLI) 관리자 직원이 실제로 생기지는 않는다.
 */
export type AccountKind = 'PLATFORM_ADMIN' | 'PLATFORM_STAFF' | 'TENANT_STAFF';

export const ACCOUNT_KIND_LABELS: Record<AccountKind, string> = {
  PLATFORM_ADMIN: '관리자',
  PLATFORM_STAFF: '관리자 직원',
  TENANT_STAFF: '고객사 직원',
};

export function accountKind(user: AccessSubject): AccountKind {
  if (user.account_type === 'TENANT') return 'TENANT_STAFF';
  return user.role === 'ADMIN' ? 'PLATFORM_ADMIN' : 'PLATFORM_STAFF';
}

/** 서버 403 `AUTH_FORBIDDEN`과 조회 전용 계정의 비활성 버튼이 함께 쓰는 문구. */
export const VIEWER_ACTION_BLOCKED = '조회 전용 계정은 조치할 수 없습니다.';
const ROLE_PENDING = '권한을 확인한 뒤 조치할 수 있습니다.';

/** 실행 · 복구 · 종료를 누를 수 있는 역할. 역할을 아직 모르면(null) 누를 수 없다. */
export function canOperate(role: UserRole | null | undefined): boolean {
  return role === 'ADMIN' || role === 'OPERATOR';
}

/**
 * 플랫폼 운영자인가. `account_type`으로만 가른다 — ADMIN 역할이나 소속 없음(NULL)은 플랫폼 권한이 아니다(#492).
 * 고객 관리(/tenants)와, 테넌트 격리(T-4 · T-5) 전까지의 업무 화면 · 실시간 연결이 이 판정을 쓴다.
 */
export function isPlatform(user: Pick<CurrentUserResponse, 'account_type'> | null | undefined): boolean {
  return user?.account_type === 'PLATFORM';
}

/**
 * 사용자 관리를 열 계정 — 플랫폼 운영자, 또는 고객사 ADMIN(자기 고객사만). 고객사 OPERATOR · VIEWER는 아니다.
 * 메뉴 · 첫 화면만 정한다 — 실제 차단과 관리 범위는 서버(403 · 404)다. 확인 전(null)이면 열지 않는다.
 */
export function canManageUsers(user: AccessSubject | null | undefined): boolean {
  if (!user) return false;
  return user.account_type === 'PLATFORM' || (user.account_type === 'TENANT' && user.role === 'ADMIN');
}

/** 조치 버튼을 막는 이유. 누를 수 있으면 null. */
export function operateBlockedReason(role: UserRole | null | undefined): string | null {
  if (canOperate(role)) return null;
  return role === null || role === undefined ? ROLE_PENDING : VIEWER_ACTION_BLOCKED;
}
