// 로그인 뒤 첫 화면과 사이드바 메뉴(#492). 플랫폼 여부는 `/auth/me`의 `account_type`으로만 가른다 —
// ADMIN 역할이나 `tenant_id` NULL로 추론하지 않는다(#506, PR #504 리뷰). 화면을 가르는 것은 편의이고
// 실제 차단은 서버(403)다.
//
// 첫 화면: 플랫폼 운영자는 대시보드(`/`, 2026-10-29 · 종전 고객 관리 `/tenants`), 고객사 ADMIN은 사용자 관리(`/users`)다.
// 고객사 OPERATOR · VIEWER는 기존 `/`에서 이용 제한을 본다. 업무 데이터를 조회하거나 새 홈을 만들지 않는다.
//
// 역할 권한과 지금 열린 기능은 따로 판정한다. 권한(사용자 관리 · 플랫폼 여부)은 roles.ts가 정하고,
// 테넌트 격리(T-4 · T-5) 전에 업무 기능(자산 · 보안 화면, GNB 자산 조회, 전역 실시간 연결)을 누구에게 열지는
// `businessOpen`이 정한다 — 지금은 확인된 플랫폼 계정뿐이다. 이 판정은 셸(메뉴 · GNB 조회 · 실시간 연결)만 가른다 —
// 격리가 열릴 때는 서버 권한 검사와 페이지의 서버 측 판정도 함께 바꿔야 한다.

import { canManageUsers, isPlatform, type AccessSubject } from './roles.ts';
import { INQUIRIES_PATH, SIGNUP_REQUESTS_PATH } from './support.ts';

/** 플랫폼 운영자의 첫 화면 — 관리자 대시보드. 고객사 계정의 `/`는 같은 경로에서 이용 제한을 본다(app/page.tsx). */
export const PLATFORM_HOME = '/';
/** 고객 관리(Post-MVP T-4). */
export const TENANTS_PATH = '/tenants';
export const CUSTOMER_HOME = '/';
export const USERS_PATH = '/users';
/** 관리자 대시보드 숫자의 상세 화면 — 수동 처리가 필요한 문제 목록 · 고객사별 직원 표(2026-10-29). */
export const ISSUES_PATH = '/tenants/issues';
export const STAFF_PATH = '/users/tenants';

/** 통합 관제(MON-001) — 자산 · 보안 두 축을 한 화면에 둔다(2026-10-29). */
export const MONITORING_PATH = '/monitoring';

/**
 * 사이드바의 업무 화면 — 대시보드뿐이다(2026-10-29). 관리자 화면은 고객사 · 직원의 권한과 오류를 보는 자리라
 * 관제(자산 하나하나)를 메뉴에 두지 않고, 대시보드 → 고객사 상세 → 관제로 들어간다.
 */
export const BUSINESS_PATHS: readonly string[] = ['/'];

/** 통합 관제의 축. URL `?axis=` 값이며 없거나 모르는 값은 `assets`다. */
export type MonitoringAxis = 'assets' | 'security';

export function parseAxis(value: unknown): MonitoringAxis {
  return value === 'security' ? 'security' : 'assets';
}

/**
 * 통합 관제의 한 축으로 가는 경로. 옛 `/assets` · `/security`의 딥링크(`?asset=` · `?tab=` · `?preset=`)를
 * 그대로 옮긴다. `자산`이 기본 축이라 `axis`를 적지 않는다. 다른 페이지의 `axis` 값은 버린다.
 */
export function monitoringPath(
  axis: MonitoringAxis,
  params: Readonly<Record<string, string | string[] | undefined>> = {},
): string {
  const query = new URLSearchParams();
  if (axis !== 'assets') query.set('axis', axis);
  for (const [key, value] of Object.entries(params)) {
    if (key === 'axis' || value === undefined) continue;
    for (const one of Array.isArray(value) ? value : [value]) query.append(key, one);
  }
  const search = query.toString();
  return search === '' ? MONITORING_PATH : `${MONITORING_PATH}?${search}`;
}

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
 * 업무 기능(자산 · 보안 화면, GNB 자산 조회, 전역 실시간 연결)이 지금 열려 있나. 역할 권한이 아니라 현재 제공 범위다 —
 * 격리 전에는 확인된 플랫폼 계정에게만 연다. 계정을 모르면(null) 닫는다. 실제 차단은 서버(403)다.
 */
export function businessOpen(user: AccessSubject | null): boolean {
  return isPlatform(user);
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
    ...(isPlatform(user) ? [TENANTS_PATH] : []),
    ...(canManageUsers(user) ? [USERS_PATH] : []),
    // 도입 신청함 · 고객 문의함(2026-10-29)은 플랫폼 계정만. 고객사 계정은 상단바 [문의하기]로 보내기만 한다.
    ...(isPlatform(user) ? [SIGNUP_REQUESTS_PATH, INQUIRIES_PATH] : []),
  ];
}
