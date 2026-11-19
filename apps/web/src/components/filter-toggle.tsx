'use client';

// 필터 토글 한 개 — 체크박스 + 글자. 자산 관제(AST-001)·보안 관제(SEC-001)의 필터 줄이 공유한다.
// 종전에는 assets-view.tsx 안의 지역 컴포넌트였다(2026-09-28 분리).
// 글자는 테두리 배지였다가 바탕 없는 글자로 바뀌었다(2026-11-19, DESIGN.md §버튼).

import { cn } from '@/lib/utils';

export function FilterToggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="group flex cursor-pointer items-center gap-1.5 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        aria-label={label}
        className="accent-primary size-4"
      />
      <span className={cn('transition-colors', checked ? 'text-foreground font-semibold' : 'text-foreground-secondary group-hover:text-foreground')}>
        {label}
      </span>
    </label>
  );
}
