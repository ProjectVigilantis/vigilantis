// 계정별 화면 권한(#492 PR 2 · PR 5). **화면 편의일 뿐이다** — 실제 차단과 범위는 서버(403 `AUTH_FORBIDDEN` ·
// 범위 밖 404)가 요청마다 다시 판정한다. `/me` 값은 낡을 수 있으므로 여기서 연 버튼도 서버가 거절할 수 있다.
//
// 플랫폼 계정은 `platform_role`(CENTRAL_ADMIN · TENANT_MANAGER)과 담당 배정(`assigned_tenant_ids`)으로만 가른다 —
// 플랫폼 계정의 호환 역할 `role=ADMIN`은 권한의 근거가 아니다. 고객사(TENANT) 계정은 기존 역할(`role`)로 가른다.
//
// 판정은 두 층이다. 넓은 자격(`hasCustomerScope` · `canManageUsers` · `canOperate`)은 메뉴 · 화면을 열지 정하고,
// 대상별 권한(`canAccessTenant` · `canManageUsersOf`)은 특정 고객 · 고객사 사용자에 대한 버튼을 그릴지 정한다.
// 담당 고객이 없는 고객 담당자는 넓은 자격부터 없다 — 고객 데이터 · 조치 · 사용자 관리를 열지 않는다.

import type { CurrentUserResponse } from '../types/api.ts';

/** 계정 종류 · 역할 · 플랫폼 등급 · 담당 배정 — 화면 판정에 쓰는 `/me` 값. */
export type AccessSubject = Pick<CurrentUserResponse, 'account_type' | 'role' | 'platform_role' | 'assigned_tenant_ids'>;

/** 플랫폼 고객 범위 판정에 쓰는 값. */
export type PlatformSubject = Pick<CurrentUserResponse, 'account_type' | 'platform_role' | 'assigned_tenant_ids'>;

/** 고객사 사용자에 대한 대상별 판정 — 고객사 ADMIN의 자기 소속(`tenant_id`)까지 본다. */
export type UserAdminSubject = AccessSubject & Pick<CurrentUserResponse, 'tenant_id'>;

/** 조회 전용 계정의 비활성 버튼 문구. */
export const VIEWER_ACTION_BLOCKED = '조회 전용 계정은 조치할 수 없습니다.';
/** 담당 고객이 없는 고객 담당자의 비활성 버튼 문구. */
export const UNASSIGNED_ACTION_BLOCKED = '담당 고객이 배정되지 않아 조치할 수 없습니다.';
/**
 * 조치 요청이 서버 403 `AUTH_FORBIDDEN`으로 거절됐을 때의 문구. 이유가 역할(조회 전용)인지 담당 범위인지 응답만으로는
 * 알 수 없어 둘 다 말한다 — 플랫폼 계정에게 "조회 전용 계정"이라고 단정하지 않는다.
 */
export const ACTION_FORBIDDEN = '이 계정으로는 이 조치를 할 수 없습니다. 조회 전용 계정이거나 담당 고객이 아닐 수 있습니다.';
const ROLE_PENDING = '권한을 확인한 뒤 조치할 수 있습니다.';

/**
 * 플랫폼 운영자인가. `account_type`으로만 가른다 — ADMIN 역할이나 소속 없음(NULL)은 플랫폼 권한이 아니다(#492).
 * 등급 · 배정과 무관한 계정 종류 판정이다(고객 관리 메뉴 · 첫 화면). 고객 데이터 범위는 `hasCustomerScope`다.
 */
export function isPlatform(user: Pick<CurrentUserResponse, 'account_type'> | null | undefined): boolean {
  return user?.account_type === 'PLATFORM';
}

/** 중앙 관리자인가 — 전체 고객 · 고객 생성. 배정 목록(언제나 빈 목록)이 아니라 등급으로 판정한다. */
export function isCentralAdmin(user: PlatformSubject | null | undefined): boolean {
  return user?.account_type === 'PLATFORM' && user.platform_role === 'CENTRAL_ADMIN';
}

const isTenantManager = (user: PlatformSubject | null | undefined): user is PlatformSubject =>
  user?.account_type === 'PLATFORM' && user.platform_role === 'TENANT_MANAGER';

