// AST-001 자산 관제는 통합 관제(MON-001 `/monitoring`)의 `자산` 축이 됐다(2026-10-29).
// 옛 링크(`?asset=` · `?tab=` · `?preset=` 딥링크 포함)를 그 축으로 보내려고 남는다 — 화면을 그리지 않는다.

import { redirect } from 'next/navigation';

import { monitoringPath } from '@/lib/home-path';

export default async function AssetsPage({ searchParams }: PageProps<'/assets'>) {
  redirect(monitoringPath('assets', await searchParams));
}
