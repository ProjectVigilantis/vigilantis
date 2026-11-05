'use client';

// 고객 구독 화면의 조회 경계(T-16 · #528) — 불러오는 중 · 오류(다시 시도) · 응답을 가른다.
// 조회 실패를 Free나 미등록으로 그리지 않는다. 미등록은 성공 응답의 `subscription: null`이고 본체가 그린다.
// 조회 · 다시 읽기 · 쓰기 응답 반영 · 경계 타이머 · 정리는 lib/subscription-refresher.ts가 소유한다. 여기서는
// 마운트 동안 그것을 하나 만들어 상태를 그리고, 화면을 떠나면 정리한다.

import { useEffect, useRef, useState } from 'react';

import { ErrorState } from '@/components/error-state';
import { SkeletonBlock } from '@/components/skeleton-block';
import { SubscriptionView } from '@/components/subscriptions/subscription-view';
import { Button } from '@/components/ui/button';
import { getMySubscription } from '@/lib/api/client';
import {
  createSubscriptionRefresher,
  type SubscriptionLoad,
  type SubscriptionRefresher,
} from '@/lib/subscription-refresher';

export function SubscriptionSection() {
  const [load, setLoad] = useState<SubscriptionLoad>({ kind: 'loading' });
  const refresher = useRef<SubscriptionRefresher | null>(null);

  useEffect(() => {
    const owner = createSubscriptionRefresher({ read: getMySubscription, onChange: setLoad });
    refresher.current = owner;
    owner.start();
    return () => {
      owner.dispose();
      refresher.current = null;
    };
  }, []);

  if (load.kind === 'loading') return <SkeletonBlock rows={4} />;
  if (load.kind === 'error') {
    return (
      <div className="flex flex-col gap-3">
        <ErrorState error={load.error} variant="inline" />
        <Button type="button" variant="outline" size="sm" className="w-fit" onClick={() => refresher.current?.retry()}>
          다시 시도
        </Button>
      </div>
    );
  }
  return (
    <SubscriptionView
      data={load.data}
      checkedAt={load.checkedAt}
      asOf={load.asOf}
      awaiting={load.awaiting}
      refreshError={load.refreshError}
      reloading={load.reloading}
      onChange={(data) => refresher.current?.accept(data)}
      onReload={() => refresher.current?.reload()}
      onRefresh={() => refresher.current?.refresh()}
      onHold={(held) => refresher.current?.hold(held)}
    />
  );
}
