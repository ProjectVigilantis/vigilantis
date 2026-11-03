'use client';

// 사이드바 맨 아래의 [설정](2026-11-03). 지금은 주간 · 야간 모드 하나뿐이다.
// 현재 모드는 html의 dark 클래스가 원천이다(lib/theme.ts). 종전에는 상단바의 전환 버튼이었다.

import { Moon, Settings, Sun } from 'lucide-react';
import { useSyncExternalStore } from 'react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { applyTheme, readTheme, type Theme } from '@/lib/theme';
import { cn } from '@/lib/utils';

/** html의 class 변화를 구독한다. 다른 탭이 아니라 이 문서 안의 변화만 본다. */
function subscribeTheme(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  return () => observer.disconnect();
}

/** 서버는 모드를 모른다. 서버 HTML은 주간으로 그리고, 붙은 뒤 실제 모드로 바꾼다. */
const serverTheme = (): Theme => 'light';

// 사이드바는 md(48rem)부터 왼쪽 세로 줄이고, 그 아래에서는 상단바 밑 가로 줄이다(gnb.tsx).
const WIDE = '(min-width: 48rem)';
function subscribeWide(onChange: () => void): () => void {
  const query = window.matchMedia(WIDE);
  query.addEventListener('change', onChange);
  return () => query.removeEventListener('change', onChange);
}

export function SettingsMenu({ className }: { className?: string }) {
  const theme = useSyncExternalStore(subscribeTheme, readTheme, serverTheme);
  // 세로 사이드바에서는 오른쪽으로, 가로 줄에서는 아래로 연다.
  const wide = useSyncExternalStore(subscribeWide, () => window.matchMedia(WIDE).matches, () => true);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger className={cn(className, 'data-[state=open]:bg-white/8 data-[state=open]:text-white')}>
        <Settings aria-hidden className="size-4.5" />
        {/* 가로 줄(< md)에서는 메뉴가 화면 밖으로 밀리지 않게 아이콘만 보인다 */}
        <span className="sr-only md:not-sr-only">설정</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent side={wide ? 'right' : 'bottom'} align="end" sideOffset={8} className="min-w-44">
        <DropdownMenuLabel>화면 모드</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme} onValueChange={(value) => applyTheme(value as Theme)}>
          <DropdownMenuRadioItem value="light">
            <Sun aria-hidden className="size-4" />
            주간
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">
            <Moon aria-hidden className="size-4" />
            야간
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
