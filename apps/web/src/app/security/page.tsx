// SEC-001 보안 관제 — 자산 관제(AST-001, `app/assets/page.tsx`)와 같은 틀이다(2026-09-28).
//
// 조회 3종의 실패 처리도 그 화면과 같다 — 자산 실패는 화면 전체 CMN-002, 인시던트·시계열 실패는
// 그것을 쓰는 패널만 "조회 실패"로 그린다(null = 조회 실패, 0건과 구분). 응답을 캐시하지 않아
// 새로고침·실시간 이벤트의 `router.refresh()`가 곧 재조회다.

import { ErrorState } from '@/components/error-state';
import { SecurityView } from '@/components/security/security-view';
import { getAssets, getIncidents, getMetricsTimeseries } from '@/lib/api/client';
import { parsePreset } from '@/lib/incident-filter';
import type { IncidentListItem } from '@/types/api';

export default async function SecurityPage({ searchParams }: PageProps<'/security'>) {
  // `?tab=incidents`·`?preset=`은 구 보안 인시던트 목록(`/incidents`)에서 redirect돼 오는 딥링크다.
  const { asset, tab, preset } = await searchParams;
  // 오류를 버리지 않고 들고 간다 — 인시던트 탭이 그것을 **대기 0건이 아니라 오류로** 그려야 한다.
  const incidentsPromise = getIncidents().then(
    (res) => ({ items: res.items, error: null as unknown }),
    (error: unknown) => ({ items: null, error }),
  );
  // 시계열은 개방 SG 추이와 상세 Drawer의 스파크라인이 쓴다 — 실패해도 화면은 뜬다.
  const metricsPromise = getMetricsTimeseries().then(
    (res) => res,
    () => null,
  );

  let assets;
  try {
    assets = await getAssets();
  } catch (error) {
    return <ErrorState error={error} />;
  }
  const listed = await incidentsPromise;
  const incidents = listed.items;
  const metrics = await metricsPromise;

  // 자산 관제와 같은 역조인 — Drawer와 카드의 인시던트 건수가 쓴다.
  let incidentsByArn: Record<string, IncidentListItem[]> | null = null;
  if (incidents !== null) {
    incidentsByArn = {};
    for (const incident of incidents) {
      (incidentsByArn[incident.subject_arn] ??= []).push(incident);
    }
  }

  // 화면 이름(h1)은 뷰 안에 있다 — 지표 띠와 한 덩이로 스크롤을 따라와야 해서다(security-view.tsx).
  return (
    <>
      <SecurityView
        data={assets}
        incidents={incidents}
        incidentsError={listed.error}
        incidentsByArn={incidentsByArn}
        metrics={metrics}
        openArn={typeof asset === 'string' ? asset : undefined}
        initialTab={typeof tab === 'string' ? tab : undefined}
        initialPreset={parsePreset(preset)}
      />
    </>
  );
}
