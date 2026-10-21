// 고객 상세(Post-MVP T-4). 없는 고객이면 TENANT_NOT_FOUND 전체 오류 화면이다.

import { unstable_rethrow } from 'next/navigation';

import { ErrorState } from '@/components/error-state';
import { TenantDetailView } from '@/components/tenants/tenant-detail-view';
import { serverApi } from '@/lib/api/server';

export default async function TenantPage({ params }: PageProps<'/tenants/[id]'>) {
  const { id } = await params;
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getTenant } = await serverApi();
  let tenant;
  try {
    tenant = await getTenant(id);
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }
  return <TenantDetailView tenant={tenant} />;
}
