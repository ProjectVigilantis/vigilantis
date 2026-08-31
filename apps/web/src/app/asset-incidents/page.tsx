// INC-004 자산 인시던트 목록 — 화면설계서 v1.6 §4.4 (`category = FINOPS`, v1.6 신규 화면).

import { IncidentListPage } from '@/components/incidents/incident-list-page';

export default async function AssetIncidentsPage({ searchParams }: PageProps<'/asset-incidents'>) {
  const { preset } = await searchParams;
  return (
    <IncidentListPage
      category="FINOPS"
      heading="자산 인시던트"
      basePath="/asset-incidents"
      presetParam={preset}
    />
  );
}
