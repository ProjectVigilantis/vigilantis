// AST-001 자산 관제 — 화면설계서 v1.5 §4.2.

import { AssetsView } from '@/components/assets/assets-view';
import { ErrorState } from '@/components/error-state';
import { getAssets, getIncident, getIncidents, getMetricsTimeseries } from '@/lib/api/client';
import { WASTE_VERDICTS } from '@/lib/dashboard';
import { savingsSummary } from '@/lib/savings';
import type { AssetItem, IncidentListItem, IncidentResponse } from '@/types/api';

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
 * `?asset=<arn>`은 INC-002 대상 자산에서 넘어오는 딥링크다(§4.5 액션). AST-002는 Drawer라
 * 자체 URL이 없어, 목록 화면이 그 항목을 고른 상태로 열어 준다.
 */
export default async function AssetsPage({ searchParams }: PageProps<'/assets'>) {
  const { asset } = await searchParams;
  const incidentsPromise = getIncidents().catch(() => null);
  // 시계열 실패는 화면을 죽이지 않는다 — 추이는 현재 상태를 읽는 데 필요한 것이 아니라 그
  // 옆에 붙는 맥락이다(대시보드와 같은 규칙). 자산 조회와 나란히 시작해 CloudWatch 왕복이
  // 첫 화면을 늦추지 않게 한다.
  const metricsPromise = getMetricsTimeseries().then(
    (res) => res,
    () => null,
  );

  let assets;
  try {
    assets = await getAssets();
  } catch (error) {
    // 자산 실패는 화면 전체 CMN-002다 — 대시보드(app/page.tsx)와 같은 규칙.
    // 잡지 않고 던지면 레이아웃(GNB)째 전역 오류 셸로 넘어가 다른 화면으로 빠져나갈 길이 없어진다.
    return <ErrorState error={error} />;
  }
  const incidents = await incidentsPromise;

  let incidentsByArn: Record<string, IncidentListItem[]> | null = null;
  if (incidents !== null) {
    incidentsByArn = {};
    for (const incident of incidents.items) {
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
      savingsTargets(incidents.items, assets.items).map((i) =>
        getIncident(i.incident_id).then(
          (res) => res,
          () => null,
        ),
      ),
    );
    for (const one of settled) {
      if (one === null) savingsFailed += 1;
      else details.push(one);
    }
  }

  const metrics = await metricsPromise;

  return (
    <>
      <h1 className="mb-4 text-lg font-semibold">자산 관제</h1>
      <AssetsView
        data={assets}
        incidentsByArn={incidentsByArn}
        metrics={metrics}
        savings={savingsSummary(details, assets.items)}
        savingsFailed={savingsFailed}
        openArn={typeof asset === 'string' ? asset : undefined}
      />
    </>
  );
}
