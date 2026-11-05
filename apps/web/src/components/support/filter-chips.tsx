'use client';

// 도입 신청함 · 고객 문의함의 분류 칩. 관리자 대시보드 문제 표(admin-dashboard/issues-table.tsx)의 칩과 같은 모양이다.

import { cn } from '@/lib/utils';

export function FilterChips<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: { value: T; label: string; count: number }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-1.5">
      {options.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(option.value)}
            className={cn(
              'focus-visible:ring-ring h-8 rounded-full border px-3 text-[13px] transition-colors focus-visible:ring-2 focus-visible:outline-none',
              on ? 'border-primary bg-accent text-accent-foreground font-semibold' : 'text-foreground-secondary hover:bg-muted',
            )}
          >
            {option.label} <span className="tabular-nums">{option.count}</span>
          </button>
        );
      })}
    </div>
  );
}
