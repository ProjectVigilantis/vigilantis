// 전역 GNB — 화면설계서 v1.4 §3.1 골격(56px, 로고·내비 3개·우측 연결 인디케이터)입니다.

'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { cn } from '@/lib/utils';

/** 2.3 진입점 정리 — 모든 화면에서 이 3개로 이동한다. */
const NAV = [
  { href: '/', label: '대시보드' },
  { href: '/assets', label: '자산' },
  { href: '/incidents', label: '인시던트' },
] as const;

export function Gnb() {
  const pathname = usePathname();

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
          const active = href === '/' ? pathname === '/' : pathname.startsWith(href);
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

      {/*
        연결 인디케이터는 CMN-001(4.8) 소유다. WebSocket 연동 전이라 실제 상태는 "연결 끊김"이고,
        수동 [재연결] 버튼도 CMN-001과 함께 붙는다. 여기서는 자리와 문구만 잡아 둔다.
      */}
      <span className="ml-auto flex shrink-0 items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground">
        <span aria-hidden>○</span>
        연결 끊김
      </span>
    </header>
  );
}
