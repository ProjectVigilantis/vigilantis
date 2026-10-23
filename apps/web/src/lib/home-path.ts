// 로그인 뒤 첫 화면과 사이드바 메뉴(#492). 플랫폼 여부는 `/auth/me`의 `account_type`으로만 가른다 —
// ADMIN 역할이나 `tenant_id` NULL로 추론하지 않는다(#506, PR #504 리뷰). 화면을 가르는 것은 편의이고
// 실제 차단은 서버(403)다.
//
// 테넌트 격리(T-4 · T-5) 전 임시 정책: 고객사 계정은 로그인 · `/me` · 자기 고객사 사용자 관리만 쓸 수 있고,
// 업무 화면(대시보드 · 자산 · 보안)과 실시간 연결은 플랫폼 운영자 전용이다. 그래서 고객사 ADMIN의 첫 화면은
// 사용자 관리이고, 고객사 OPERATOR · VIEWER는 고객 첫 화면(`/`)에서 서버 403 안내를 본다. 격리가 열리면
// `BUSINESS_PATHS`를 고객사에게도 열고 고객사 ADMIN의 첫 화면을 `CUSTOMER_HOME`으로 되돌린다.

import { canManageUsers, isPlatform, type AccessSubject } from './roles.ts';

export const PLATFORM_HOME = '/tenants';
export const CUSTOMER_HOME = '/';
export const USERS_PATH = '/users';

/** 업무 화면 — 대시보드 · 자산 · 보안. 격리 전에는 플랫폼 전용이다. */
export const BUSINESS_PATHS: readonly string[] = ['/', '/assets', '/security'];

export function homePathFor(user: AccessSubject): string {
  if (isPlatform(user)) return PLATFORM_HOME;
  return canManageUsers(user) ? USERS_PATH : CUSTOMER_HOME;
}

/**
 * 사이드바에 보일 경로(순서대로). 계정을 아직 모르면(null — 확인 전 · 조회 실패) 지금까지처럼 업무 화면만 보이고
 * 관리 메뉴는 열지 않는다. 고객 관리(/tenants)는 플랫폼 운영자에게만 보인다.
 */
export function navPathsFor(user: AccessSubject | null): string[] {
  const platform = isPlatform(user);
  return [
    ...(user === null || platform ? BUSINESS_PATHS : []),
    ...(platform ? [PLATFORM_HOME] : []),
    ...(canManageUsers(user) ? [USERS_PATH] : []),
  ];
}
