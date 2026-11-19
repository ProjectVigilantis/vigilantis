'use client';

// 카드 머리의 기간 전환 버튼 묶음. 위협 이벤트 · 자산 수 흐름 카드가 같이 쓴다.

import { cn } from '@/lib/utils';

export function RangeToggle<T extends number>({
  options,
  value,
  onChange,
}: {
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div role="group" aria-label="기간" className="flex gap-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            'focus-visible:ring-ring h-7 rounded px-2 text-xs whitespace-nowrap transition-colors focus-visible:ring-2 focus-visible:outline-none',
            value === o.value ? 'text-foreground font-semibold' : 'text-foreground-secondary hover:text-foreground',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
