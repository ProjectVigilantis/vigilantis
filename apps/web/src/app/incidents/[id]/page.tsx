// INC-002 인시던트 상세 — 화면설계서 v1.5 §4.5.

import { ErrorState } from '@/components/error-state';
import { IncidentDetail } from '@/components/incidents/incident-detail';
import { getAssets, getIncident } from '@/lib/api/client';

/**
 * 404 INCIDENT_NOT_FOUND는 정상 경로다(삭제·오타 링크) — 오류 경계로 던지지 않고 §4.9 전체 오류
 * 화면으로 그린다. ErrorState가 code별 처리(목록으로 버튼)를 이미 안다.
 *
 * 자산은 `subject_arn` 조인에만 쓴다. 실패해도 인시던트 화면은 떠야 하므로 조인 결과만 비운다.
 */
export default async function IncidentDetailPage({ params }: PageProps<'/incidents/[id]'>) {
  const { id } = await params;

  let incident;
  try {
    incident = await getIncident(id);
  } catch (error) {
    return <ErrorState error={error} variant="page" />;
  }

  const assets = await getAssets().catch(() => null);
  const subject = assets?.items.find((a) => a.arn === incident.subject_arn) ?? null;

  return <IncidentDetail incident={incident} subject={subject} />;
}
