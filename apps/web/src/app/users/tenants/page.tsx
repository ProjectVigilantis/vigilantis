// 고객사별 계정 표. 관리자 대시보드의 "고객사 계정" 숫자 · 그래프에서 넘어온다(2026-10-29 — 대시보드는 숫자와 그래프만 두고
// 고객사 한 곳 한 줄 표는 이 화면으로 옮겼다). 계정 하나씩 고치는 일은 계정 관리(`/users`)가 한다.

import type { Metadata } from 'next';
import { redirect, unstable_rethrow } from 'next/navigation';

import { DetailHeader } from '@/components/admin-dashboard/detail-header';
import { StaffTable } from '@/components/admin-dashboard/staff-table';
import { BusinessUnavailable } from '@/components/business-unavailable';
import { ErrorState } from '@/components/error-state';
import { staffRows } from '@/lib/admin-dashboard';
import { serverApi } from '@/lib/api/server';
import { businessOpen } from '@/lib/home-path';
import { canManageUsers, isPlatform, isUnassignedManager } from '@/lib/roles';

export const metadata: Metadata = { title: '고객사별 계정 · Vigilantis' };

export default async function TenantStaffPage() {
  const renderedAt = new Date().toISOString();
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getMe, getTenants, getUsers } = await serverApi();
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
  // 고객사 직원 목록(`GET /users`)은 중앙 관리계정만 읽는다(서버 #572 9번). 고객 담당자는 대시보드로 보낸다 —
  // 부르면 403이 활동 로그에 쌓인다(#589).
  if (!canManageUsers(user)) redirect('/');
  let tenants;
  let users;
  try {
    [tenants, users] = await Promise.all([getTenants(), getUsers()]);
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }

  return (
    <div className="flex flex-col gap-6">
      <DetailHeader
        title="고객사별 계정"
        renderedAt={renderedAt}
      />
      <section className="bg-card ring-foreground/10 rounded-xl p-6 ring-1">
        <StaffTable rows={staffRows(tenants.tenants, users.items)} nowMs={Date.parse(renderedAt)} />
      </section>
    </div>
  );
}
