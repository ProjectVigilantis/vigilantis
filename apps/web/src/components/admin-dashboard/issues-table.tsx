'use client';

// 수동 처리가 필요한 문제 표. 고객이 많아져도 높이가 고정된다: 위에 분류별 건수(누르면 그 분류만), 아래는 한 건 한 줄
// 표이고 일정 높이를 넘으면 표 안에서 스크롤한다. 머리줄은 스크롤해도 남는다.
// 분류 건수 → 걸러진 목록으로 내려가는 틀은 AWS Security Hub 요약 대시보드의 `Top risks` 위젯과 같다.

import { ChevronRight } from 'lucide-react';
import Link from 'next/link';
import { useState } from 'react';

import { formatAge, type IssueKind, type IssueRow } from '@/lib/admin-dashboard';
import { cn } from '@/lib/utils';

type Filter = 'ALL' | IssueKind;

const FILTERS: readonly { value: Filter; label: string }[] = [
  { value: 'ALL', label: '전체' },
  { value: 'CONNECTION', label: '연결 실패' },
  { value: 'STUCK', label: '진행 불가 인시던트' },
];

const DOT: Record<IssueKind, string> = { CONNECTION: 'bg-status-critical', STUCK: 'bg-status-serious' };

export function IssuesTable({ rows, nowMs }: { rows: readonly IssueRow[]; nowMs: number }) {
  const [filter, setFilter] = useState<Filter>('ALL');
  const shown = filter === 'ALL' ? rows : rows.filter((r) => r.kind === filter);
  const count = (f: Filter) => (f === 'ALL' ? rows.length : rows.filter((r) => r.kind === f).length);

  if (rows.length === 0) {
    return <p className="text-muted-foreground text-sm">지금 수동으로 처리할 문제가 없습니다.</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      <div role="group" aria-label="문제 분류" className="flex flex-wrap gap-1.5">
        {FILTERS.map(({ value, label }) => {
          const on = filter === value;
          return (
            <button
              key={value}
              type="button"
              aria-pressed={on}
              onClick={() => setFilter(value)}
              className={cn(
                'focus-visible:ring-ring h-8 rounded-full border px-3 text-[13px] transition-colors focus-visible:ring-2 focus-visible:outline-none',
                on ? 'border-primary bg-accent text-accent-foreground font-semibold' : 'text-foreground-secondary hover:bg-muted',
              )}
            >
              {label} <span className="tabular-nums">{count(value)}</span>
            </button>
          );
        })}
      </div>
      <div className="max-h-80 overflow-y-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-secondary text-foreground-secondary sticky top-0 z-10 text-left text-xs">
            <tr>
              <th scope="col" className="w-36 px-3 py-2 font-semibold">고객사</th>
              <th scope="col" className="px-3 py-2 font-semibold">문제</th>
              <th scope="col" className="w-24 px-3 py-2 text-right font-semibold">경과</th>
              <th scope="col" className="w-8"><span className="sr-only">열기</span></th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {shown.map((r) => (
              <tr key={r.key} className="hover:bg-muted relative">
                <td className="truncate px-3 py-2 font-semibold">{r.tenantName}</td>
                <td className="px-3 py-2">
                  <span className="flex items-center gap-2">
                    <span aria-hidden className={cn('size-2 shrink-0 rounded-full', DOT[r.kind])} />
                    {/* 줄 전체를 누를 수 있게 링크를 늘린다(after:inset-0). 표 의미는 그대로 남는다. */}
                    <Link href={r.href} className="line-clamp-1 after:absolute after:inset-0">
                      {r.title}
                    </Link>
                  </span>
                </td>
                <td className="text-muted-foreground px-3 py-2 text-right text-xs whitespace-nowrap">
                  {r.at === '' ? '—' : formatAge(r.at, nowMs)}
                </td>
                <td className="pr-2">
                  <ChevronRight aria-hidden className="text-muted-foreground size-4" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
