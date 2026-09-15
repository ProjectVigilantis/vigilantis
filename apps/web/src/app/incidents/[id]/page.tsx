// INC-002 인시던트 상세 — 화면설계서 v1.5 §4.5.

import { ErrorState } from '@/components/error-state';
import { IncidentDetail } from '@/components/incidents/incident-detail';
import { getAssets, getIncident } from '@/lib/api/client';

/**
 * 404 INCIDENT_NOT_FOUND는 정상 경로다(삭제·오타 링크) — 오류 경계로 던지지 않고 §4.9 전체 오류
 * 화면으로 그린다. ErrorState가 code별 처리(목록으로 버튼)를 이미 안다.
 *
 * 자산은 조인에만 쓴다 — `subject_arn`(대상 자산 블록)과 각 제안의 `target_arn`(승인 모달, #183).
 * 실패해도 인시던트 화면은 떠야 하므로 목록만 비운다.
 */
export default async function IncidentDetailPage({
  params,
  searchParams,
}: PageProps<'/incidents/[id]'>) {
  const { id } = await params;
  // `?execution=`은 INC-001 목록에서 실행하고 넘어온 경우다(§4.4 → §4.7). ACT-002를 연 채로 연다.
  const { execution } = await searchParams;

  let incident;
  try {
    incident = await getIncident(id);
  } catch (error) {
    return <ErrorState error={error} variant="page" />;
  }

  const assets = await getAssets().catch(() => null);

  return (
    <IncidentDetail
      incident={incident}
      assets={assets?.items ?? []}
      openExecutionId={typeof execution === 'string' ? execution : null}
    />
  );
}
