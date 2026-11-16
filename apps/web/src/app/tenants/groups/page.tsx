// 고객사 그룹(#572 2번). 그룹을 만들고 지우는 것은 중앙 관리계정만 한다(서버 403). 다른 계정은 고객사 목록으로 보낸다.

import type { Metadata } from 'next';
import { redirect, unstable_rethrow } from 'next/navigation';

import { ErrorState } from '@/components/error-state';
import { TenantGroupsView } from '@/components/tenants/tenant-groups-view';
import { orNull, serverApi } from '@/lib/api/server';
import { isCentralAdmin } from '@/lib/roles';

export const metadata: Metadata = { title: '고객사 그룹 · Vigilantis' };

export default async function TenantGroupsPage() {
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getMe, getTenants, listGroups } = await serverApi();
  let user;
  try {
    user = await getMe();
  } catch (error) {
    unstable_rethrow(error);
    return <ErrorState error={error} />;
  }
  if (!isCentralAdmin(user)) redirect('/tenants');

  // 고객사 목록은 배정 표를 그리는 데 쓴다. 못 읽으면(null) 그룹 목록은 서버가 센 수로 그리고, 배정 표 자리에
  // 조회 실패를 적는다(tenant-groups-view.tsx).
  const tenantsPromise = orNull(getTenants());
  let groups;
  try {
    groups = await listGroups();
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }
  const tenants = await tenantsPromise;

  return <TenantGroupsView groups={groups.items} tenants={tenants?.tenants ?? null} />;
}
