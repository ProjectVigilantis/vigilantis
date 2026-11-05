// 고객 설정(T-7) — 플랫폼 계정이 고객 상세에서 여는 그 고객의 설정. 고객사 화면(app/settings)과 같은 폼을 쓴다.
// 범위 밖 · 없는 고객이면 서버가 404 `TENANT_NOT_FOUND`로 막고, 그 오류 화면을 그린다.

import { ArrowLeft } from 'lucide-react';
import type { Metadata } from 'next';
import Link from 'next/link';
import { unstable_rethrow } from 'next/navigation';

import { ErrorState } from '@/components/error-state';
import { TenantSettingsView } from '@/components/settings/tenant-settings-view';
import { serverApi } from '@/lib/api/server';

export const metadata: Metadata = { title: '고객 설정 · Vigilantis' };

export default async function TenantSettingsPage({ params }: PageProps<'/tenants/[id]/settings'>) {
  const { id } = await params;
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getTenant, getTenantSettings } = await serverApi();
  let tenant;
  let settings;
  try {
    [tenant, settings] = await Promise.all([getTenant(id), getTenantSettings(id)]);
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Link href={`/tenants/${encodeURIComponent(id)}`}
          className="text-muted-foreground inline-flex w-fit items-center gap-1 text-sm hover:underline">
          <ArrowLeft aria-hidden className="size-3.5" />
          고객 상세
        </Link>
        <h1 className="text-lg font-semibold break-all">{tenant.name} 설정</h1>
      </div>
      <TenantSettingsView key={id} tenantId={id} initial={settings} />
    </div>
  );
}
