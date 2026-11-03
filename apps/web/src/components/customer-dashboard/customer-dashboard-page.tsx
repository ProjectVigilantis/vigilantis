// 고객사 계정의 대시보드(`/`, 2026-11-03 — 종전 `/preview/dashboard` 미리보기를 옮겼다). 화면 본문은
// customer-dashboard-view.tsx다. 자산 · 인시던트는 서버가 자기 고객사 몫만 돌려준다.
// 인시던트 · 시계열 실패는 화면을 죽이지 않는다. 자산 실패만 화면 전체 오류다.

import { unstable_rethrow } from 'next/navigation';

import { CustomerDashboardView } from '@/components/customer-dashboard/customer-dashboard-view';
import { ErrorState } from '@/components/error-state';
import { ApiError } from '@/lib/api/client';
import { orError, orNull, type serverApi } from '@/lib/api/server';
import type { CostLoad } from '@/lib/asset-costs';
import { actionQueue, proposalRows } from '@/lib/dashboard';
import { notYetServed } from '@/lib/support';
import type { IncidentResponse } from '@/types/api';

type ReadApi = Awaited<ReturnType<typeof serverApi>>;

export async function CustomerDashboardPage({ api, renderedAt }: { api: ReadApi; renderedAt: string }) {
  const { getAssetCosts, getAssets, getIncident, getIncidents, getMetricsTimeseries } = api;
  const incidentsPromise = orError(getIncidents());
  const metricsPromise = orNull(getMetricsTimeseries());
  const costsPromise = orError(getAssetCosts());

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
  // 유형별 비용은 FE 제안 계약이다(lib/asset-costs.ts). 서버에 없으면 404라 "missing"으로 넘긴다.
  const costResult = await costsPromise;
  const costs: CostLoad =
    costResult.value !== null
      ? { kind: 'ready', data: costResult.value }
      : costResult.error instanceof ApiError && notYetServed(costResult.error.httpStatus)
        ? { kind: 'missing' }
        : { kind: 'failed' };

  // 비용 건의 절감액은 상세에만 있어 그 건들만 나란히 부른다. 실패한 건은 "추정 없음"이 된다.
  const queue = actionQueue(incidents);
  const details = new Map<string, IncidentResponse>();
  if (queue !== null) {
    const settled = await Promise.all(
      queue.filter((i) => i.category === 'FINOPS').map((i) => orNull(getIncident(i.incident_id))),
    );
    for (const detail of settled) if (detail !== null) details.set(detail.incident_id, detail);
  }

  const rows = queue === null ? null : proposalRows(queue, details);

  // 제목 · 새로고침은 관제 상태 알림과 한 줄이라 뷰 안에 있다.
  return (
    <CustomerDashboardView
      assets={assets}
      rows={rows}
      metrics={metrics}
      costs={costs}
      renderedAt={renderedAt}
    />
  );
}
