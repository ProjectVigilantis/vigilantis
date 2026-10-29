// 도입 신청함(플랫폼 계정, 2026-10-29). 로그인 창 회원가입에서 처음 쓰는 회사가 보낸 신청을 모은다.
// 관리자 상단바 알림 종에서 신청 알림을 누르면 `?id=`로 그 건을 펼친다. 계약은 초안이다(types/api.ts 끝).
// 서버에 아직 경로가 없으면(404 · 405) 오류 대신 준비 중이라고 적는다.

import type { Metadata } from 'next';

import { ErrorState } from '@/components/error-state';
import { SignupRequestsView } from '@/components/support/signup-requests-view';
import { NotYetServed, SupportPage } from '@/components/support/support-page';
import { ApiError } from '@/lib/api/client';
import { orError, serverApi } from '@/lib/api/server';
import { notYetServed } from '@/lib/support';

export const metadata: Metadata = { title: '도입 신청 · Vigilantis' };

export default async function SignupRequestsPage({ searchParams }: PageProps<'/signup-requests'>) {
  const renderedAt = new Date().toISOString();
  const id = (await searchParams).id;
  const { getSignupRequests } = await serverApi();
  // 401 → 로그인 redirect는 orError가 접지 않고 그대로 던진다(lib/api/server.ts).
  const result = await orError(getSignupRequests());

  return (
    <SupportPage
      title="도입 신청"
      description="처음 쓰는 회사가 보낸 신청입니다. 검토한 뒤 승인하면 고객사 화면에서 고객사를 추가하고 관리자를 초대합니다. 시각은 모두 KST입니다."
      renderedAt={renderedAt}
    >
      {result.value !== null ? (
        <SignupRequestsView items={result.value.items} focusId={typeof id === 'string' ? id : null} />
      ) : result.error instanceof ApiError && notYetServed(result.error.httpStatus) ? (
        <NotYetServed what="도입 신청" />
      ) : (
        <ErrorState error={result.error} />
      )}
    </SupportPage>
  );
}
