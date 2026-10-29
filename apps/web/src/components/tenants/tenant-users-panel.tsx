// 고객 상세의 사용자 패널: 이 고객에 속한 로그인 계정 수와 사용자 관리로 가는 바로가기다.
// 사용자는 고객 하나에 속하고(`tenant_id`), 고객사 사용자는 고객이 관제 중(ACTIVE)일 때만 초대한다(ADR-0012).
// 관리 자체(초대 · 역할 · 비활성화)는 사용자 관리 화면이 한다. 여기서는 세고 그 화면의 이 고객 몫으로 보낸다.
//
// 훅이 없어 `'use client'`를 붙이지 않는다.

import Link from 'next/link';

import { Panel } from '@/components/panel';
import { Muted } from '@/components/panel-parts';
import { Button } from '@/components/ui/button';
import { ROLE_LABELS } from '@/lib/user-admin';
import type { TenantStatus, UserListItem, UserRole } from '@/types/api';

const ROLES: readonly UserRole[] = ['ADMIN', 'OPERATOR', 'VIEWER'];

export function TenantUsersPanel({
  tenantId,
  status,
  users,
}: {
  tenantId: string;
  status: TenantStatus;
  /** 이 고객 소속 사용자. `null`은 조회 실패다(0명과 구분한다). */
  users: UserListItem[] | null;
}) {
  const listHref = `/users?tenant=${encodeURIComponent(tenantId)}`;
  const canInvite = status === 'ACTIVE';

  return (
    <Panel title="고객사 직원" description="이 고객사에서 Vigilantis에 로그인하는 직원 계정입니다.">
      {users === null ? (
        <Muted>직원 목록을 불러오지 못했습니다.</Muted>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-2xl font-bold">{users.length}명</span>
            {users.length > 0 ? (
              <span className="text-muted-foreground text-[13px]">
                {ROLES.map((r) => `${ROLE_LABELS[r]} ${users.filter((u) => u.role === r).length}`).join(' · ')}
                {users.some((u) => u.status === 'DISABLED')
                  ? ` · 비활성 ${users.filter((u) => u.status === 'DISABLED').length}`
                  : null}
              </span>
            ) : null}
          </div>
          {canInvite ? null : (
            <p className="text-muted-foreground text-[13px]">
              {status === 'SUSPENDED'
                ? '원격 회수 중에는 새 직원을 초대할 수 없습니다.'
                : '연결 확인을 마쳐 관제 중이 되면 이 고객사의 직원을 초대할 수 있습니다.'}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button asChild size="sm" variant="outline">
              <Link href={listHref}>계정 관리에서 보기</Link>
            </Button>
            {canInvite ? (
              <Button asChild size="sm" variant="outline">
                <Link href={`${listHref}&invite=1`}>이 고객사에 직원 초대</Link>
              </Button>
            ) : null}
          </div>
        </div>
      )}
    </Panel>
  );
}
