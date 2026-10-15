// INC-004 자산 인시던트 목록은 자산 관제(AST-001, `/assets`)의 `인시던트` 탭으로 흡수됐다(2026-09-28).
// 이 경로는 옛 링크(Slack·북마크·`?preset=` 딥링크)를 그 탭으로 보내려고 남는다 — 화면을 그리지 않는다.

import { redirect } from 'next/navigation';

export default async function AssetIncidentsPage({ searchParams }: PageProps<'/asset-incidents'>) {
  const { preset } = await searchParams;
  const query = new URLSearchParams({ tab: 'incidents' });
  if (typeof preset === 'string') query.set('preset', preset);
  redirect(`/assets?${query.toString()}`);
}
