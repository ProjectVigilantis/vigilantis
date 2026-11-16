// 로그(2026-11-03, 고객사 계정 메뉴). 누가 어떤 조치를 실행 · 종료했고, 어떤 위협과 인시던트가 들어왔는지 시각순으로 본다.
// 자산 이름 · 인시던트 목록 · 직원 권한은 기간과 무관해 여기서 한 번 읽고, 기간마다 달라지는 감사 기록 · 위협 이벤트는 화면이 읽는다
// (components/logs/logs-view.tsx). 자산 · 인시던트 조회가 실패해도 화면은 그린다. 이름 대신 ID가 보이고 인시던트 발생 줄이 빠진다.

import type { Metadata } from 'next';

import { LogsView } from '@/components/logs/logs-view';
import { orNull, serverApi } from '@/lib/api/server';
import { canManageUsers } from '@/lib/roles';
import { ROLE_LABELS } from '@/lib/user-admin';

export const metadata: Metadata = { title: '로그 · Vigilantis' };

export default async function LogsPage() {
  const { getAssets, getIncidents, getUsers, getMe } = await serverApi();
  // 계정 목록은 고객사 ADMIN만 읽는다. 읽으면 조치한 직원 옆에 권한을 붙인다. 그 밖의 계정은 부르지 않는다 —
  // 부르면 403이 활동 로그에 남고, 실시간 이벤트의 재조회마다 한 행씩 더 쌓인다(#589).
  const me = await orNull(getMe());
  const [assets, incidents, users] = await Promise.all([
    orNull(getAssets()),
    orNull(getIncidents()),
    canManageUsers(me) ? orNull(getUsers()) : null,
  ]);
  const assetNames = Object.fromEntries((assets?.items ?? []).map((a) => [a.arn, a.name ?? a.resource_id]));
  const staffRoles = Object.fromEntries(
    (users?.items ?? []).filter((u) => u.account_type === 'TENANT').map((u) => [u.email, ROLE_LABELS[u.role]]),
  );

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-bold tracking-tight">로그</h1>
      <LogsView assetNames={assetNames} incidents={incidents?.items ?? null} staffRoles={staffRoles} />
    </div>
  );
}
