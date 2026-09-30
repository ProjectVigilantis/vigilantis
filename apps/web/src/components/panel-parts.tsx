// 요약 패널 안에서 되풀이되는 조각 셋 — 자산 관제(AST-001)·보안 관제(SEC-001)의 요약 패널이 공유한다.
// 훅·이벤트가 없어 `'use client'`를 붙이지 않는다(panel.tsx와 같은 이유).

import { cn } from '@/lib/utils';

/** 패널 맨 아래 한 줄 집계 — 본문과 윗선으로 가른다. */
export function StatLine({ label, value }: { label: string; value: number }) {
  return (
    <div className="mt-3 flex items-center justify-between border-t pt-3 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono tabular-nums">{value}</span>
    </div>
  );
}

/** 카드 안에서 값 대신 쓰는 한 줄. 어느 화면이든 같은 무게라야 나란히 선 카드가 맞는다. */
export function Muted({ children }: { children: React.ReactNode }) {
  return <p className="text-muted-foreground py-4 text-center text-sm">{children}</p>;
}

/** 비율 막대 — 색은 호출부가 준다(판정 색·상태 색이 축마다 다르다). */
export function Bar({ ratio, className }: { ratio: number; className: string }) {
  return (
    <span className="bg-muted block h-1.5 overflow-hidden rounded-full">
      <span className={cn('block h-full rounded-full', className)} style={{ width: `${ratio * 100}%` }} />
    </span>
  );
}
