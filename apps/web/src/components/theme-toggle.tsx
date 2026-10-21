'use client';

// 상단바의 주간 · 야간 전환. 현재 모드는 html의 dark 클래스가 원천이다(lib/theme.ts).

import { Moon, Sun } from 'lucide-react';
import { useSyncExternalStore } from 'react';

import { applyTheme, readTheme, type Theme } from '@/lib/theme';

/** html의 class 변화를 구독한다. 다른 탭이 아니라 이 문서 안의 변화만 본다. */
function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  return () => observer.disconnect();
}

/** 서버는 모드를 모른다. 서버 HTML은 주간 기준 문구로 그리고, 붙은 뒤 실제 모드로 바꾼다. */
const serverTheme = (): Theme => 'light';

export function ThemeToggle() {
  const theme = useSyncExternalStore(subscribe, readTheme, serverTheme);
  const next: Theme = theme === 'dark' ? 'light' : 'dark';
  const label = next === 'dark' ? '야간 모드' : '주간 모드';

  return (
    <button
      type="button"
      onClick={() => applyTheme(next)}
      aria-label={`${label}로 바꾸기`}
      className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-md border border-white/20 px-3 text-[13px] text-white/90 transition-colors hover:bg-white/10 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
    >
      {next === 'dark' ? <Moon aria-hidden className="size-4" /> : <Sun aria-hidden className="size-4" />}
      <span className="hidden sm:inline">{label}</span>
    </button>
  );
}
