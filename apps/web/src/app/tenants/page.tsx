// 고객 관리(Post-MVP T-4): 고객 AWS 계정 목록. 응답을 캐시하지 않아 `router.refresh()`가 곧 재조회다.

import { unstable_rethrow } from 'next/navigation';

import { ErrorState } from '@/components/error-state';
import { TenantsView } from '@/components/tenants/tenants-view';
import { serverApi } from '@/lib/api/server';

export default async function TenantsPage() {
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getTenants } = await serverApi();
  let data;
  try {
    data = await getTenants();
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }
  // 연결 확인 경과 시간의 기준 시각. 다른 조회 화면과 같이 서버가 그린 시각을 넘긴다(app/page.tsx).
  const renderedAt = new Date().toISOString();
  return <TenantsView tenants={data.tenants} renderedAt={renderedAt} />;
}
