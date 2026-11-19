'use client';
// 로그인 전 화면(랜딩 · 도입 가이드 · 약관 같은 공개 문서)의 상단 메뉴. 메뉴 이름에 마우스를 올리거나 누르면 하위 메뉴가
// 열리고, 항목을 누르면 그 페이지로 간다. 여닫기 · 키보드 · 열림 상태(aria-expanded)는 Radix NavigationMenu가 맡는다.
// CSS(hover · focus-visible)만으로 열던 때는 Tab이 첫 링크에 닿기 전에 메뉴가 닫혀 하위 항목에 들어갈 수 없었고,
// hover가 없는 터치에서는 열리지 않았다(PR #665 리뷰).
// 좁은 화면(< md)에서는 접는다. 같은 문서는 바닥글 링크(components/legal/legal-document.tsx `FooterLinks`)로도 갈 수 있다.

import { ChevronDown } from 'lucide-react';
import Link from 'next/link';
import { NavigationMenu } from 'radix-ui';

import { SITE_MENU } from '@/lib/site-menu';

export function SiteNav({ className }: { className?: string }) {
  return (
    <NavigationMenu.Root aria-label="사이트 메뉴" delayDuration={100} className={className ?? 'hidden md:block'}>
      <NavigationMenu.List className="flex items-center gap-2">
        {SITE_MENU.map((group) => (
          <NavigationMenu.Item key={group.label} className="relative">
            <NavigationMenu.Trigger className="group text-foreground-secondary hover:text-foreground data-[state=open]:text-foreground focus-visible:ring-ring inline-flex h-10 items-center gap-1 rounded-md px-3 text-sm font-medium transition-colors focus-visible:ring-2 focus-visible:outline-none">
              {group.label}
              <ChevronDown aria-hidden className="size-3.5 transition-transform group-data-[state=open]:rotate-180" />
            </NavigationMenu.Trigger>
            {/* pt-2는 버튼과 메뉴 사이의 빈틈이다. 이 틈도 메뉴 안이어야 마우스를 내리는 동안 메뉴가 닫히지 않는다. */}
            <NavigationMenu.Content className="absolute top-full left-1/2 z-50 -translate-x-1/2 pt-2">
              {/* 불투명한 떠 있는 메뉴다. 반투명 바탕은 밝은 문서 본문 위에서 글자 대비가 1.2:1까지 떨어졌다(DESIGN.md §깊이). */}
              <ul className="bg-popover flex min-w-44 flex-col rounded-xl border p-1.5 shadow-lg">
                {group.items.map((item) => (
                  <li key={item.href}>
                    <NavigationMenu.Link asChild>
                      <Link
                        href={item.href}
                        className="text-foreground-secondary hover:text-foreground focus-visible:text-foreground focus-visible:ring-ring flex h-9 items-center rounded-lg px-3 text-sm whitespace-nowrap transition-colors focus-visible:ring-2 focus-visible:outline-none"
                      >
                        {item.label}
                      </Link>
                    </NavigationMenu.Link>
                  </li>
                ))}
              </ul>
            </NavigationMenu.Content>
          </NavigationMenu.Item>
        ))}
      </NavigationMenu.List>
    </NavigationMenu.Root>
  );
}
