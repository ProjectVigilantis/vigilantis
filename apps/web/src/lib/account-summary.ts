// 상단바 계정 정보(components/auth/user-menu.tsx)에 보일 현재 계정 항목. `/me` 값만 쓴다 —
// 고객 이름은 `/me` 계약에 없어 만들지 않는다. 플랫폼 계정은 소속 고객이 없어 고객 ID 항목이 없다.

import type { AccountScope, AccountType, UserRole } from '../types/api.ts';
import { ROLE_LABELS } from './user-admin.ts';

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

export function accountFields(user: AccountScope & { email: string; role: UserRole }): AccountField[] {
  const fields: AccountField[] = [
    { label: '이메일', value: user.email },
    { label: '역할', value: ROLE_LABELS[user.role] },
    { label: '계정 종류', value: ACCOUNT_TYPE_LABELS[user.account_type] },
  ];
  if (user.account_type === 'TENANT') fields.push({ label: '고객 ID', value: user.tenant_id, mono: true });
  return fields;
}
