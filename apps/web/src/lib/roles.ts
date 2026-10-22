// 역할별 조치 가능 여부(#492 PR 2). **화면 편의일 뿐이다** — 실제 차단은 서버의 403 `AUTH_FORBIDDEN`이다.

import type { UserRole } from '../types/api.ts';

/** 서버 403 `AUTH_FORBIDDEN`과 조회 전용 계정의 비활성 버튼이 함께 쓰는 문구. */
export const VIEWER_ACTION_BLOCKED = '조회 전용 계정은 조치할 수 없습니다.';
const ROLE_PENDING = '권한을 확인한 뒤 조치할 수 있습니다.';

/** 실행 · 복구 · 종료를 누를 수 있는 역할. 역할을 아직 모르면(null) 누를 수 없다. */
export function canOperate(role: UserRole | null | undefined): boolean {
  return role === 'ADMIN' || role === 'OPERATOR';
}

/** 사용자 관리 메뉴를 보일 역할(#492 PR 3). 메뉴 노출만 정한다 — 실제 차단은 서버 403이다. */
export function canManageUsers(role: UserRole | null | undefined): boolean {
  return role === 'ADMIN';
}

/** 조치 버튼을 막는 이유. 누를 수 있으면 null. */
export function operateBlockedReason(role: UserRole | null | undefined): string | null {
  if (canOperate(role)) return null;
  return role === null || role === undefined ? ROLE_PENDING : VIEWER_ACTION_BLOCKED;
}
