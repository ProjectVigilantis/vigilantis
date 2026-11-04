// 고객 설정(T-7) — 고객사 계정의 자기 고객 설정. 대상 고객은 주소가 아니라 로그인한 계정(`/auth/me`의 tenant_id)이다.
// 모든 고객사 역할이 들어오고 ADMIN만 바꾼다(OPERATOR · VIEWER는 조회만 — components/settings/tenant-settings-view.tsx).
// 플랫폼 계정은 고객 상세의 설정(`/tenants/[id]/settings`)을 쓴다. 실제 차단과 범위는 서버(403 · 404)다.

import type { Metadata } from 'next';
import Link from 'next/link';
import { unstable_rethrow } from 'next/navigation';

import { ErrorState } from '@/components/error-state';
import { TenantSettingsView } from '@/components/settings/tenant-settings-view';
import { serverApi } from '@/lib/api/server';
import type { TenantSettings } from '@/types/api';

export const metadata: Metadata = { title: '설정 · Vigilantis' };

export default async function SettingsPage() {
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const api = await serverApi();
  // 플랫폼 계정이면 null — 조회하지 않고 안내만 그린다.
  let tenantId: string | null = null;
  let settings: TenantSettings | null = null;
  try {
    const me = await api.getMe();
    if (me.account_type === 'TENANT') {
      tenantId = me.tenant_id;
      settings = await api.getTenantSettings(tenantId);
    }
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }
  if (tenantId === null || settings === null) {
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-lg font-semibold">설정</h1>
        <p role="status" className="rounded-lg border p-4 text-sm text-muted-foreground">
          고객사 계정의 설정 화면입니다. 플랫폼 계정은{' '}
          <Link href="/tenants" className="underline">고객 목록</Link>에서 고객을 연 뒤 그 고객의 설정을 엽니다.
        </p>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold">설정</h1>
      <TenantSettingsView key={tenantId} tenantId={tenantId} initial={settings} />
    </div>
  );
}
