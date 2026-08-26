// INC-001 인시던트 목록 — 화면설계서 v1.5 §4.4.

import { ErrorState } from '@/components/error-state';
import { IncidentsView } from '@/components/incidents/incidents-view';
import { getIncidents } from '@/lib/api/client';
import type { IncidentStatus } from '@/types/api';

/**
 * `승인 대기` 프리셋은 **서버 필터**다(§4.4) — `?status=AWAITING_APPROVAL`을 그대로 계약 쿼리로
 * 넘긴다. 프리셋 전환이 곧 재조회이므로 링크로 두고, 유형·상태 셀렉트만 받아 온 목록 위에서 건다.
 *
 * 대기 건수 배지는 요청을 한 번 더 부르지 않는다 — `전체`에서는 받아 온 목록에서 세고,
 * `승인 대기`에서는 응답이 곧 그 목록이다.
 */
export default async function IncidentsPage({ searchParams }: PageProps<'/incidents'>) {
  const { status } = await searchParams;
  const pendingOnly = status === 'AWAITING_APPROVAL';

  let incidents;
  try {
    // `status`가 있으면 정규화하지 않고 그대로 계약 쿼리로 넘긴다 — 미등록 값은 서버가
    // 422 REQUEST_VALIDATION_FAILED로 답하고 화면은 그 오류를 그린다(§4.4). 조용히 `전체`로
    // 되돌리면 사용자는 잘못된 링크를 맞는 목록으로 착각한다.
    incidents = await getIncidents(
      typeof status === 'string' ? { status: status as IncidentStatus } : undefined,
    );
  } catch (error) {
    return <ErrorState error={error} variant="page" />;
  }

  // 응답이 무엇으로 걸러졌든 배지는 같은 식으로 센다 — `승인 대기` 프리셋에서는 전량이,
  // `전체`에서는 해당 status만 잡힌다. 요청을 한 번 더 부르지 않는다.
  const pendingCount = incidents.items.filter((i) => i.status === 'AWAITING_APPROVAL').length;

  return (
    <>
      <h1 className="mb-4 text-lg font-semibold">인시던트</h1>
      <IncidentsView
        items={incidents.items}
        pendingOnly={pendingOnly}
        pendingCount={pendingCount}
      />
    </>
  );
}