/** 담당 고객이 하나도 없는 고객 담당자인가 — 고객 데이터 · 조치 · 사용자 관리가 없는 상태를 보일 때 쓴다. */
export function isUnassignedManager(user: PlatformSubject | null | undefined): boolean {
  return isTenantManager(user) && user.assigned_tenant_ids.length === 0;
}

/**
 * 고객 범위가 있는 플랫폼 계정인가(넓은 자격) — 중앙 관리자, 또는 담당 고객이 하나 이상인 고객 담당자.
 * 업무 화면 · 실시간 연결(lib/home-path.ts `businessOpen`), 고객사 사용자 관리, 기존 계정 연결(복구), 조치 자격을
 * 열지 정한다. 특정 고객에 대한 권한은 `canAccessTenant`다.
 */
export function hasCustomerScope(user: PlatformSubject | null | undefined): boolean {
  return isCentralAdmin(user) || (isTenantManager(user) && user.assigned_tenant_ids.length > 0);
}

/**
 * 이 고객을 다룰 수 있나(대상별) — 중앙 관리자는 모든 고객, 고객 담당자는 배정된 고객만. 고객 ID(UUID)는 대소문자를
 * 가리지 않고 비교한다. 서버가 돌려준 고객이어도 배정 스냅샷에 없으면 버튼을 그리지 않는다(서버가 다시 판정한다).
 */
export function canAccessTenant(user: PlatformSubject | null | undefined, tenantId: string): boolean {
  if (isCentralAdmin(user)) return true;
  if (!isTenantManager(user)) return false;
  const target = tenantId.toLowerCase();
  return user.assigned_tenant_ids.some((id) => id.toLowerCase() === target);
}

/** 고객 추가(생성) — 중앙 관리자만. 새 고객은 어떤 담당자에게도 자동 배정되지 않는다. */
export function canCreateTenant(user: PlatformSubject | null | undefined): boolean {
  return isCentralAdmin(user);
}

/**
 * 사용자 관리를 열 계정(넓은 자격) — 고객 범위가 있는 플랫폼 계정, 또는 고객사 ADMIN(자기 고객사만).
 * 담당 고객이 없는 고객 담당자와 고객사 OPERATOR · VIEWER는 아니다. 메뉴 · 첫 화면만 정한다 — 실제 차단과 관리
 * 범위는 서버(403 · 404)다. 확인 전(null)이면 열지 않는다.
 */
export function canManageUsers(user: AccessSubject | null | undefined): boolean {
  if (!user) return false;
  return hasCustomerScope(user) || (user.account_type === 'TENANT' && user.role === 'ADMIN');
}

/**
 * 이 고객사의 사용자를 관리할 수 있나(대상별) — 플랫폼 계정은 `canAccessTenant`, 고객사 ADMIN은 자기 고객사만.
 * 대상의 고객을 모르면(null) 아니다.
 */
export function canManageUsersOf(user: UserAdminSubject | null | undefined, tenantId: string | null): boolean {
  if (!user || tenantId === null) return false;
  if (user.account_type === 'PLATFORM') return canAccessTenant(user, tenantId);
  return user.role === 'ADMIN' && user.tenant_id === tenantId;
}

/**
 * 실행 · 복구 · 종료를 누를 수 있는 계정(넓은 자격). 플랫폼 계정은 등급 · 배정(`hasCustomerScope`)으로, 고객사
 * 계정은 역할(ADMIN · OPERATOR)로 판정한다. 계정을 아직 모르면(null) 누를 수 없다. 인시던트는 고객 ID를 싣지 않아
 * 대상별 판정은 서버가 한다(범위 밖이면 403 · 404).
 */
export function canOperate(user: AccessSubject | null | undefined): boolean {
  if (!user) return false;
  if (user.account_type === 'PLATFORM') return hasCustomerScope(user);
  return user.role === 'ADMIN' || user.role === 'OPERATOR';
}

/** 조치 버튼을 막는 이유. 누를 수 있으면 null. */
export function operateBlockedReason(user: AccessSubject | null | undefined): string | null {
  if (canOperate(user)) return null;
  if (!user) return ROLE_PENDING;
  if (user.account_type === 'PLATFORM') return isUnassignedManager(user) ? UNASSIGNED_ACTION_BLOCKED : ACTION_FORBIDDEN;
  return VIEWER_ACTION_BLOCKED;
}
