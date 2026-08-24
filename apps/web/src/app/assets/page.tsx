// AST-001 자산 관제 — 화면설계서 v1.5 §4.2.

import { AssetsView } from '@/components/assets/assets-view';
import { getAssets, getIncidents } from '@/lib/api/client';

/**
 * 조회는 재분석하지 않는 계약이라 화면에 "재분석"이 없다(§6.1). 대신 응답을 캐시하지 않아
 * 새로고침이 곧 재조회다 — Next 16의 fetch는 기본 무캐시다.
 *
 * 인시던트는 자산 계약에 없는 값이라 목록 API를 한 번 더 부른다(`subject_arn` 역조인, §4.2).
 * 실패해도 자산 화면은 떠야 하므로 카운트만 비운다.
 */
export default async function AssetsPage() {
  const [assets, incidents] = await Promise.all([
    getAssets(),
    getIncidents().catch(() => null),
  ]);

  const incidentCountByArn: Record<string, number> = {};
  for (const incident of incidents?.items ?? []) {
    incidentCountByArn[incident.subject_arn] = (incidentCountByArn[incident.subject_arn] ?? 0) + 1;
  }

  return (
    <>
      <h1 className="mb-4 text-lg font-semibold">자산 관제</h1>
      <AssetsView data={assets} incidentCountByArn={incidentCountByArn} />
    </>
  );
}
