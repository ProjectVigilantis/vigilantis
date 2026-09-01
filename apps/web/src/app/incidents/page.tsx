// INC-001 보안 인시던트 목록 — 화면설계서 v1.6 §4.4 (`category = SECOPS`).
// 경로를 `/incidents`로 유지하는 이유는 gnb.tsx 주석 참조(상세·ACT-002 딥링크가 이 아래에 있다).

import { IncidentListPage } from '@/components/incidents/incident-list-page';

export default async function SecurityIncidentsPage({ searchParams }: PageProps<'/incidents'>) {
  const { preset } = await searchParams;
  return (
    <IncidentListPage
      category="SECOPS"
      heading="보안 인시던트"
      basePath="/incidents"
      presetParam={preset}
    />
  );
}
