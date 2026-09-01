// 전역 GNB — 화면설계서 v1.6 §3.1 골격(56px, 로고·내비 4개·우측 연결 인디케이터)입니다.

'use client';

import Link from 'next/link';

import { ConnectionIndicator } from '@/components/connection-indicator';
import { usePathname } from 'next/navigation';

import { cn } from '@/lib/utils';

/**
 * 2.3 진입점 정리 — 모든 화면에서 이 **4개**로 이동한다(v1.6 팀 회의 결정, 구 3개).
 *
 * 인시던트는 유형별로 화면이 갈렸다 — 구 `유형 ▾` 필터가 하던 구분을 화면이 한다(§4.4).
 * 두 목록은 구성 자체가 달라서다: FINOPS는 계약이 두 위험도를 `null`로 강제해 위험도 띠·
 * 전이 배지·위험도 정렬이 성립하지 않고, `response_mode`가 SECOPS 전용이라 `선제차단`
 * 프리셋도 없다(§1.4).
 *
 * **경로는 `/incidents`를 보안이 그대로 쓴다.** 상세(`/incidents/[id]`)와 ACT-002 딥링크가
 * 이 아래에 있어 옮기면 기존 링크가 전부 깨진다(PR #180). 자산만 새 경로를 받는다.
 *
 * `자산`(AST-001)과 `자산 인시던트`(INC-004)는 다른 것이다 — 앞은 자원 인벤토리, 뒤는 그
 * 자원에 대한 최적화 진단 건이다. 순서가 그 관계를 드러낸다(자원 → 그 자원의 진단).
 */
const NAV = [
  { href: '/', label: '대시보드' },
  { href: '/assets', label: '자산' },
  { href: '/incidents', label: '보안 인시던트' },
  { href: '/asset-incidents', label: '자산 인시던트' },
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
          // `/assets`가 `/asset-incidents`의 접두라 startsWith만으로는 둘 다 활성이 된다.
          // 경계(`/` 또는 문자열 끝)까지 봐야 한다.
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

      {/* 연결 인디케이터는 CMN-001(4.8) 소유다 — 소켓 상태를 RealtimeProvider에서 받아 그린다. */}
      <ConnectionIndicator />
    </header>
  );
}
