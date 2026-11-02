// 메인 대시보드. 플랫폼 운영자에게는 고객 전체의 운영 현황(수동 처리가 필요한 문제 · 고객 연결 상태 · 고객사별 관제 직원)을 보인다(2026-10-29 초안 ·
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
import { ErrorState } from '@/components/error-state';
import { orNull, serverApi } from '@/lib/api/server';
import { businessOpen } from '@/lib/home-path';
import { isUnassignedManager } from '@/lib/roles';
import { unstable_rethrow } from 'next/navigation';

export default async function DashboardPage() {
  // 제목 옆 `마지막 갱신` — 다시 그려질 때마다 새로 찍힌다(auto-refresh.tsx).
  const renderedAt = new Date().toISOString();
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getMe, getTenants, getUsers, getIncidents } = await serverApi();
  // 고객 범위가 없는 계정(고객사 계정 · 담당 고객 없는 고객 담당자)에는 업무 데이터를 읽지 않고 이용 제한을 알린다
  // (lib/home-path.ts `businessOpen`). 담당 고객이 없는 담당자에게는 배정 없음을 따로 말한다. 고객 담당자가 받는
  // 데이터의 담당 범위는 서버가 정한다. 확인 실패도 업무 조회로 넘어가지 않는다.
  let user;
  try {
    user = await getMe();
  } catch (error) {
    unstable_rethrow(error);
    return <ErrorState error={error} />;
  }
  if (!businessOpen(user)) return <BusinessUnavailable unassigned={isUnassignedManager(user)} />;

  const usersPromise = orNull(getUsers());
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

  return (
    <AdminDashboardView
      tenants={tenants.tenants}
      users={users === null ? null : users.items}
      incidents={incidents === null ? null : incidents.items}
      renderedAt={renderedAt}
    />
  );
}
