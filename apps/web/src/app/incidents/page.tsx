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

  /**
   * 대기 건수는 **셀 수 있을 때만** 넘긴다 — 요청을 한 번 더 부르지 않기 때문이다.
   * `전체`(무필터)와 `승인 대기`에서는 받아 온 목록으로 정확히 세지만, `?status=FAILED` 같은
   * 두 프리셋 밖 필터에서는 응답에 승인 대기 건이 아예 없어 0이 **거짓말**이 된다(PR #171 리뷰).
   * 그때는 null로 넘겨 배지를 감춘다.
   */
  const countable = status === undefined || pendingOnly;
  const pendingCount = countable
    ? incidents.items.filter((i) => i.status === 'AWAITING_APPROVAL').length
    : null;

  return (
    <>
      <h1 className="mb-4 text-lg font-semibold">인시던트</h1>
      <IncidentsView
        items={incidents.items}
        pendingOnly={pendingOnly}
        // 두 프리셋 밖 필터가 걸린 상태 — 어느 프리셋도 활성이 아니다.
        otherStatusFilter={!countable && typeof status === 'string' ? status : null}
        pendingCount={pendingCount}
      />
    </>
  );
}
