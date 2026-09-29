'use client';

// 필터 토글 한 개 — 체크박스 + 배지. 자산 관제(AST-001)·보안 관제(SEC-001)의 필터 줄이 공유한다.
// 종전에는 assets-view.tsx 안의 지역 컴포넌트였다(2026-09-28 분리).

import { Badge } from '@/components/ui/badge';
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
    <label className="flex cursor-pointer items-center gap-1.5 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        aria-label={label}
        className="accent-primary size-4"
      />
      <Badge variant="outline" className={cn(checked && 'border-ring text-foreground')}>
        {label}
      </Badge>
    </label>
  );
}
