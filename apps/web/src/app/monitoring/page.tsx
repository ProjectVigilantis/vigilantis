// MON-001 통합 관제 — 자산 관제(AST-001 · 화면설계서 v1.5 §4.2)와 보안 관제(SEC-001)를 두 축으로 합쳤다
// (2026-10-29 초안 · `components/monitoring/monitoring-view.tsx` 머리말). 두 축이 같은 조회 3종을 쓰므로
// 한 번 받아 둘 다 넘기고, 축 전환은 클라이언트가 한다. 옛 경로 `/assets` · `/security`는 여기로 redirect한다.
// 조회 자체는 고객사 상세와 함께 쓰려고 `components/monitoring/load-monitoring.ts`로 옮겼다.

import { unstable_rethrow } from 'next/navigation';

import { BusinessUnavailable } from '@/components/business-unavailable';
import { ErrorState } from '@/components/error-state';
import { loadMonitoring } from '@/components/monitoring/load-monitoring';
import { MonitoringView } from '@/components/monitoring/monitoring-view';
import { serverApi } from '@/lib/api/server';
import { parseAxis } from '@/lib/home-path';
import { parsePreset } from '@/lib/incident-filter';
import { isUnassignedManager } from '@/lib/roles';

/**
 * `?asset=<arn>`은 INC-002 대상 자산에서 넘어오는 딥링크다(§4.5 액션). AST-002는 Drawer라
 * 자체 URL이 없어, 목록 화면이 그 항목을 고른 상태로 열어 준다. `?tab=incidents`·`?preset=`은
 * 구 자산 인시던트 목록(`/asset-incidents`)에서 redirect돼 오는 딥링크다(2026-09-28).
 */
export default async function MonitoringPage({ searchParams }: PageProps<'/monitoring'>) {
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const api = await serverApi();
  // 담당 고객이 없는 고객 담당자는 메뉴가 없어도 주소로 들어올 수 있다 — 업무 데이터를 하나도 읽지 않고 배정 없음을
  // 알린다(대시보드와 같은 안내, #492). 계정 확인 실패도 업무 조회로 넘어가지 않는다. 고객사 계정은 기존대로 조회하고
  // 서버의 거절(403)을 오류 화면으로 본다.
  let user;
  try {
    user = await api.getMe();
  } catch (error) {
    unstable_rethrow(error);
    return <ErrorState error={error} />;
  }
  if (isUnassignedManager(user)) return <BusinessUnavailable unassigned />;
  const { axis, asset, tab, preset } = await searchParams;

  let loaded;
  try {
    loaded = await loadMonitoring(api);
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    // 자산 실패는 화면 전체 CMN-002다 — 대시보드(app/page.tsx)와 같은 규칙.
    // 잡지 않고 던지면 레이아웃(GNB)째 전역 오류 셸로 넘어가 다른 화면으로 빠져나갈 길이 없어진다.
    return <ErrorState error={error} />;
  }

  // 화면 이름(h1)은 뷰 안에 있다 — 지표 띠와 한 덩이로 스크롤을 따라와야 해서다(assets-view.tsx).
  return (
    <MonitoringView
      {...loaded}
      initialAxis={parseAxis(axis)}
      openArn={typeof asset === 'string' ? asset : undefined}
      initialTab={typeof tab === 'string' ? tab : undefined}
      initialPreset={parsePreset(preset)}
    />
  );
}
