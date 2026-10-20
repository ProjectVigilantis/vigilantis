'use client';

// 화면 골격: 셸(상단바 · 사이드바) · 실시간 연결(CMN-001) · Toast를 두를지 경로로 고른다(#492 PR 1).
// 로그인 화면은 세션이 없으므로 셸도, WebSocket도 열지 않는다. 라우트 그룹으로 옮기면 기존 페이지
// 파일이 전부 이동해 diff가 커지므로, URL은 그대로 두고 여기서 경로 하나만 뺀다.

import { usePathname } from 'next/navigation';

import { SessionExpiryNotice } from '@/components/auth/session-expiry-notice';
import { SessionProvider } from '@/components/auth/session-provider';
import { ShellHeader, ShellNav } from '@/components/gnb';
import { RealtimeProvider } from '@/components/realtime-provider';
import { ToastStack } from '@/components/toast-stack';
import { LANDING_PATH, LOGIN_PATH } from '@/lib/return-to';

/** 골격 없이 본문만 그리는 경로. 랜딩(T-15)은 로그인 전 화면이라 셸 · 소켓을 열지 않는다. */
const BARE_PATHS: ReadonlySet<string> = new Set([LOGIN_PATH, LANDING_PATH]);

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  if (BARE_PATHS.has(pathname)) {
    return <main className="flex flex-1 flex-col">{children}</main>;
  }

  return (
    // 세션(`/me`)은 사용자 메뉴 · 조치 버튼 역할 판정 · 만료 안내가 같이 읽는다(#492 PR 2).
    <SessionProvider>
      {/* CMN-001이 소켓 수명주기를 소유한다 — layout 아래에 두어야 라우트가 바뀌어도 연결이 유지된다(4.8 1). */}
      <RealtimeProvider>
        <ShellHeader />
        {/* 만료 안내는 화면 전체 폭으로 상단바 바로 아래에 둔다. */}
        <SessionExpiryNotice />
        <div className="flex flex-1 flex-col md:flex-row">
          <ShellNav />
          {/* 작업 영역은 최대 1400px에 좌우 32px 여백이다(DESIGN.md §레이아웃과 간격). */}
          <main className="flex min-w-0 flex-1 flex-col p-4 sm:p-6 lg:p-8">
            <div className="mx-auto flex w-full max-w-[1400px] flex-1 flex-col">{children}</div>
          </main>
        </div>
        {/* CMN-001 Toast 스택 — 우하단 고정·8초·최대 3개(4.8). */}
        <ToastStack />
      </RealtimeProvider>
    </SessionProvider>
  );
}
