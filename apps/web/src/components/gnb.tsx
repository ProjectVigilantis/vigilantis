// 전역 셸: 상단바(로고 · 수집 · 실시간 · 주간/야간 · 사용자)와 사이드바(주요 화면)다(DESIGN.md §셸).
//
// 셸은 두 모드 모두 남색이고, 안쪽 부품(상태 칩 · 사용자 메뉴)은 어두운 바탕용 색으로 그려야 읽힌다.
// 그래서 셸 요소에 `dark` 클래스를 달아 그 아래만 야간 토큰으로 푼다. 셸 배경색은 html에 걸린
// `--shell`이 정하므로 주간 · 야간에 따라 바뀐다(globals.css).

'use client';

import { Building2, ClipboardList, LayoutDashboard, LifeBuoy, Shield, Users, type LucideIcon } from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { useSession } from '@/components/auth/session-provider';
import { UserMenu } from '@/components/auth/user-menu';
import { ConnectionIndicator } from '@/components/connection-indicator';
import { InquiryButton } from '@/components/support/inquiry-button';
import { NotificationBell } from '@/components/support/notification-bell';
import { ThemeToggle } from '@/components/theme-toggle';
import {
  MONITORING_PATH,
  TENANTS_PATH,
  USERS_PATH,
  businessOpen,
  confirmedAccount,
  homePathFor,
  navPathsFor,
} from '@/lib/home-path';
import { isPlatform, type AccessSubject } from '@/lib/roles';
import { INQUIRIES_PATH, SIGNUP_REQUESTS_PATH } from '@/lib/support';
import { cn } from '@/lib/utils';

/**
 * 2.3 진입점 정리 — 모든 화면에서 이 **3개**로 이동한다(2026-09-28 개편 — v1.6 팀 회의의 4개에서).
 *
 * 인시던트 목록 두 화면(INC-001 `/incidents` · INC-004 `/asset-incidents`)은 각각 보안 관제(SEC-001)·
 * 자산 관제(AST-001)의 `인시던트` 탭으로 흡수됐다 — 자원과 그 자원의 진단이 한 화면에 있어야 지표 띠의
 * 타일·필터 하나로 둘을 같이 좁힐 수 있다. 옛 경로는 그 탭으로 redirect한다(app/incidents/page.tsx ·
 * app/asset-incidents/page.tsx). **상세(`/incidents/[id]`)와 ACT-002 딥링크는 그대로다** — 옮기면 기존
 * 링크가 전부 깨진다(PR #180).
 *
 * `자산`(AST-001)과 `보안`(SEC-001)은 짝이라 2026-10-29에 `관제`(MON-001) 한 화면의 두 축으로 합쳤다 —
 * 앞이 자원 인벤토리와 최적화, 뒤가 그 자원의 노출·위협이다(components/monitoring/monitoring-view.tsx).
 *
 * `고객`은 Post-MVP T-4 플랫폼 운영자 화면이다(고객 계정 연결 · 원격 회수). 관제 화면이 아니라 뒤에 둔다.
 * 2026-10-29부터 `관제`는 메뉴에서 뺐다. 관리자 화면은 고객사 · 직원의 권한과 오류를 보는 자리라 고객사 상세에서 들어간다.
 * 어떤 계정에게 어떤 항목을 보일지는 `navPathsFor`(lib/home-path.ts)가 정한다 — 숨김은 편의이고 차단은 서버 403이다.
 */
const NAV: Readonly<Record<string, { label: string; icon: LucideIcon }>> = {
  '/': { label: '대시보드', icon: LayoutDashboard },
  [TENANTS_PATH]: { label: '고객사', icon: Building2 },
  [USERS_PATH]: { label: '계정', icon: Users },
  [SIGNUP_REQUESTS_PATH]: { label: '도입 신청', icon: ClipboardList },
  [INQUIRIES_PATH]: { label: '고객 문의', icon: LifeBuoy },
};

