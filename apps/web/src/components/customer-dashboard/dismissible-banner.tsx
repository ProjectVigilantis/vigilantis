'use client';

// 대시보드 제목 옆 관제 상태 알림의 닫기(2026-11-03). 닫은 내용(`dismissKey`)은 탭을 닫을 때까지 숨긴다.
// 상태가 바뀌어 문구가 달라지면 키가 달라져 다시 보인다. 새 위협을 닫힌 알림 뒤에 숨기지 않기 위해서다.

import { X } from 'lucide-react';
import { useSyncExternalStore } from 'react';

const STORAGE_KEY = 'vigilantis-dismissed-banner';
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function DismissibleBanner({
  dismissKey,
  label,
  className,
  children,
}: {
  dismissKey: string;
  label: string;
  className?: string;
  children: React.ReactNode;
}) {
  const dismissed = useSyncExternalStore(
    subscribe,
    () => sessionStorage.getItem(STORAGE_KEY) === dismissKey,
    () => false,
  );
  if (dismissed) return null;

  return (
    <section aria-label={label} className={className}>
      {children}
      <button
        type="button"
        aria-label="알림 닫기"
        onClick={() => {
          sessionStorage.setItem(STORAGE_KEY, dismissKey);
          for (const l of listeners) l();
        }}
        className="text-muted-foreground focus-visible:ring-ring inline-flex size-6 shrink-0 items-center justify-center rounded-full focus-visible:ring-2 focus-visible:outline-none"
      >
        <X aria-hidden className="size-3.5" />
      </button>
    </section>
  );
}
