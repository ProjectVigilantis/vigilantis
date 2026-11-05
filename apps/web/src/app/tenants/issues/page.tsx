// 수동 처리가 필요한 문제 전체 목록. 관리자 대시보드의 "수동 처리 필요" 숫자 · 그래프에서 넘어온다(2026-10-29 —
// 대시보드는 숫자와 그래프만 두고 한 건 한 줄 표는 이 화면으로 옮겼다). 셈은 lib/admin-dashboard.ts가 대시보드와 같이 한다.
//
// 고객 목록 실패는 화면 전체 CMN-002, 인시던트 목록 실패는 진행 불가 인시던트만 빠지고 그 사실을 적는다(대시보드와 같다).

import type { Metadata } from 'next';
import { redirect, unstable_rethrow } from 'next/navigation';

import { DetailHeader } from '@/components/admin-dashboard/detail-header';
import { IssuesTable } from '@/components/admin-dashboard/issues-table';
import { BusinessUnavailable } from '@/components/business-unavailable';
import { ErrorState } from '@/components/error-state';
import { issueRows, manualIssues } from '@/lib/admin-dashboard';
import { orNull, serverApi } from '@/lib/api/server';
import { businessOpen } from '@/lib/home-path';
import { isPlatform, isUnassignedManager } from '@/lib/roles';

export const metadata: Metadata = { title: '수동 처리가 필요한 문제 · Vigilantis' };

export default async function IssuesPage() {
  const renderedAt = new Date().toISOString();
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getMe, getTenants, getIncidents } = await serverApi();
  // 대시보드(app/page.tsx)와 같은 관문: 고객 범위가 없는 계정(고객사 계정 · 담당 고객사 없는 관리자 직원계정)에는
  // 데이터를 읽지 않고 이용 제한을 알린다. 계정 확인 실패도 조회로 넘어가지 않는다.
  let user;
  try {
    user = await getMe();
  } catch (error) {
    unstable_rethrow(error);
    return <ErrorState error={error} />;
  }
  // 관리자 화면이다. 고객사 계정은 업무 화면이 열려 있어도 여기 오지 않는다(고객사 대시보드로 보낸다, 2026-11-03).
  if (!isPlatform(user)) redirect('/');
  if (!businessOpen(user)) return <BusinessUnavailable unassigned={isUnassignedManager(user)} />;
  const incidentsPromise = orNull(getIncidents());
  let tenants;
  try {
    tenants = (await getTenants()).tenants;
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }
  const incidents = await incidentsPromise;
  const rows = issueRows(manualIssues(tenants, incidents?.items ?? null), tenants);

  return (
    <div className="flex flex-col gap-6">
      <DetailHeader
        title="수동 처리가 필요한 문제"
        renderedAt={renderedAt}
      />
      <section className="bg-card ring-foreground/10 rounded-xl p-6 ring-1">
        <IssuesTable rows={rows} nowMs={Date.parse(renderedAt)} />
        {incidents === null ? (
          <p role="status" className="text-muted-foreground mt-3 text-xs">
            인시던트 목록을 불러오지 못해 진행 불가 인시던트는 빠졌습니다.
          </p>
        ) : null}
      </section>
    </div>
  );
}
