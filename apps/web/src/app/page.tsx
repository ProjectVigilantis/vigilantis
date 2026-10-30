// DSH-001 메인 대시보드 — 화면설계서 v1.6 §4.1. 요약 API가 없어 계약 엔드포인트를 서버에서 부른다.
//
// 조회는 반드시 `lib/api/client`를 거친다 — 오리진을 정하는 자리가 거기 하나(`apiBaseUrl()`)이고,
// 이 경로를 벗어나 상대 경로로 부르면 Next 서버가 자기 자신에게 물어 화면만 다른 곳을 본다(PR #299 리뷰 §2).
// 응답을 캐시하지 않아 새로고침·실시간 이벤트의 `router.refresh()`가 곧 재조회다.
//
// 호출 3종의 실패 처리가 서로 다르다(§4.1 예외) — 자산은 화면 전체 CMN-002, 인시던트 목록은
// 그것을 쓰는 자리(`미조치 자산·보안 인시던트` 지표 두 칸 · AI 카드)만 오류, 시계열은 추이 자리만 비운다.

import { ActionProposalCard } from '@/components/dashboard/action-proposal-card';
import { DashboardView } from '@/components/dashboard/dashboard-view';
import { AutoRefresh } from '@/components/auto-refresh';
import { BusinessUnavailable } from '@/components/business-unavailable';
import { ErrorState } from '@/components/error-state';
import { orError, orNull, serverApi } from '@/lib/api/server';
import { actionQueue, proposalRows } from '@/lib/dashboard';
import { businessOpen } from '@/lib/home-path';
import { isUnassignedManager } from '@/lib/roles';
import type { IncidentResponse } from '@/types/api';
import { unstable_rethrow } from 'next/navigation';

export default async function DashboardPage() {
  // 제목 옆 `마지막 갱신` — 다시 그려질 때마다 새로 찍힌다(auto-refresh.tsx).
  const renderedAt = new Date().toISOString();
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getMe, getAssets, getIncident, getIncidents, getMetricsTimeseries } = await serverApi();
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

  // 인시던트 실패는 대시보드를 죽이지 않는다 — 자산 화면과 같은 규칙(null = 조회 실패)이다.
  // 오류를 버리지 않고 들고 간다: AI 카드가 그것을 **대기 0건이 아니라 오류로** 그려야 한다(PR #351 리뷰 2).
  // 401(로그인 redirect)만은 접지 않는다 — orError가 그대로 던진다.
  const incidentsPromise = orError(getIncidents());

  // 시계열에서 대시보드가 쓰는 축은 `asset_inventory`(축 5, 자산 수)와 `asset_status`(축 4, 위협 판정)다 — CPU·네트워크는 자산 관제, 개방 SG는
  // 보안 관제가 그린다(dashboard-view.tsx 머리말). 실패는 화면을 죽이지 않는다: 추이는 현재 상태를 읽는
  // 데 필요한 것이 아니라 그 옆에 붙는 맥락이다. 자산 조회와 나란히 시작해 첫 화면을 늦추지 않게 한다.
  const metricsPromise = orNull(getMetricsTimeseries());

  let assets;
  try {
    assets = await getAssets();
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    // 자산 실패는 화면 전체 CMN-002다 — 계약에 부분 성공 개념이 없다(§4.1 예외).
    return <ErrorState error={error} />;
  }

  const listed = await incidentsPromise;
  const incidents = listed.value === null ? null : listed.value.items;
  const metrics = await metricsPromise;

  // AI 조치 제안 카드는 요약 목록이다. 목록 계약에 없는 것은 **자산 건의 추정 절감액** 하나라
  // 자산(FINOPS) 건만 상세를 나란히 부른다(자산 관제 AST-001의 절감 카드와 같은 방식). 실패한 건은
  // 버리고 금액 없음으로 그린다 — 카드 전체를 오류로 내리지 않는다.
  const queue = actionQueue(incidents);
  const details = new Map<string, IncidentResponse>();
  if (queue !== null) {
    const settled = await Promise.all(
      queue
        .filter((i) => i.category === 'FINOPS')
        .map((i) => orNull(getIncident(i.incident_id))),
    );
    for (const detail of settled) if (detail !== null) details.set(detail.incident_id, detail);
  }

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold">대시보드</h1>
        <AutoRefresh renderedAt={renderedAt} />
      </div>
      {/* AI 카드를 본문 위에 쌓지 않고 **슬롯으로 넘긴다** — 넓은 화면에서 본문 오른쪽 레일에
          세워야 하는데, 그 자리를 아는 것은 격자를 가진 `DashboardView`다. 여기서 만들어 넘기는
          것은 그대로다: 서버에서 그려야 `errorSlot`의 RSC 직렬화 제약(아래)을 지킬 수 있다. */}
      <DashboardView
        assets={assets}
        incidents={incidents}
        assetStatus={metrics === null ? null : metrics.asset_status}
        assetInventory={metrics === null ? null : metrics.asset_inventory}
        proposalSlot={
          <ActionProposalCard
            rows={queue === null ? null : proposalRows(queue, details)}
            // CMN-002를 **서버에서 그려 넘긴다** — `ErrorState`를 클라이언트 경계 너머로 보내면
            // RSC 직렬화가 `ApiError`의 code·requestId를 버려 분기가 무너진다(error-state.tsx 주의).
            errorSlot={listed.error === null ? null : <ErrorState error={listed.error} variant="inline" />}
          />
        }
      />
    </>
  );
}
