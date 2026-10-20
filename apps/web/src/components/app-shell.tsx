'use client';

// 화면 골격 — GNB · 실시간 연결(CMN-001) · Toast를 두를지 경로로 고른다(#492 PR 1).
// 로그인 화면은 세션이 없으므로 GNB도, WebSocket도 열지 않는다. 라우트 그룹으로 옮기면 기존 페이지
// 파일이 전부 이동해 diff가 커지므로, URL은 그대로 두고 여기서 경로 하나만 뺀다.

import { usePathname } from 'next/navigation';

import { Gnb } from '@/components/gnb';
import { RealtimeProvider } from '@/components/realtime-provider';
import { ToastStack } from '@/components/toast-stack';

/** 골격 없이 본문만 그리는 경로. */
const BARE_PATHS: ReadonlySet<string> = new Set(['/login']);

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  if (BARE_PATHS.has(pathname)) {
    return <main className="flex flex-1 flex-col">{children}</main>;
  }

  return (
    // CMN-001이 소켓 수명주기를 소유한다 — layout 아래에 두어야 라우트가 바뀌어도 연결이 유지된다(4.8 1).
    <RealtimeProvider>
      <Gnb />
      <main className="flex flex-1 flex-col p-6">{children}</main>
      {/* CMN-001 Toast 스택 — 우하단 고정·8초·최대 3개(4.8). */}
      <ToastStack />
    </RealtimeProvider>
  );
}
