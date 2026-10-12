'use client';

// 요약 화면(대시보드 · 자산 관제 · 보안 관제)을 주기적으로 다시 읽고, 마지막으로 읽은 시각과 수동 갱신
// 버튼을 화면 제목 옆에 보인다. 시계열 그래프·자산 상태는 스캔마다 바뀌지만 스캔 자체는 WebSocket 이벤트를
// 내지 않아(새 인시던트가 없으면) 이것 없이는 새로고침해야 화면이 따라온다. 인시던트 변화는 WS가
// RealtimeProvider의 `router.refresh()`로 즉시 반영하므로 이 주기와 무관하다.
//
// 주기는 스캔 주기(`SCAN_INTERVAL_SECONDS` 기본 300초)와 맞춘 5분이다. 그보다 짧으면 바뀐 것 없이
// API만 두드리고, 길면 스캔 결과가 그만큼 늦게 뜬다. 지금 보고 싶으면 [새로고침]을 누른다.
//
// `router.refresh()`는 서버 컴포넌트만 다시 그리고 클라이언트 상태(열린 Drawer · 필터 · 탭)는 보존한다.
// 탭이 가려져 있으면 건너뛴다 — 보지 않는 화면이 API를 두드리지 않게 한다.

import { RefreshCw } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useEffect, useTransition } from 'react';

import { Button } from '@/components/ui/button';

export const REFRESH_MS = 5 * 60_000;

const TIME_FORMAT = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

/**
 * @param renderedAt 페이지(서버 컴포넌트)가 데이터를 읽은 시각(ISO). 자동·수동·WS 어느 경로로 다시
 *   그려져도 페이지가 새 값을 넘기므로 "화면 숫자가 언제 것인가"와 항상 같다. 시간대를 KST로 고정해
 *   서버·브라우저 렌더 결과가 갈리지 않게 한다(하이드레이션 불일치 방지).
 */
export function AutoRefresh({ renderedAt }: { renderedAt: string }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') router.refresh();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [router]);

  return (
    <div className="text-muted-foreground flex items-center gap-2 text-xs">
      <span>
        마지막 갱신 <time dateTime={renderedAt}>{TIME_FORMAT.format(new Date(renderedAt))}</time>
      </span>
      <Button
        variant="outline"
        size="sm"
        disabled={pending}
        onClick={() => startTransition(() => router.refresh())}
      >
        <RefreshCw className={pending ? 'animate-spin' : undefined} aria-hidden />
        {pending ? '갱신 중' : '새로고침'}
      </Button>
    </div>
  );
}
