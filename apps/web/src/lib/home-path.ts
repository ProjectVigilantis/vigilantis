// 로그인 뒤 첫 화면과 사이드바 메뉴(#492). 플랫폼 여부는 `/auth/me`의 `account_type`으로만 가른다 —
// ADMIN 역할이나 `tenant_id` NULL로 추론하지 않는다(#506, PR #504 리뷰). 화면을 가르는 것은 편의이고
// 실제 차단은 서버(403)다.
//
// 첫 화면: 고객 범위가 있는 플랫폼 계정(중앙 관리자 · 배정된 고객 담당자)은 대시보드(`/`, 2026-10-29 · 종전 고객 관리
// `/tenants`)다. 담당 고객이 없는 고객 담당자는 업무 화면이 닫혀 있어 고객 관리(`/tenants`)에서 배정 없음 안내를 본다.
// 고객사 계정은 역할과 관계없이 고객사 대시보드(`/`)가 첫 화면이다(2026-11-03 — 종전 ADMIN은 `/users`, 그 밖은
// 이용 제한). 사용자 관리 메뉴는 중앙 관리계정과 고객사 ADMIN에게만 열린다(roles.ts `canManageUsers`).
//
// 역할 권한과 열린 기능은 따로 판정한다. 권한(사용자 관리 · 플랫폼 여부 · 고객 범위)은 roles.ts가 정하고,
// 업무 기능(대시보드 · 관제 화면, GNB 자산 조회, 실시간 연결)을 누구에게 열지는 `businessOpen`이 정한다 —
// 고객사 계정과 고객 범위가 있는 플랫폼 계정(중앙 관리자 · 담당 고객이 하나 이상인 고객 담당자)이다. 담당 고객이
// 없는 고객 담당자에게는 열지 않는다. 받는 데이터 · 실시간 이벤트를 자기 고객사 · 담당 고객으로 거르는 것은 서버다.
// 이 판정은 셸(메뉴 · GNB 조회 · 실시간 연결)과 대시보드의 서버 측 안내만 가른다.
//
// 구독(`/subscription` · T-16 #528)은 고객사 자기 구독이라 고객사 중앙계정 · 고객사 직원계정 모두에 연다(쓰기는 서버가
// 고객사 중앙계정만 받는다). 플랫폼 계정에는 자기 구독이 없다(서버 403). 첫 화면은 바꾸지 않는다.

import { canManageUsers, hasCustomerScope, isCentralAdmin, isPlatform, type AccessSubject } from './roles.ts';
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
/**
 * 고객사 계정의 자기 고객 설정(T-7). 역할과 관계없이 연다(OPERATOR · VIEWER는 조회만). 플랫폼 계정은 고객 상세
 * (`/tenants/[id]/settings`)에서 들어간다 — 전체 고객을 고르는 메뉴는 두지 않는다.
 */
export const SETTINGS_PATH = '/settings';
export const SUBSCRIPTION_PATH = '/subscription';

/** 통합 관제(MON-001) — 자산 · 보안 두 축을 한 화면에 둔다(2026-10-29). */
export const MONITORING_PATH = '/monitoring';

/** 로그(2026-11-03): 조치 실행 · 종료, 위협 유입, 인시던트 발생을 시각순으로 본다. 고객사 계정 메뉴에 있다. */
export const LOGS_PATH = '/logs';

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
 * 업무 기능(대시보드 · 관제 화면, GNB 자산 조회, 실시간 연결)이 열려 있나 — 고객사 계정과 고객 범위가 있는 확인된
 * 플랫폼 계정(roles.ts `hasCustomerScope`)이다. 담당 고객이 없는 고객 담당자 · 계정 미상(null)은 닫는다.
 * 고객사 계정은 2026-11-03부터 연다 — 서버가 자산 · 인시던트 · 실시간 이벤트를 자기 고객사로 거른다(#535 등).
 * 실제 차단과 범위 필터는 서버(403 · 목록 · 이벤트 필터)다.
 */
export function businessOpen(user: AccessSubject | null): boolean {
  if (user === null) return false;
  return !isPlatform(user) || hasCustomerScope(user);
}

/** 고객사 계정의 업무 메뉴 — 대시보드와 관제. 고객사 상세가 없으니 관제를 메뉴에 둔다(관리자는 고객사 상세에서 들어간다). */
export const CUSTOMER_BUSINESS_PATHS: readonly string[] = [CUSTOMER_HOME, MONITORING_PATH, LOGS_PATH];

/** 자기 구독 화면이 열려 있나 — 확인된 고객사 계정이면 역할과 관계없이 연다. */
export function subscriptionOpen(user: AccessSubject | null): boolean {
  return user?.account_type === 'TENANT';
}

export function homePathFor(user: AccessSubject): string {
  // 담당 고객이 없는 고객 담당자는 대시보드가 닫혀 있어 고객 관리(배정 없음 안내)로 보낸다.
  if (isPlatform(user)) return businessOpen(user) ? PLATFORM_HOME : TENANTS_PATH;
  return CUSTOMER_HOME;
}

/**
 * 사이드바에 보일 경로(순서대로). 계정을 아직 모르면(null — 확인 전 · 조회 실패 · 미로그인) 아무 메뉴도 열지 않는다.
 * 업무 화면은 `businessOpen`, 고객 관리는 플랫폼, 사용자 관리는 `canManageUsers`가 연다.
 * 고객 설정과 자기 구독은 고객사 계정에 연다.
 */
export function navPathsFor(user: AccessSubject | null): string[] {
  if (user === null) return [];
  // 고객 설정은 고객사 계정이면 역할과 관계없이 연다. 플랫폼 계정은 고객사 상세에서 들어간다.
  if (!isPlatform(user)) return [
    ...CUSTOMER_BUSINESS_PATHS,
    ...(canManageUsers(user) ? [USERS_PATH] : []),
    SETTINGS_PATH,
    SUBSCRIPTION_PATH,
  ];
  return [
    ...(businessOpen(user) ? BUSINESS_PATHS : []),
    ...(isPlatform(user) ? [TENANTS_PATH] : []),
    ...(canManageUsers(user) ? [USERS_PATH] : []),
    // 도입 신청함(2026-10-29)은 중앙 관리자만 — 서버가 `require_central_admin`으로 막는다(#492).
    ...(isCentralAdmin(user) ? [SIGNUP_REQUESTS_PATH] : []),
    // 고객 문의함은 플랫폼 계정만. 고객사 계정은 사이드바 [문의하기]로 보내기만 한다.
    ...(isPlatform(user) ? [INQUIRIES_PATH] : []),
  ];
}
