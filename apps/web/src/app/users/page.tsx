// 사용자 관리(#492) — 플랫폼 운영자 · 고객사 ADMIN 전용. GNB 메뉴는 그 계정에게만 보이지만, 실제 차단은 서버 403
// `AUTH_FORBIDDEN`이다(주소를 직접 쳐서 들어온 고객사 OPERATOR · VIEWER도 여기서 403 안내를 본다).

import type { Metadata } from 'next';
import { unstable_rethrow } from 'next/navigation';

import { ErrorState } from '@/components/error-state';
import { UsersView } from '@/components/users/users-view';
import { ApiError } from '@/lib/api/client';
import { orNull, serverApi } from '@/lib/api/server';
import { USER_ADMIN_FORBIDDEN } from '@/lib/user-admin';

export const metadata: Metadata = { title: '계정 관리 · Vigilantis' };

export default async function UsersPage({ searchParams }: PageProps<'/users'>) {
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getUsers, getTenant } = await serverApi();
  // `?tenant=<id>`는 고객 상세의 사용자 패널에서 넘어오는 딥링크다(`invite=1`이면 그 고객으로 초대 창을 연다).
  // 목록 API에 고객 필터가 없어 화면이 거른다. 고객 이름은 표시용이라 못 읽으면 ID로 적는다(고객사 ADMIN은 403).
  const { tenant: tenantParam, invite } = await searchParams;
  const tenantId = typeof tenantParam === 'string' && tenantParam !== '' ? tenantParam : null;
  const tenantPromise = tenantId === null ? null : orNull(getTenant(tenantId));
  let users;
  try {
    users = await getUsers();
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    if (error instanceof ApiError && error.code === 'AUTH_FORBIDDEN') {
      return (
        <div className="flex flex-col gap-4">
          <h1 className="text-lg font-semibold">계정 관리</h1>
          <p role="alert" className="rounded-lg border p-4 text-sm text-muted-foreground">
            {USER_ADMIN_FORBIDDEN}
          </p>
        </div>
      );
    }
    return <ErrorState error={error} />;
  }
  const tenant = tenantPromise === null ? null : await tenantPromise;
  return (
    <UsersView
      users={users.items}
      tenantFilter={tenantId === null ? null : { id: tenantId, name: tenant?.name ?? null }}
      inviteOnOpen={tenantId !== null && invite === '1'}
    />
  );
}
