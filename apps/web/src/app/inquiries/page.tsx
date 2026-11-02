// 고객사 문의함(플랫폼 계정, 2026-10-29). 고객사 계정(중앙계정 · 직원계정)이 상단바 [문의하기]로 보낸 장애 · 버그 문의를 모은다.
// 목록은 페이지 응답이고(#565) 주소의 `?page=`가 쪽을 정한다. 관리자 상단바 알림 종에서 문의 알림을 누르면 `?id=`로 그 건을 펼친다. 계약은 #552다(types/api.ts).
// 서버에 아직 경로가 없으면(404 · 405) 오류 대신 준비 중이라고 적는다.

import type { Metadata } from 'next';

import { ErrorState } from '@/components/error-state';
import { InquiriesView } from '@/components/support/inquiries-view';
import { FocusMissing, NotYetServed, SupportPage, SupportPager } from '@/components/support/support-page';
import { ApiError } from '@/lib/api/client';
import { orError, serverApi } from '@/lib/api/server';
import { INQUIRIES_PATH, notYetServed, parsePage, SUPPORT_PAGE_SIZE } from '@/lib/support';

export const metadata: Metadata = { title: '고객사 문의 · Vigilantis' };

export default async function InquiriesPage({ searchParams }: PageProps<'/inquiries'>) {
  const renderedAt = new Date().toISOString();
  const params = await searchParams;
  const id = typeof params.id === 'string' ? params.id : null;
  const page = parsePage(params.page);
  const { getInquiries } = await serverApi();
  // 401 → 로그인 redirect는 orError가 접지 않고 그대로 던진다(lib/api/server.ts).
  const result = await orError(getInquiries({ page, page_size: SUPPORT_PAGE_SIZE }));

  return (
    <SupportPage
      title="고객사 문의"
      description="고객사 계정이 보낸 장애 · 버그 문의입니다. 처리하면서 상태를 옮기고, 답은 보낸 사람 이메일로 드립니다. 시각은 모두 KST입니다."
      renderedAt={renderedAt}
    >
      {result.value !== null ? (
        <div className="flex flex-col gap-4">
          {id !== null && !result.value.items.some((item) => item.inquiry_id === id) ? <FocusMissing /> : null}
          <InquiriesView items={result.value.items} focusId={id} />
          <SupportPager
            path={INQUIRIES_PATH}
            page={page}
            totalPages={result.value.total_pages}
            total={result.value.total}
          />
        </div>
      ) : result.error instanceof ApiError && notYetServed(result.error.httpStatus) ? (
        <NotYetServed what="고객사 문의" />
      ) : (
        <ErrorState error={result.error} />
      )}
    </SupportPage>
  );
}
