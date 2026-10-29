// INC-001 보안 인시던트 목록은 보안 관제(SEC-001)의 `인시던트` 탭으로 흡수됐다(2026-09-28).
// SEC-001은 2026-10-29 통합 관제(`/monitoring`)의 `보안` 축이 됐다. 이 경로는 옛 링크(Slack·북마크·`?preset=` 딥링크)를 그 탭으로 보내려고 남는다 — 화면을 그리지 않는다.
// 상세(`/incidents/[id]`)와 ACT-002 딥링크는 이 아래에 그대로 있다(gnb.tsx 주석).

import { redirect } from 'next/navigation';

export default async function SecurityIncidentsPage({ searchParams }: PageProps<'/incidents'>) {
  const { preset } = await searchParams;
  const query = new URLSearchParams({ tab: 'incidents' });
  if (typeof preset === 'string') query.set('preset', preset);
  redirect(`/monitoring?axis=security&${query.toString()}`);
}
