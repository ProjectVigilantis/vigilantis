// AST-001 자산 관제 — 화면설계서 v1.5 §4.2.

import { AssetsView } from '@/components/assets/assets-view';
import { getAssets, getIncidents } from '@/lib/api/client';
import type { IncidentListItem } from '@/types/api';

/**
 * 조회는 재분석하지 않는 계약이라 화면에 "재분석"이 없다(§6.1). 대신 응답을 캐시하지 않아
 * 새로고침이 곧 재조회다 — Next 16의 fetch는 기본 무캐시다.
 *
 * 인시던트는 자산 계약에 없는 값이라 목록 API를 한 번 더 부른다(`subject_arn` 역조인, §4.2·§4.3).
 * 실패해도 자산 화면은 떠야 하므로 역조인 결과만 비우되, **실패와 0건은 구분해서** 넘긴다(null = 조회 실패).
 * 합치면 관제 화면이 "연결된 인시던트 없음"으로 조회 실패를 덮어버린다(PR #137 리뷰).
 */
export default async function AssetsPage() {
  const [assets, incidents] = await Promise.all([
    getAssets(),
    getIncidents().catch(() => null),
  ]);

  let incidentsByArn: Record<string, IncidentListItem[]> | null = null;
  if (incidents !== null) {
    incidentsByArn = {};
    for (const incident of incidents.items) {
      (incidentsByArn[incident.subject_arn] ??= []).push(incident);
    }
  }

  return (
    <>
      <h1 className="mb-4 text-lg font-semibold">자산 관제</h1>
      <AssetsView data={assets} incidentsByArn={incidentsByArn} />
    </>
  );
}
