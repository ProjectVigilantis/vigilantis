// 고객 상세(Post-MVP T-4). 없는 고객이면 TENANT_NOT_FOUND 전체 오류 화면이다.

import { ErrorState } from '@/components/error-state';
import { TenantDetailView } from '@/components/tenants/tenant-detail-view';
import { getTenant } from '@/lib/api/client';

export default async function TenantPage({ params }: PageProps<'/tenants/[id]'>) {
  const { id } = await params;
  let tenant;
  try {
    tenant = await getTenant(id);
  } catch (error) {
    return <ErrorState error={error} />;
  }
  return <TenantDetailView tenant={tenant} />;
}
