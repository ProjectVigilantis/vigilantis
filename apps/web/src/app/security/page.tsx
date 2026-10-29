// SEC-001 보안 관제 — 자산 관제(AST-001, `app/assets/page.tsx`)와 같은 틀이다(2026-09-28).
//
// 조회 3종의 실패 처리도 그 화면과 같다 — 자산 실패는 화면 전체 CMN-002, 인시던트·시계열 실패는
// 그것을 쓰는 패널만 "조회 실패"로 그린다(null = 조회 실패, 0건과 구분). 응답을 캐시하지 않아
// 새로고침·실시간 이벤트의 `router.refresh()`가 곧 재조회다.

import { BusinessUnavailable } from '@/components/business-unavailable';
import { ErrorState } from '@/components/error-state';
import { SecurityView } from '@/components/security/security-view';
import { orError, orNull, serverApi } from '@/lib/api/server';
import { parsePreset } from '@/lib/incident-filter';
import { isUnassignedManager } from '@/lib/roles';
import type { IncidentListItem } from '@/types/api';
import { unstable_rethrow } from 'next/navigation';

export default async function SecurityPage({ searchParams }: PageProps<'/security'>) {
  // 제목 옆 `마지막 갱신` — 다시 그려질 때마다 새로 찍힌다(auto-refresh.tsx).
  const renderedAt = new Date().toISOString();
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getMe, getAssets, getIncidents, getMetricsTimeseries } = await serverApi();
  // 담당 고객이 없는 고객 담당자는 업무 데이터를 하나도 읽지 않고 배정 없음을 본다 — 자산 관제와 같은 규칙이다.
  // 계정 확인 실패도 업무 조회로 넘어가지 않는다. 고객사 계정은 기존대로 조회하고 서버의 거절(403)을 오류 화면으로 본다.
  let user;
  try {
    user = await getMe();
  } catch (error) {
    unstable_rethrow(error);
    return <ErrorState error={error} />;
  }
  if (isUnassignedManager(user)) return <BusinessUnavailable unassigned />;
  // `?tab=incidents`·`?preset=`은 구 보안 인시던트 목록(`/incidents`)에서 redirect돼 오는 딥링크다.
  const { asset, tab, preset } = await searchParams;
  // 오류를 버리지 않고 들고 간다 — 인시던트 탭이 그것을 **대기 0건이 아니라 오류로** 그려야 한다.
  // 401(로그인 redirect)만은 접지 않는다 — orError · orNull이 그대로 던진다.
  const incidentsPromise = orError(getIncidents());
  // 시계열은 개방 SG 추이와 상세 Drawer의 스파크라인이 쓴다 — 실패해도 화면은 뜬다.
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
        renderedAt={renderedAt}
      />
    </>
  );
}
