// INC-001 보안 / INC-004 자산 목록의 공통 서버 컴포넌트 — 화면설계서 v1.6 §4.4.
// 두 화면은 `category`와 `선제차단` 프리셋 유무만 다르고 나머지 규칙이 같습니다.

import { ErrorState } from '@/components/error-state';
import { IncidentsView } from '@/components/incidents/incidents-view';
import { getIncidents } from '@/lib/api/client';
import { byPreset, parsePreset, presetServerStatus } from '@/lib/incident-filter';
import type { IncidentCategory } from '@/types/api';

export async function IncidentListPage({
  category,
  heading,
  basePath,
  presetParam,
}: {
  category: IncidentCategory;
  heading: string;
  /** 프리셋 링크가 붙을 경로 — 화면이 갈렸으므로 하드코딩하지 않는다. */
  basePath: string;
  presetParam: string | string[] | undefined;
}) {
  const preset = parsePreset(presetParam);
  const serverStatus = presetServerStatus(preset);

  let incidents;
  try {
    // `category`는 **항상** 넘긴다 — 화면이 곧 유형이다(v1.6). 서버 필터가 이미 있어
    // 계약 변경 없이 화면 분리가 성립한다(§4.4 소비 계약).
    incidents = await getIncidents({ category, status: serverStatus });
  } catch (error) {
    // variant를 넘기지 않는다 — code별 프리셋이 정한 자리를 쓴다(§4.9, PR #171 리뷰).
    return <ErrorState error={error} />;
  }

  /**
   * 대기·선제차단 건수는 **셀 수 있을 때만** 넘긴다 — 요청을 한 번 더 부르지 않기 때문이다.
   * 서버가 `status`로 걸러 준 응답(`PENDING`·`HISTORY`)에는 다른 프리셋의 건이 아예 없어
   * 0이 **거짓말**이 된다(PR #171 리뷰). 그때는 null로 넘겨 배지를 감춘다.
   */
  const countable = serverStatus === undefined;
  const items = incidents.items;

  return (
    <>
      <h1 className="mb-4 text-lg font-semibold">{heading}</h1>
      <IncidentsView
        items={items}
        preset={preset}
        basePath={basePath}
        // 선제차단 프리셋은 보안 화면에만 둔다 — FINOPS는 `response_mode`가 없어 항상 0건이다.
        showPreemptive={category === 'SECOPS'}
        pendingCount={countable ? byPreset(items, 'PENDING').length : null}
        preemptiveCount={countable ? byPreset(items, 'PREEMPTIVE').length : null}
      />
    </>
  );
}
