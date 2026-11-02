// 상단바 계정 정보(components/auth/user-menu.tsx)에 보일 현재 계정 항목. `/me` 값만 쓴다 —
// 고객사 이름은 `/me` 계약에 없어 만들지 않는다. 중앙 관리계정 · 관리자 직원계정은 소속 고객사가 없어 고객사 ID 항목이
// 없다. 계정 종류는 lib/roles.ts `ACCOUNT_KIND_LABELS`(용어 4종, 2026-11-02)를 따른다. 플랫폼 계정은 `platform_role`로
// 가르고(호환 값 `role=ADMIN`으로 가르지 않는다) 고객사 범위(중앙 관리계정은 전체 · 관리자 직원계정은 담당 고객사 수
// 또는 배정 없음)를 함께 보인다. 담당 고객사 ID는 나열하지 않는다.

import type { AccountScope, UserRole } from '../types/api.ts';
import { ACCOUNT_KIND_LABELS, accountKind, isCentralAdmin, type PlatformSubject } from './roles.ts';
import { ROLE_LABELS } from './user-admin.ts';

export interface AccountField {
  label: string;
  value: string;
  /** 식별자(고객사 ID)는 고정폭으로 그린다. */
  mono?: boolean;
}

/** 플랫폼 계정의 고객 범위 문구. 빈 배정 목록은 등급으로 가른다 — 중앙 관리자는 전체, 고객 담당자는 배정 없음. */
function customerScopeLabel(user: PlatformSubject): string {
  if (isCentralAdmin(user)) return '전체 고객사';
  const count = user.assigned_tenant_ids.length;
  return count === 0 ? '배정 없음' : `담당 고객사 ${count}곳`;
}

export function accountFields(
  user: AccountScope & { email: string; role: UserRole; assigned_tenant_ids: string[] },
): AccountField[] {
  const fields: AccountField[] = [
    { label: '이메일', value: user.email },
    { label: '계정 종류', value: ACCOUNT_KIND_LABELS[accountKind(user)] },
  ];
  if (user.account_type === 'PLATFORM') fields.push({ label: '고객사 범위', value: customerScopeLabel(user) });
  // 권한(직원 관리 · 조치 · 조회 전용)은 고객사 직원 안에서 나뉘는 값이다. 관리자 계정에는 붙이지 않는다.
  if (user.account_type === 'TENANT') {
    fields.push({ label: '권한', value: ROLE_LABELS[user.role] });
    fields.push({ label: '고객사 ID', value: user.tenant_id, mono: true });
  }
  return fields;
}
