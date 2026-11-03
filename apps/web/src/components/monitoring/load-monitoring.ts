// 통합 관제(MON-001)의 서버 조회. 관제 화면(app/monitoring/page.tsx)과 고객사 상세(app/tenants/[id]/page.tsx)가
// 같은 조회를 쓴다. 고객사 상세는 서버에 고객사별 조회 범위(T-1)가 없어 받은 뒤 그 고객사 AWS 계정으로 거른다
// (lib/monitoring-scope.ts).
//
// 자산 조회 실패만 화면 오류다(대시보드와 같은 규칙). 인시던트 · 시계열 · 고객 목록 실패는 그 자리만 비운다.

import type { ComponentProps } from 'react';

import type { MonitoringView } from '@/components/monitoring/monitoring-view';
import { orError, orNull, serverApi } from '@/lib/api/server';
import { WASTE_VERDICTS } from '@/lib/dashboard';
import { scopeAssets, scopeIncidents, scopeMetrics } from '@/lib/monitoring-scope';
import { savingsSummary } from '@/lib/savings';
import type { AssetItem, IncidentListItem, IncidentResponse } from '@/types/api';

type ReadApi = Awaited<ReturnType<typeof serverApi>>;
/** 조회로 채우는 몫. 축 · 딥링크 · 끼워 넣기 여부는 부르는 쪽이 정한다. */
export type MonitoringData = Pick<
  ComponentProps<typeof MonitoringView>,
  'tenants' | 'data' | 'incidents' | 'incidentsError' | 'incidentsByArn' | 'metrics' | 'savings' | 'savingsFailed' | 'renderedAt'
>;

/**
 * 절감 예상을 위해 더 부를 인시던트 상세의 상한. 목록 계약에 `recommendations`가 없어
 * **상세를 건건이 더 불러야** 금액을 알 수 있는데(§4.2 API), 낭비 후보가 수십 건인 계정에서
 * 그만큼 왕복하면 자산 화면이 그 대기만큼 늦어진다. 큰 금액이 위에 오는 카드라 상위 몇 건으로
 * 충분하고, 잘려 나간 건수는 화면이 따로 적지 않는다 — 이 카드는 합계가 아니라 **순위**를 본다.
 */
const SAVINGS_DETAIL_LIMIT = 8;

/** 절감 예상을 물어볼 인시던트 — 낭비 후보로 판정된 자산에 걸린 것만, 최근 갱신 순으로 상한까지. */
function savingsTargets(
  incidents: readonly IncidentListItem[],
  assets: readonly AssetItem[],
): IncidentListItem[] {
  const wasteArns = new Set(
    assets.filter((a) => WASTE_VERDICTS.some((v) => v === a.verdict)).map((a) => a.arn),
  );
  return incidents
    .filter((i) => wasteArns.has(i.subject_arn))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, SAVINGS_DETAIL_LIMIT);
}

/**
 * 조회는 재분석하지 않는 계약이라 화면에 "재분석"이 없다(§6.1). 대신 응답을 캐시하지 않아
 * 새로고침이 곧 재조회다 — Next 16의 fetch는 기본 무캐시다.
 *
 * 인시던트는 자산 계약에 없는 값이라 목록 API를 한 번 더 부른다(`subject_arn` 역조인, §4.2·§4.3).
 * 실패해도 자산 화면은 떠야 하므로 역조인 결과만 비우되, **실패와 0건은 구분해서** 넘긴다(null = 조회 실패).
 * 합치면 관제 화면이 "연결된 인시던트 없음"으로 조회 실패를 덮어버린다(PR #137 리뷰).
 *
 * 자산 조회가 실패하면 그 오류를 던진다. 부르는 쪽이 잡아 화면 오류(CMN-002)로 그린다.
 * `accountId`를 주면 그 AWS 계정 몫만 남긴다(고객사 상세). 절감 예상도 거른 뒤의 자산 · 인시던트로 셈한다.
 */
export async function loadMonitoring(
  api: ReadApi,
  { accountId, withTenants = true }: { accountId?: string; withTenants?: boolean } = {},
): Promise<MonitoringData> {
  const { getAssets, getIncident, getIncidents, getMetricsTimeseries, getTenants } = api;
  // 제목 옆 `마지막 갱신` — 다시 그려질 때마다 새로 찍힌다(auto-refresh.tsx).
  const renderedAt = new Date().toISOString();
  // 고객 선택 목록 — 실패해도 화면은 뜬다(선택은 아직 잠겨 있다 · monitoring-view.tsx).
  // 고객사 계정은 부르지 않는다(`withTenants: false`) — 플랫폼 전용 목록(403)이고 고를 고객사도 없다(2026-11-03).
  const tenantsPromise = withTenants ? orNull(getTenants()) : Promise.resolve(undefined);
  // 오류를 버리지 않고 들고 간다 — 인시던트 탭이 그것을 **대기 0건이 아니라 오류로** 그려야 한다.
  // 401(로그인 redirect)만은 접지 않는다 — orError · orNull이 그대로 던진다.
  const incidentsPromise = orError(getIncidents());
  // 시계열 실패는 화면을 죽이지 않는다 — 추이는 현재 상태를 읽는 데 필요한 것이 아니라 그
  // 옆에 붙는 맥락이다(대시보드와 같은 규칙). 자산 조회와 나란히 시작해 CloudWatch 왕복이
  // 첫 화면을 늦추지 않게 한다.
  const metricsPromise = orNull(getMetricsTimeseries());

  const allAssets = await getAssets();
  const assets = accountId === undefined ? allAssets : scopeAssets(allAssets, accountId);
  const listed = await incidentsPromise;
  const allIncidents = listed.value === null ? null : listed.value.items;
  const incidents = allIncidents === null || accountId === undefined ? allIncidents : scopeIncidents(allIncidents, accountId);

  let incidentsByArn: Record<string, IncidentListItem[]> | null = null;
  if (incidents !== null) {
    incidentsByArn = {};
    for (const incident of incidents) {
      (incidentsByArn[incident.subject_arn] ??= []).push(incident);
    }
  }

  // 절감 예상(AI 추정)의 원천 — 금액은 **상세에만** 있어 상위 몇 건을 더 부른다.
  // 실패는 건별로 흘려보내되 **몇 건이 실패했는지는 세어 넘긴다** — 카드가 그것을 0원으로
  // 덮으면 합계가 "이만큼이 전부"로 읽힌다(#347의 실 청구액 오해와 같은 종류의 오류다).
  const details: IncidentResponse[] = [];
  let savingsFailed = 0;
  if (incidents !== null) {
    const settled = await Promise.all(
      savingsTargets(incidents, assets.items).map((i) => orNull(getIncident(i.incident_id))),
    );
    for (const one of settled) {
      if (one === null) savingsFailed += 1;
      else details.push(one);
    }
  }

  const allMetrics = await metricsPromise;
  const metrics = allMetrics === null || accountId === undefined ? allMetrics : scopeMetrics(allMetrics, accountId);
  const tenants = await tenantsPromise;

  return {
    tenants: tenants === undefined ? undefined : tenants === null ? null : tenants.tenants,
    data: assets,
    incidents,
    incidentsError: listed.error,
    incidentsByArn,
    metrics,
    savings: savingsSummary(details, assets.items),
    savingsFailed,
    renderedAt,
  };
}
