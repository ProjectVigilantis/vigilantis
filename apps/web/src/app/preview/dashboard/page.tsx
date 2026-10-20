// 고객사 대시보드 미리보기. 지금 `/` 대시보드는 그대로 두고, 새 구성을 이 주소에서만 보여 준다.
// 확정되면 `/`로 옮기고 이 경로는 지운다. 조회 방식과 실패 처리는 `/`(app/page.tsx)와 같다.

import Link from 'next/link';
import { unstable_rethrow } from 'next/navigation';

import { AutoRefresh } from '@/components/auto-refresh';
import { CustomerDashboardView } from '@/components/customer-dashboard/customer-dashboard-view';
import { ErrorState } from '@/components/error-state';
import { orError, orNull, serverApi } from '@/lib/api/server';
import { actionQueue, proposalRows } from '@/lib/dashboard';
import type { IncidentResponse } from '@/types/api';

export default async function CustomerDashboardPreviewPage() {
  const renderedAt = new Date().toISOString();
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getAssets, getIncident, getIncidents, getMetricsTimeseries } = await serverApi();

  // 인시던트 · 시계열 실패는 화면을 죽이지 않는다. 자산 실패만 화면 전체 오류다(`/`와 같은 규칙).
  const incidentsPromise = orError(getIncidents());
  const metricsPromise = orNull(getMetricsTimeseries());

  let assets;
  try {
    assets = await getAssets();
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }

  const listed = await incidentsPromise;
  const incidents = listed.value === null ? null : listed.value.items;
  const metrics = await metricsPromise;

  // 비용 건의 절감액은 상세에만 있어 그 건들만 나란히 부른다. 실패한 건은 "추정 없음"이 된다.
  const queue = actionQueue(incidents);
  const details = new Map<string, IncidentResponse>();
  if (queue !== null) {
    const settled = await Promise.all(
      queue.filter((i) => i.category === 'FINOPS').map((i) => orNull(getIncident(i.incident_id))),
    );
    for (const detail of settled) if (detail !== null) details.set(detail.incident_id, detail);
  }

  return (
    <div className="flex flex-col gap-6">
      <p className="border-primary/30 bg-accent text-accent-foreground rounded-lg border px-4 py-2.5 text-[13px]">
        새 대시보드 미리보기입니다. 지금 쓰는 대시보드(<Link href="/" className="font-semibold underline">/</Link>)는 바뀌지 않았습니다.
      </p>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">대시보드</h1>
        <AutoRefresh renderedAt={renderedAt} />
      </div>
      <CustomerDashboardView
        assets={assets}
        incidents={incidents}
        rows={queue === null ? null : proposalRows(queue, details)}
        // CMN-002를 서버에서 그려 넘긴다. 클라이언트 경계 너머로 오류 객체를 보내면 code · requestId가 사라진다.
        incidentsErrorSlot={listed.error === null ? null : <ErrorState error={listed.error} variant="inline" />}
        metrics={metrics}
        renderedAt={renderedAt}
      />
    </div>
  );
}
