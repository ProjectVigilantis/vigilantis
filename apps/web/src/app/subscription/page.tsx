// 구독(T-16 · #528) — 고객사 계정의 자기 구독 화면. 고객사 중앙계정 · 고객사 직원계정 모두 보고, 쓰기는 서버가 고객사 중앙계정만 받는다.
// 플랫폼 계정에는 자기 구독이 없어(서버 403) 고객 구독은 고객 상세(`/tenants/[id]`)에서 조회만 하라고 안내한다 —
// 구독 API를 부르지 않는다. 계정 확인은 서버에서 하고, 구독 조회 · 다시 읽기 · 쓰기는 클라이언트 경계
// (components/subscriptions/subscription-section.tsx)가 한다.

import type { Metadata } from 'next';
import { unstable_rethrow } from 'next/navigation';

import { ErrorState } from '@/components/error-state';
import { SubscriptionSection } from '@/components/subscriptions/subscription-section';
import { serverApi } from '@/lib/api/server';
import { subscriptionOpen } from '@/lib/home-path';

export const metadata: Metadata = { title: '구독 · Vigilantis' };

export default async function SubscriptionPage() {
  // 세션 쿠키만 실어 부르고, 401이면 로그인으로 redirect한다(lib/api/server.ts).
  const { getMe } = await serverApi();
  let user;
  try {
    user = await getMe();
  } catch (error) {
    unstable_rethrow(error); // 401 → 로그인 redirect는 삼키지 않는다
    return <ErrorState error={error} />;
  }
  if (!subscriptionOpen(user)) {
    return (
      <section className="flex flex-col gap-4" aria-labelledby="subscription-unavailable">
        <h1 id="subscription-unavailable" className="text-lg font-semibold">구독</h1>
        <p role="status" className="rounded-lg border p-4 text-sm">
          플랫폼 계정에는 자기 구독이 없습니다. 고객의 구독은 고객 관리의 고객 상세에서 조회할 수 있습니다.
        </p>
      </section>
    );
  }
  return <SubscriptionSection />;
}
