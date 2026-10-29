// 고객사별 직원 표. 관리자 대시보드의 "고객사 직원" 숫자 · 그래프에서 넘어온다(2026-10-29 — 대시보드는 숫자와 그래프만 두고
// 고객사 한 곳 한 줄 표는 이 화면으로 옮겼다). 직원 한 명씩 고치는 일은 계정 관리(`/users`)가 한다.

import type { Metadata } from 'next';
import { unstable_rethrow } from 'next/navigation';

import { DetailHeader } from '@/components/admin-dashboard/detail-header';
import { StaffTable } from '@/components/admin-dashboard/staff-table';
import { ErrorState } from '@/components/error-state';
import { staffRows } from '@/lib/admin-dashboard';
import { serverApi } from '@/lib/api/server';

export const metadata: Metadata = { title: '고객사별 직원 · Vigilantis' };

export default async function TenantStaffPage() {
  const renderedAt = new Date().toISOString();
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getTenants, getUsers } = await serverApi();
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
        title="고객사별 직원"
        description="고객사마다 Vigilantis에 로그인하는 직원 계정입니다. 줄을 펼치면 이메일이 보이고, 고객사 이름을 누르면 그 고객사 직원을 관리합니다."
        renderedAt={renderedAt}
      />
      <section className="bg-card ring-foreground/10 rounded-xl p-6 ring-1">
        <StaffTable rows={staffRows(tenants.tenants, users.items)} nowMs={Date.parse(renderedAt)} />
      </section>
    </div>
  );
}
