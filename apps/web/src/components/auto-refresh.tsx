'use client';

// 요약 화면(대시보드 · 자산 관제 · 보안 관제)을 주기적으로 다시 읽는다 — 시계열 그래프는 스캔마다 점이
// 늘지만 스캔 자체는 WebSocket 이벤트를 내지 않아(새 인시던트가 없으면), 이것 없이는 새로고침해야
// 그래프가 자란다. `router.refresh()`는 서버 컴포넌트만 다시 그리고 클라이언트 상태(열린 Drawer ·
// 필터 · 탭)는 보존한다. 탭이 가려져 있으면 건너뛴다 — 보지 않는 화면이 API를 두드리지 않게 한다.

import { useRouter } from 'next/navigation';
import { useEffect } from 'react';

const REFRESH_MS = 15_000;

export function AutoRefresh() {
  const router = useRouter();
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') router.refresh();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [router]);
  return null;
}
