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
import { ErrorState } from '@/components/error-state';
import { getAssets, getIncident, getIncidents, getMetricsTimeseries } from '@/lib/api/client';
import { actionQueue, proposalRows } from '@/lib/dashboard';
import type { IncidentResponse } from '@/types/api';

export default async function DashboardPage() {
  // 인시던트 실패는 대시보드를 죽이지 않는다 — 자산 화면과 같은 규칙(null = 조회 실패)이다.
  // 오류를 버리지 않고 들고 간다: AI 카드가 그것을 **대기 0건이 아니라 오류로** 그려야 한다(PR #351 리뷰 2).
  const incidentsPromise = getIncidents().then(
    (res) => ({ items: res.items, error: null }),
    (error: unknown) => ({ items: null, error }),
  );

  // 시계열에서 대시보드가 쓰는 축은 `asset_inventory`(축 5, 자산 수)와 `asset_status`(축 4, 위협 판정)다 — CPU·네트워크는 자산 관제, 개방 SG는
  // 보안 관제가 그린다(dashboard-view.tsx 머리말). 실패는 화면을 죽이지 않는다: 추이는 현재 상태를 읽는
  // 데 필요한 것이 아니라 그 옆에 붙는 맥락이다. 자산 조회와 나란히 시작해 첫 화면을 늦추지 않게 한다.
  const metricsPromise = getMetricsTimeseries().then(
    (res) => res,
    () => null,
  );

  let assets;
  try {
    assets = await getAssets();
  } catch (error) {
    // 자산 실패는 화면 전체 CMN-002다 — 계약에 부분 성공 개념이 없다(§4.1 예외).
    return <ErrorState error={error} />;
  }

  const listed = await incidentsPromise;
  const incidents = listed.items;
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
        .map((i) => getIncident(i.incident_id).then((res) => res, () => null)),
    );
    for (const detail of settled) if (detail !== null) details.set(detail.incident_id, detail);
  }

  return (
    <>
      <AutoRefresh />
      <h1 className="mb-4 text-lg font-semibold">대시보드</h1>
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
