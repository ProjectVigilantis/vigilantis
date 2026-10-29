// 로그인 뒤 첫 화면과 사이드바 메뉴(#492). 플랫폼 여부는 `/auth/me`의 `account_type`으로만 가른다 —
// ADMIN 역할이나 `tenant_id` NULL로 추론하지 않는다(#506, PR #504 리뷰). 화면을 가르는 것은 편의이고
// 실제 차단은 서버(403)다.
//
// 첫 화면: 플랫폼 계정은 등급 · 배정과 관계없이 고객 관리(`/tenants`)다 — 담당 고객이 없는 고객 담당자는 거기서
// 배정 없음 안내를 본다. 고객사 ADMIN은 사용자 관리(`/users`), 고객사 OPERATOR · VIEWER는 기존 `/`에서 이용 제한을 본다.
// 업무 데이터를 조회하거나 새 홈을 만들지 않는다. 사용자 관리 메뉴는 고객 범위가 있는 플랫폼 계정(중앙 관리자 ·
// 배정된 고객 담당자)과 고객사 ADMIN에게만 열린다(roles.ts `canManageUsers`).
//
// 역할 권한과 열린 기능은 따로 판정한다. 권한(사용자 관리 · 플랫폼 여부 · 고객 범위)은 roles.ts가 정하고,
// 업무 기능(대시보드 · 자산 · 보안 화면, GNB 자산 조회, 실시간 연결)을 누구에게 열지는 `businessOpen`이 정한다 —
// 고객 범위가 있는 플랫폼 계정(중앙 관리자 · 담당 고객이 하나 이상인 고객 담당자)이다. 담당 고객이 없는 고객 담당자와
// 고객사(TENANT) 계정에는 열지 않는다. 고객 담당자가 받는 데이터 · 실시간 이벤트를 담당 고객으로 거르는 것은 서버다.
// 이 판정은 셸(메뉴 · GNB 조회 · 실시간 연결)과 대시보드의 서버 측 안내만 가른다.

import { canManageUsers, hasCustomerScope, isPlatform, type AccessSubject } from './roles.ts';

export const PLATFORM_HOME = '/tenants';
export const CUSTOMER_HOME = '/';
export const USERS_PATH = '/users';

/** 업무 화면 — 대시보드 · 자산 · 보안. */
export const BUSINESS_PATHS: readonly string[] = ['/', '/assets', '/security'];

/** 세션 Provider의 상태에서 계정 판정에 쓰는 모양(components/auth/session-provider.tsx `SessionState`). */
export type SessionView =
  | { kind: 'loading' }
  | { kind: 'unknown' }
  | { kind: 'anonymous' }
  | { kind: 'user'; user: AccessSubject };

/** `/me`로 확인된 계정. 확인 전 · 조회 실패 · 미로그인은 null이다 — 어떤 기능도 열지 않는다. */
export function confirmedAccount(session: SessionView): AccessSubject | null {
  return session.kind === 'user' ? session.user : null;
}

/**
 * 업무 기능(대시보드 · 자산 · 보안 화면, GNB 자산 조회, 실시간 연결)이 열려 있나 — 고객 범위가 있는 확인된 플랫폼
 * 계정(roles.ts `hasCustomerScope`)이다. 담당 고객이 없는 고객 담당자 · 고객사 계정 · 계정 미상(null)은 닫는다.
 * 실제 차단과 담당 범위 필터는 서버(403 · 목록 · 이벤트 필터)다.
 */
export function businessOpen(user: AccessSubject | null): boolean {
  return hasCustomerScope(user);
}

export function homePathFor(user: AccessSubject): string {
  if (isPlatform(user)) return PLATFORM_HOME;
  return canManageUsers(user) ? USERS_PATH : CUSTOMER_HOME;
}

/**
 * 사이드바에 보일 경로(순서대로). 계정을 아직 모르면(null — 확인 전 · 조회 실패 · 미로그인) 아무 메뉴도 열지 않는다.
 * 업무 화면은 `businessOpen`, 고객 관리는 플랫폼, 사용자 관리는 `canManageUsers`가 연다.
 */
export function navPathsFor(user: AccessSubject | null): string[] {
  if (user === null) return [];
  return [
    ...(businessOpen(user) ? BUSINESS_PATHS : []),
    ...(isPlatform(user) ? [PLATFORM_HOME] : []),
    ...(canManageUsers(user) ? [USERS_PATH] : []),
  ];
}