/** 현재 계정(세션 Provider · #492). 확인 전 · 미로그인 · 조회 실패는 null이다(메뉴 · 업무 기능을 열지 않는다). */
function useAccount(): AccessSubject | null {
  const { session } = useSession();
  return confirmedAccount(session);
}

export function ShellHeader() {
  const account = useAccount();
  // 자산 조회와 두 인디케이터는 업무 기능이다 — 확인된 플랫폼 계정에게만 연다(lib/home-path.ts `businessOpen`).
  // 확인 전 · 조회 실패 · 고객사 계정은 부르지도 그리지도 않는다. 주간/야간 전환과 사용자 메뉴는 그대로 둔다.
  const open = businessOpen(account);

  return (
    <header className="dark bg-shell text-foreground flex h-16 shrink-0 items-center gap-3 border-b px-4 sm:gap-6">
      {/* 고객 계정은 기존 역할별 진입점으로 간다. 플랫폼 대시보드 링크는 유지한다. */}
      <Link href={account !== null && !open ? homePathFor(account) : '/'} className="flex shrink-0 items-center gap-2.5">
        <span className="bg-primary flex size-7 items-center justify-center rounded-md">
          <Shield aria-hidden className="size-4 text-white" />
        </span>
        <span className="text-base font-bold tracking-tight">Vigilantis</span>
      </Link>

      {/* 서버 칩은 이 화면과 서버의 실시간 연결이다(connection-indicator.tsx). 끊겼을 때 [재연결] 버튼(§4.8 4)이 여기 있다.
          수집 칩(collection-indicator.tsx)은 2026-10-29에 뺐다 — 고객사 한 곳의 수집 상태라 모든 고객사를 보는 관리자
          상단바에는 맞지 않고, 고객사 웹에 붙일 정보다. */}
      {/* 좁은 화면(< lg)에서는 인디케이터를 접고 전환 · 사용자만 남긴다. */}
      <div className="ml-auto flex min-w-0 shrink items-center gap-3 sm:gap-4">
        {open ? (
          <div className="hidden shrink-0 items-center gap-4 lg:flex">
            <ConnectionIndicator />
          </div>
        ) : null}
        {/* 도입 신청 · 고객 문의 알림은 관리자(플랫폼) 계정에게, 문의하기는 고객사 계정(관리자 · 직원)에게 둔다(2026-10-29). */}
        {isPlatform(account) ? <NotificationBell /> : null}
        {account?.account_type === 'TENANT' ? <InquiryButton /> : null}
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}

/** 넓은 화면에서는 왼쪽 세로 메뉴, 좁은 화면(< md)에서는 상단바 아래 가로 줄이다. */
export function ShellNav() {
  const pathname = usePathname();
  const account = useAccount();
  const items = navPathsFor(account).map((href) => ({ href, ...NAV[href] }));
  if (items.length === 0) return null;

  return (
    <nav
      aria-label="주요 화면"
      className="dark bg-shell-nav flex shrink-0 gap-1 overflow-x-auto p-2 [scrollbar-width:none] md:w-58 md:flex-col md:overflow-visible md:border-r md:p-4"
    >
      {items.map(({ href, label, icon: Icon }) => {
        // 경계(`/` 또는 문자열 끝)까지 본다. 접두만 보면 다른 경로가 같이 활성이 된다.
        // 인시던트 상세(`/incidents/[id]`)는 카테고리를 모르는 자리라 어느 항목도 켜지 않는다.
        // 관제는 메뉴에 없고 고객사 상세에서 들어가므로 관제 화면에서는 `고객사`를 켠다.
        const under = (base: string) => pathname === base || pathname.startsWith(`${base}/`);
        const active =
          href === '/' ? pathname === '/' : under(href) || (href === TENANTS_PATH && under(MONITORING_PATH));
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? 'page' : undefined}
            className={cn(
              'flex h-10 shrink-0 items-center gap-2.5 rounded-lg px-3 text-[13px] font-bold whitespace-nowrap transition-colors',
              active ? 'bg-primary text-white' : 'text-white/78 hover:bg-white/8 hover:text-white',
            )}
          >
            <Icon aria-hidden className="size-4.5" />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
