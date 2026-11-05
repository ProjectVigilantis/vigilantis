// 고객 상세(Post-MVP T-4). 없는 고객이면 TENANT_NOT_FOUND 전체 오류 화면이다.

import { unstable_rethrow } from 'next/navigation';

import type { ReactNode } from 'react';

import { ErrorState } from '@/components/error-state';
import { loadMonitoring } from '@/components/monitoring/load-monitoring';
import { MonitoringView } from '@/components/monitoring/monitoring-view';
import { TenantDetailView } from '@/components/tenants/tenant-detail-view';
import { orNull, serverApi } from '@/lib/api/server';
import { parseAxis } from '@/lib/home-path';

export default async function TenantPage({ params, searchParams }: PageProps<'/tenants/[id]'>) {
  const { id } = await params;
  // 관제 축 전환이 주소에 `?axis=`를 적는다(monitoring-view.tsx). 새로고침해도 같은 축으로 연다.
  const { axis } = await searchParams;
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const api = await serverApi();
  const { getTenant, getUsers } = api;
  // 사용자 패널의 원천. 목록 API에 고객 필터가 없어 전량을 받아 이 고객 것만 고른다. 실패해도 상세는 뜬다(null = 조회 실패).
  const usersPromise = orNull(getUsers());
  let tenant;
  try {
    tenant = await getTenant(id);
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }
  const users = await usersPromise;

  // 관제(자산 · 보안)를 상세 안에서 바로 본다(2026-11-02). 이 고객사 AWS 계정 몫만 남긴다. 연결 전에는 수집한 것이 없어 부르지 않는다.
  // 자산 조회가 실패해도 상세는 뜨고 관제 자리만 오류다.
  let monitoring: ReactNode = null;
  if (tenant.status !== 'PENDING_CONNECTION') {
    let loaded;
    try {
      loaded = await loadMonitoring(api, { accountId: tenant.aws_account_id });
    } catch (error) {
      unstable_rethrow(error);
      monitoring = <ErrorState error={error} variant="inline" />;
    }
    if (loaded !== undefined) monitoring = <MonitoringView {...loaded} initialAxis={parseAxis(axis)} embedded />;
  }

  return (
    <TenantDetailView
      tenant={tenant}
      monitoring={monitoring}
      users={users === null ? null : users.items.filter((u) => u.tenant_id === tenant.tenant_id)}
    />
  );
}
