// 상단바 계정 정보(components/auth/user-menu.tsx)에 보일 현재 계정 항목. `/me` 값만 쓴다 —
// 고객 이름은 `/me` 계약에 없어 만들지 않는다. 플랫폼 계정은 소속 고객이 없어 고객 ID 항목이 없다.
// 플랫폼 계정의 역할은 `platform_role`로 보인다 — 호환 값 `role=ADMIN`을 "관리자"로 보이지 않는다. 플랫폼 계정에는
// 고객 범위(중앙 관리자는 전체 · 고객 담당자는 담당 고객 수 또는 배정 없음)를 함께 보인다. 담당 고객 ID는 나열하지 않는다.

import type { AccountScope, AccountType, UserRole } from '../types/api.ts';
import { isCentralAdmin, type PlatformSubject } from './roles.ts';
import { PLATFORM_ROLE_LABELS, ROLE_LABELS } from './user-admin.ts';

export const ACCOUNT_TYPE_LABELS: Record<AccountType, string> = {
  PLATFORM: '플랫폼 운영자',
  TENANT: '고객사',
};

export interface AccountField {
  label: string;
  value: string;
  /** 식별자(고객 ID)는 고정폭으로 그린다. */
  mono?: boolean;
}

/** 플랫폼 계정의 고객 범위 문구. 빈 배정 목록은 등급으로 가른다 — 중앙 관리자는 전체, 고객 담당자는 배정 없음. */
function customerScopeLabel(user: PlatformSubject): string {
  if (isCentralAdmin(user)) return '전체 고객';
  const count = user.assigned_tenant_ids.length;
  return count === 0 ? '배정 없음' : `담당 고객 ${count}곳`;
}

export function accountFields(
  user: AccountScope & { email: string; role: UserRole; assigned_tenant_ids: string[] },
): AccountField[] {
  const fields: AccountField[] = [
    { label: '이메일', value: user.email },
    {
      label: '역할',
      value: user.account_type === 'PLATFORM' ? PLATFORM_ROLE_LABELS[user.platform_role] : ROLE_LABELS[user.role],
    },
    { label: '계정 종류', value: ACCOUNT_TYPE_LABELS[user.account_type] },
  ];
  if (user.account_type === 'PLATFORM') fields.push({ label: '고객 범위', value: customerScopeLabel(user) });
  if (user.account_type === 'TENANT') fields.push({ label: '고객 ID', value: user.tenant_id, mono: true });
  return fields;
}
