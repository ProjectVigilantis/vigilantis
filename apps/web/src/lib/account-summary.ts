// 상단바 계정 정보(components/auth/user-menu.tsx)에 보일 현재 계정 항목. `/me` 값만 쓴다 —
// 고객사 이름은 `/me` 계약에 없어 만들지 않는다. 관리자 · 관리자 직원은 소속 고객사가 없어 고객사 ID 항목이 없다.
// 용어는 lib/roles.ts `ACCOUNT_KIND_LABELS`(관리자 · 관리자 직원 · 고객사 직원)를 따른다.

import type { AccountScope, UserRole } from '../types/api.ts';
import { ACCOUNT_KIND_LABELS, accountKind } from './roles.ts';
import { ROLE_LABELS } from './user-admin.ts';

export interface AccountField {
  label: string;
  value: string;
  /** 식별자(고객사 ID)는 고정폭으로 그린다. */
  mono?: boolean;
}

export function accountFields(user: AccountScope & { email: string; role: UserRole }): AccountField[] {
  const fields: AccountField[] = [
    { label: '이메일', value: user.email },
    { label: '계정 종류', value: ACCOUNT_KIND_LABELS[accountKind(user)] },
  ];
  // 권한(직원 관리 · 조치 · 조회 전용)은 고객사 직원 안에서 나뉘는 값이다. 관리자 계정에는 붙이지 않는다.
  if (user.account_type === 'TENANT') {
    fields.push({ label: '권한', value: ROLE_LABELS[user.role] });
    fields.push({ label: '고객사 ID', value: user.tenant_id, mono: true });
  }
  return fields;
}
