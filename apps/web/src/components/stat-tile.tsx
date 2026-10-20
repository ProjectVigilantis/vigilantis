// 숫자 카드: 이름 · 값 · 한 줄 설명의 세 줄이다(DESIGN.md §숫자 카드). 카드끼리 16px 간격을 둔
// 낱장이며, 붙은 띠(metric-strip.tsx)와 달리 화면 폭에 따라 줄이 바뀐다.
//
// 훅이 없어 `'use client'`를 붙이지 않는다. 서버 · 클라이언트 컴포넌트 어디서나 쓴다.

import { cn } from '@/lib/utils';

/** 문제가 있는 값만 상태 글자색으로 칠한다. 평소 값은 기본 글자색이다. */
const TONE = {
  critical: 'text-badge-critical-fg',
  serious: 'text-badge-serious-fg',
  warning: 'text-badge-warning-fg',
  good: 'text-badge-good-fg',
} as const;

export type StatTone = keyof typeof TONE;

export function StatGrid({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={cn('grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-4', className)}>
      {children}
    </div>
  );
}

export function StatTile({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: React.ReactNode;
  note?: string;
  tone?: StatTone;
}) {
  return (
    <div className="bg-card ring-foreground/10 flex flex-col gap-1.5 rounded-xl px-5 py-4 ring-1">
      <span className="text-muted-foreground text-xs font-semibold tracking-wide">{label}</span>
      <span className={cn('text-[30px] leading-[38px] font-bold', tone && TONE[tone])}>{value}</span>
      {note ? <span className="text-muted-foreground text-xs">{note}</span> : null}
    </div>
  );
}
