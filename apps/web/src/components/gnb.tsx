// 전역 GNB — 화면설계서 v1.6 §3.1 골격(56px, 로고·내비·우측 연결 인디케이터)입니다.

'use client';

import Link from 'next/link';

import { CollectionIndicator, useAssetsEnvelope } from '@/components/collection-indicator';
import { ConnectionIndicator, type AssetPresence } from '@/components/connection-indicator';
import { usePathname } from 'next/navigation';

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
 * `자산`(AST-001)과 `보안`(SEC-001)은 짝이다 — 앞이 자원 인벤토리와 최적화, 뒤가 그 자원의 노출·위협이다.
 */
const NAV = [
  { href: '/', label: '대시보드' },
  { href: '/assets', label: '자산' },
  { href: '/security', label: '보안' },
] as const;

export function Gnb() {
  const pathname = usePathname();
  const assets = useAssetsEnvelope();
  const presence: AssetPresence = assets.failed
    ? 'failed'
    : assets.env === null
      ? 'pending'
      : assets.env.items.length > 0
        ? 'present'
        : 'none';

  return (
    <header className="flex h-14 shrink-0 items-center gap-6 border-b px-4">
      <Link
        href="/"
        className="shrink-0 font-heading text-sm font-semibold tracking-widest whitespace-nowrap"
      >
        VIGILANTIS
      </Link>

      <nav className="flex items-center gap-1" aria-label="주요 화면">
        {NAV.map(({ href, label }) => {
          // 경계(`/` 또는 문자열 끝)까지 본다 — 접두만 보면 다른 경로가 같이 활성이 된다.
          // 인시던트 상세(`/incidents/[id]`)는 카테고리를 모르는 자리라 어느 항목도 켜지 않는다.
          const active =
            href === '/'
              ? pathname === '/'
              : pathname === href || pathname.startsWith(`${href}/`);
          return (
            <Link
              key={href}
              href={href}
              aria-current={active ? 'page' : undefined}
              className={cn(
                'rounded-md px-3 py-1.5 text-sm whitespace-nowrap transition-colors hover:bg-muted',
                active ? 'font-medium text-foreground' : 'text-muted-foreground',
              )}
            >
              {label}
            </Link>
          );
        })}
      </nav>

      {/* 두 인디케이터는 축이 다르다 — 왼쪽은 서버↔클라우드 수집 상태(`GET /assets` 봉투), 오른쪽은
          관제 자산↔화면 실시간 연결(소켓이 열렸고 수집된 자산이 있을 때만 초록 — connection-indicator.tsx). 자리를 다투지 않고, WS 쪽을
          빼면 끊겼을 때 [재연결] 버튼(§4.8 4)이 사라져 둘 다 둔다(PR #299 리뷰). */}
      <div className="ml-auto flex shrink-0 items-center gap-4">
        <CollectionIndicator env={assets.env} failed={assets.failed} />
        <ConnectionIndicator assets={presence} />
      </div>
    </header>
  );
}
