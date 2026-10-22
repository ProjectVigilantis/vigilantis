// 전역 셸: 상단바(로고 · 수집 · 실시간 · 주간/야간 · 사용자)와 사이드바(주요 화면)다(DESIGN.md §셸).
//
// 셸은 두 모드 모두 남색이고, 안쪽 부품(상태 칩 · 사용자 메뉴)은 어두운 바탕용 색으로 그려야 읽힌다.
// 그래서 셸 요소에 `dark` 클래스를 달아 그 아래만 야간 토큰으로 푼다. 셸 배경색은 html에 걸린
// `--shell`이 정하므로 주간 · 야간에 따라 바뀐다(globals.css).

'use client';

import { Building2, LayoutDashboard, Server, Shield, Users, type LucideIcon } from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { useSession } from '@/components/auth/session-provider';
import { UserMenu } from '@/components/auth/user-menu';
import { CollectionIndicator, useAssetsEnvelope } from '@/components/collection-indicator';
import { ConnectionIndicator, type AssetPresence } from '@/components/connection-indicator';
import { ThemeToggle } from '@/components/theme-toggle';
import { canManageUsers } from '@/lib/roles';
import { cn } from '@/lib/utils';
import type { UserRole } from '@/types/api';

/**
 * 2.3 진입점 정리 — 모든 화면에서 이 **3개**로 이동한다(2026-09-28 개편 — v1.6 팀 회의의 4개에서).
 *
 * 인시던트 목록 두 화면(INC-001 `/incidents` · INC-004 `/asset-incidents`)은 각각 보안 관제(SEC-001)·
 * 자산 관제(AST-001)의 `인시던트` 탭으로 흡수됐다 — 자원과 그 자원의 진단이 한 화면에 있어야 지표 띠의
 * 타일·필터 하나로 둘을 같이 좁힐 수 있다. 옛 경로는 그 탭으로 redirect한다(app/incidents/page.tsx ·
 * app/asset-incidents/page.tsx). **상세(`/incidents/[id]`)와 ACT-002 딥링크는 그대로다** — 옮기면 기존
 * 링크가 전부 깨진다(PR #180).
 *
 * `자산`(AST-001)과 `보안`(SEC-001)은 짝이다 — 앞이 자원 인벤토리와 최적화, 뒤가 그 자원의 노출·위협이다.
 *
 * `고객`은 Post-MVP T-4 관리자 화면이다(고객 계정 연결 · 원격 회수). 관제 화면이 아니라 맨 뒤에 둔다.
 * 누구에게 보일지(역할 제한)는 T-1 역할과 함께 정한다. 그 전까지 권한 판정은 서버 몫이다.
 */
const NAV: readonly { href: string; label: string; icon: LucideIcon; adminOnly?: boolean }[] = [
  { href: '/', label: '대시보드', icon: LayoutDashboard },
  { href: '/assets', label: '자산', icon: Server },
  { href: '/security', label: '보안', icon: Shield },
  // 플랫폼 관리자 화면. 숨기는 것은 편의이고 고객 관리 API의 차단은 서버가 한다(lib/home-path.ts).
  { href: '/tenants', label: '고객', icon: Building2, adminOnly: true },
];

/** ADMIN에게만 보이는 메뉴(#492 PR 3). 숨김은 화면 편의이고 실제 차단은 서버 403이다. */
const ADMIN_NAV: readonly { href: string; label: string; icon: LucideIcon }[] = [
  { href: '/users', label: '사용자', icon: Users },
];

/** 현재 사용자의 역할(세션 Provider · #492). 확인 전 · 미로그인 · 조회 실패는 null이다(관리자 메뉴를 열지 않는다). */
function useRole(): UserRole | null {
  const { session } = useSession();
  return session.kind === 'user' ? session.user.role : null;
}

export function ShellHeader() {
  const assets = useAssetsEnvelope();
  const presence: AssetPresence = assets.failed
    ? 'failed'
    : assets.env === null
      ? 'pending'
      : assets.env.items.length > 0
        ? 'present'
        : 'none';

  return (
    <header className="dark bg-shell text-foreground flex h-16 shrink-0 items-center gap-3 border-b px-4 sm:gap-6">
      <Link href="/" className="flex shrink-0 items-center gap-2.5">
        <span className="bg-primary flex size-7 items-center justify-center rounded-md">
          <Shield aria-hidden className="size-4 text-white" />
        </span>
        <span className="text-base font-bold tracking-tight">Vigilantis</span>
      </Link>

      {/* 두 인디케이터는 축이 다르다 — 왼쪽은 서버↔클라우드 수집 상태(`GET /assets` 봉투), 오른쪽은
          관제 자산↔화면 실시간 연결(소켓이 열렸고 수집된 자산이 있을 때만 초록 — connection-indicator.tsx). 자리를 다투지 않고, WS 쪽을
          빼면 끊겼을 때 [재연결] 버튼(§4.8 4)이 사라져 둘 다 둔다(PR #299 리뷰). */}
      {/* 좁은 화면(< lg)에서는 두 인디케이터가 들어갈 폭이 없다(수집 칩만 288px): 전환 · 사용자만 남긴다. */}
      <div className="ml-auto flex min-w-0 shrink items-center gap-3 sm:gap-4">
        <div className="hidden shrink-0 items-center gap-4 lg:flex">
          <CollectionIndicator env={assets.env} failed={assets.failed} />
          <ConnectionIndicator assets={presence} />
        </div>
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}

/** 넓은 화면에서는 왼쪽 세로 메뉴, 좁은 화면(< md)에서는 상단바 아래 가로 줄이다. */
export function ShellNav() {
  const pathname = usePathname();
  const role = useRole();
  const items = [
    ...NAV.filter((item) => !item.adminOnly || role === 'ADMIN'),
    ...(canManageUsers(role) ? ADMIN_NAV : []),
  ];

  return (
    <nav
      aria-label="주요 화면"
      className="dark bg-shell-nav flex shrink-0 gap-1 overflow-x-auto p-2 [scrollbar-width:none] md:w-58 md:flex-col md:overflow-visible md:border-r md:p-4"
    >
      {items.map(({ href, label, icon: Icon }) => {
        // 경계(`/` 또는 문자열 끝)까지 본다. 접두만 보면 다른 경로가 같이 활성이 된다.
        // 인시던트 상세(`/incidents/[id]`)는 카테고리를 모르는 자리라 어느 항목도 켜지 않는다.
        const active =
          href === '/' ? pathname === '/' : pathname === href || pathname.startsWith(`${href}/`);
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
