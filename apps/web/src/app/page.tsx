// 메인 대시보드. 고객사 계정에게는 고객사 대시보드(components/customer-dashboard/customer-dashboard-page.tsx, 2026-11-03)를,
// 플랫폼 운영자에게는 고객 전체의 운영 현황(수동 처리가 필요한 문제 · 고객 연결 상태 · 고객사별 관제 직원)을 보인다(2026-10-29 초안 ·
// components/admin-dashboard/admin-dashboard-view.tsx). 종전 DSH-001(화면설계서 v1.6 §4.1 — 자산 띠 · 위협 추이 ·
// AI 조치 제안)은 고객 계정 하나를 보는 구성이라 운영자 화면에서 내렸다. 고객 하나를 들여다보는 일은 관제 화면이 한다.
//
// 조회는 반드시 `lib/api/client`를 거친다 — 오리진을 정하는 자리가 거기 하나(`apiBaseUrl()`)이고,
// 이 경로를 벗어나 상대 경로로 부르면 Next 서버가 자기 자신에게 물어 화면만 다른 곳을 본다(PR #299 리뷰 §2).
// 응답을 캐시하지 않아 새로고침·실시간 이벤트의 `router.refresh()`가 곧 재조회다.
//
// 고객 목록 실패는 화면 전체 CMN-002다(이 화면의 숫자가 전부 거기서 나온다). 사용자 · 인시던트 목록 실패는 그것을 쓰는
// 자리만 비우고 그 사실을 적는다.

import { AdminDashboardView } from '@/components/admin-dashboard/admin-dashboard-view';
import { BusinessUnavailable } from '@/components/business-unavailable';
import { CustomerDashboardPage } from '@/components/customer-dashboard/customer-dashboard-page';
import { ErrorState } from '@/components/error-state';
import { orNull, serverApi } from '@/lib/api/server';
import { businessOpen } from '@/lib/home-path';
import { canManageUsers, canViewPlatformStatus, isCentralAdmin, isPlatform, isUnassignedManager } from '@/lib/roles';
import { unstable_rethrow } from 'next/navigation';

export default async function DashboardPage() {
  // 제목 옆 `마지막 갱신` — 다시 그려질 때마다 새로 찍힌다(auto-refresh.tsx).
  const renderedAt = new Date().toISOString();
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const api = await serverApi();
  const { getMe, getTenants, getUsers, getIncidents, getPlatformOverview } = api;
  // 담당 고객이 없는 고객 담당자에게는 업무 데이터를 읽지 않고 배정 없음을 알린다(lib/home-path.ts `businessOpen`).
  // 고객사 계정은 고객사 대시보드를 본다(2026-11-03). 받는 데이터의 범위는 서버가 정한다.
  // 확인 실패도 업무 조회로 넘어가지 않는다.
  let user;
  try {
    user = await getMe();
  } catch (error) {
    unstable_rethrow(error);
    return <ErrorState error={error} />;
  }
  if (!businessOpen(user)) return <BusinessUnavailable unassigned={isUnassignedManager(user)} />;
  if (!isPlatform(user)) return <CustomerDashboardPage api={api} renderedAt={renderedAt} />;

  // 플랫폼 상태는 중앙 관리계정만 본다(lib/roles.ts `canViewPlatformStatus`, 서버 require_central_admin). 그 밖은 부르지 않는다 —
  // 부르면 403이 자동 갱신마다 활동 로그에 쌓인다(#589). 실패해도 대시보드는 뜬다.
  const platformPromise = canViewPlatformStatus(user) ? orNull(getPlatformOverview()) : undefined;
  // 고객사 직원 목록은 중앙 관리계정만 읽는다(`canManageUsers`). 고객 담당자는 부르지 않는다(undefined = 볼 수 없음) —
  // 부르면 자동 갱신 · 실시간 재조회마다 403이 활동 로그에 쌓인다(#589).
  const usersPromise = canManageUsers(user) ? orNull(getUsers()) : undefined;
  const incidentsPromise = orNull(getIncidents());
  let tenants;
  try {
    tenants = await getTenants();
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }
  const users = await usersPromise;
  const incidents = await incidentsPromise;
  const platform = await platformPromise;

  return (
    <AdminDashboardView
      tenants={tenants.tenants}
      users={users == null ? users : users.items}
      incidents={incidents === null ? null : incidents.items}
      renderedAt={renderedAt}
      platform={platform}
      showUnassigned={isCentralAdmin(user)}
    />
  );
}
