// 도입 신청함(중앙 관리계정, 2026-10-29). 로그인 창 회원가입에서 처음 쓰는 회사가 보낸 신청을 모은다.
// 목록은 페이지 응답이고(#565) 주소의 `?page=`가 쪽을 정한다. 관리자 상단바 알림 종에서 신청 알림을 누르면 `?id=`로 그 건을 펼친다. 계약은 #552다(types/api.ts).
// 서버에 아직 경로가 없으면(404 · 405) 오류 대신 준비 중이라고 적는다.

import type { Metadata } from 'next';

import { ErrorState } from '@/components/error-state';
import { SignupRequestsView } from '@/components/support/signup-requests-view';
import { FocusMissing, NotYetServed, SupportPage, SupportPager } from '@/components/support/support-page';
import { ApiError } from '@/lib/api/client';
import { orError, serverApi } from '@/lib/api/server';
import { SIGNUP_REQUESTS_PATH, notYetServed, parsePage, SUPPORT_PAGE_SIZE } from '@/lib/support';

export const metadata: Metadata = { title: '도입 신청 · Vigilantis' };

export default async function SignupRequestsPage({ searchParams }: PageProps<'/signup-requests'>) {
  const renderedAt = new Date().toISOString();
  const params = await searchParams;
  const id = typeof params.id === 'string' ? params.id : null;
  const page = parsePage(params.page);
  const { getSignupRequests } = await serverApi();
  // 401 → 로그인 redirect는 orError가 접지 않고 그대로 던진다(lib/api/server.ts).
  const result = await orError(getSignupRequests({ page, page_size: SUPPORT_PAGE_SIZE }));

  return (
    <SupportPage
      title="도입 신청"
      renderedAt={renderedAt}
    >
      {result.value !== null ? (
        <div className="flex flex-col gap-4">
          {id !== null && !result.value.items.some((item) => item.request_id === id) ? <FocusMissing /> : null}
          {/* 알림마다 새로 그린다(key): 같은 화면에서 다른 알림을 누르면 주소만 바뀌고 펼침 · 필터 상태가 남는다(PR #578 리뷰). */}
          <SignupRequestsView key={id ?? ''} items={result.value.items} focusId={id} />
          <SupportPager
            path={SIGNUP_REQUESTS_PATH}
            page={page}
            totalPages={result.value.total_pages}
            total={result.value.total}
          />
        </div>
      ) : result.error instanceof ApiError && notYetServed(result.error.httpStatus) ? (
        <NotYetServed what="도입 신청" />
      ) : (
        <ErrorState error={result.error} />
      )}
    </SupportPage>
  );
}
