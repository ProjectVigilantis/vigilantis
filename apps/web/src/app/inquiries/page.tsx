// 고객 문의함(플랫폼 계정, 2026-10-29). 고객사 계정(관리자 · 직원)이 상단바 [문의하기]로 보낸 장애 · 버그 문의를 모은다.
// 관리자 상단바 알림 종에서 문의 알림을 누르면 `?id=`로 그 건을 펼친다. 계약은 초안이다(types/api.ts 끝).
// 서버에 아직 경로가 없으면(404 · 405) 오류 대신 준비 중이라고 적는다.

import type { Metadata } from 'next';

import { ErrorState } from '@/components/error-state';
import { InquiriesView } from '@/components/support/inquiries-view';
import { NotYetServed, SupportPage } from '@/components/support/support-page';
import { ApiError } from '@/lib/api/client';
import { orError, serverApi } from '@/lib/api/server';
import { notYetServed } from '@/lib/support';

export const metadata: Metadata = { title: '고객 문의 · Vigilantis' };

export default async function InquiriesPage({ searchParams }: PageProps<'/inquiries'>) {
  const renderedAt = new Date().toISOString();
  const id = (await searchParams).id;
  const { getInquiries } = await serverApi();
  // 401 → 로그인 redirect는 orError가 접지 않고 그대로 던진다(lib/api/server.ts).
  const result = await orError(getInquiries());

  return (
    <SupportPage
      title="고객 문의"
      description="고객사 계정이 보낸 장애 · 버그 문의입니다. 처리하면서 상태를 옮기고, 답은 보낸 사람 이메일로 드립니다. 시각은 모두 KST입니다."
      renderedAt={renderedAt}
    >
      {result.value !== null ? (
        <InquiriesView items={result.value.items} focusId={typeof id === 'string' ? id : null} />
      ) : result.error instanceof ApiError && notYetServed(result.error.httpStatus) ? (
        <NotYetServed what="고객 문의" />
      ) : (
        <ErrorState error={result.error} />
      )}
    </SupportPage>
  );
}
