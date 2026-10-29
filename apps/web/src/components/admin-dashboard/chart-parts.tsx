// 관리자 대시보드 그래프 카드가 함께 쓰는 부품(DESIGN.md §데이터 시각화).
//   BarRows: 항목끼리 건수 비교 — 가로 막대, 파랑 한 계열. 상태 색은 이름 옆 점에만 쓴다.
//   ShareBar: 전체 중 비율 — 한 줄 누적 막대와 건수 범례. 값이 상태라 상태 색을 쓴다.
//   MoreLink: 카드 아래 상세 화면으로 가는 링크. 대시보드는 숫자와 그래프만 두고 목록은 상세 화면에서 본다.
// 훅이 없어 `'use client'`를 붙이지 않는다.

import { ChevronRight } from 'lucide-react';
import Link from 'next/link';

import { cn } from '@/lib/utils';

export interface BarRow {
  key: string;
  label: string;
  value: number;
  /** 이름 옆 상태 점의 색 클래스. 없으면 점을 그리지 않는다. */
  dot?: string;
  /** 마우스를 올리면 보이는 설명. */
  title?: string;
  href?: string;
}

export function BarRows({ rows, labelWidth = 84 }: { rows: readonly BarRow[]; labelWidth?: number }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <ul className="flex flex-col gap-2">
      {rows.map((r) => {
        const label = (
          <span className="inline-flex min-w-0 items-center gap-1.5 text-[13px]">
            {r.dot ? <span aria-hidden className={cn('size-2.5 shrink-0 rounded-full', r.dot)} /> : null}
            <span className="truncate">{r.label}</span>
          </span>
        );
        return (
          <li
            key={r.key}
            title={r.title}
            className="grid items-center gap-2.5"
            style={{ gridTemplateColumns: `${labelWidth}px minmax(0,1fr) 36px` }}
          >
            {r.href ? (
              <Link href={r.href} className="min-w-0 hover:underline">
                {label}
              </Link>
            ) : (
              label
            )}
            <div className="bg-data-track h-3.5 rounded-[3px]">
              {r.value > 0 ? <div className="bg-data h-3.5 rounded-r-[3px]" style={{ width: `${(r.value / max) * 100}%` }} /> : null}
            </div>
            <span className="text-right text-[13px] font-bold tabular-nums">{r.value}</span>
          </li>
        );
      })}
    </ul>
  );
}

export interface SharePart {
  key: string;
  label: string;
  value: number;
  fill: string;
}

export function ShareBar({ parts }: { parts: readonly SharePart[] }) {
  const total = parts.reduce((s, p) => s + p.value, 0);
  const shown = parts.filter((p) => p.value > 0);
  return (
    <div className="flex flex-col gap-2">
      <div role="img" aria-label={parts.map((p) => `${p.label} ${p.value}`).join(', ')} className="bg-heat-none flex h-3 gap-0.5 rounded-sm">
        {shown.map((p, i) => (
          <div
            key={p.key}
            title={`${p.label} ${p.value} (${Math.round((p.value / total) * 100)}%)`}
            className={cn(p.fill, i === 0 && 'rounded-l-sm', i === shown.length - 1 && 'rounded-r-sm')}
            style={{ width: `${(p.value / total) * 100}%` }}
          />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {parts.map((p) => (
          <li key={p.key} className="inline-flex items-center gap-1.5">
            <span aria-hidden className={cn('size-2.5 rounded-[3px]', p.fill)} />
            {p.label}
            <span className="font-bold tabular-nums">{p.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function MoreLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link href={href} className="text-primary mt-auto inline-flex w-fit items-center gap-0.5 pt-2 text-sm font-semibold hover:underline">
      {children}
      <ChevronRight aria-hidden className="size-4" />
    </Link>
  );
}

/** 그래프 카드 안에서 묶음 하나의 작은 제목. */
export function ChartSection({ title, children, className }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <p className="text-muted-foreground text-xs font-semibold">{title}</p>
      {children}
    </div>
  );
}

export interface HistogramBin {
  key: string;
  /** 가로축 눈금 글자. */
  label: string;
  /** 칸이 차지하는 값 범위(가로축 단위). */
  from: number;
  to: number;
  value: number;
  title: string;
}

/** 점들을 잇는 부드러운 곡선(Catmull-Rom → 3차 베지어). 곡선이 바닥 아래로 내려가지 않게 조절점 높이를 자른다. */
function curvePath(pts: readonly [number, number][]): string {
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i += 1) {
    const p0 = pts[Math.max(0, i - 1)];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[Math.min(pts.length - 1, i + 2)];
    const c1y = Math.min(100, p1[1] + (p2[1] - p0[1]) / 6);
    const c2y = Math.min(100, p2[1] - (p3[1] - p1[1]) / 6);
    d += ` C ${p1[0] + (p2[0] - p0[0]) / 6} ${c1y} ${p2[0] - (p3[0] - p1[0]) / 6} ${c2y} ${p2[0]} ${p2[1]}`;
  }
  return d;
}

/**
 * 분포 그래프: 세로 막대(히스토그램) + 막대 위를 잇는 곡선. 가로축은 `domain` 범위의 연속 눈금이고 칸은 제 값 자리에 놓인다.
 * 곡선은 범위 양 끝에서 바닥으로 내려 분포 모양(종 모양)이 보이게 한다.
 */
export function Histogram({
  bins,
  domain: [lo, hi],
  axisLabel,
}: {
  bins: readonly HistogramBin[];
  domain: readonly [number, number];
  /** 가로축 이름(단위). */
  axisLabel: string;
}) {
  const max = Math.max(1, ...bins.map((b) => b.value));
  const x = (v: number) => ((v - lo) / (hi - lo)) * 100;
  const y = (v: number) => 100 - (v / max) * 100;
  const curve = curvePath([[0, 100], ...bins.map((b): [number, number] => [x((b.from + b.to) / 2), y(b.value)]), [100, 100]]);
  // 칸이 많으면 눈금 글자를 하나 걸러 적는다.
  const step = bins.length > 10 ? 2 : 1;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex gap-2">
        <div className="text-muted-foreground relative h-28 w-5 shrink-0 text-[11px]">
          <span className="absolute top-0 right-0 -translate-y-1/2">{max}</span>
          <span className="absolute right-0 bottom-0 translate-y-1/2">0</span>
        </div>
        <div className="border-axis relative h-28 flex-1 border-b">
          <div aria-hidden className="border-gridline absolute inset-x-0 top-0 border-t" />
          {bins.map((b) => (
            <div
              key={b.key}
              title={b.title}
              className="group absolute inset-y-0 flex items-end px-px"
              style={{ left: `${Math.max(0, x(b.from))}%`, width: `${Math.min(100, x(b.to)) - Math.max(0, x(b.from))}%` }}
            >
              <div
                className="bg-data/45 group-hover:bg-data w-full rounded-t-[3px] transition-colors"
                style={{ height: b.value === 0 ? 0 : `${(b.value / max) * 100}%` }}
              />
            </div>
          ))}
          <svg aria-hidden viewBox="0 0 100 100" preserveAspectRatio="none" className="pointer-events-none absolute inset-0 size-full overflow-visible">
            <path d={curve} fill="none" className="stroke-data" strokeWidth={2} vectorEffect="non-scaling-stroke" />
          </svg>
        </div>
      </div>
      <div className="text-muted-foreground relative ml-7 h-4 text-[11px]">
        {bins.map((b, i) =>
          i % step === 0 ? (
            <span key={b.key} className="absolute -translate-x-1/2" style={{ left: `${x((b.from + b.to) / 2)}%` }}>
              {b.label}
            </span>
          ) : null,
        )}
      </div>
      <p className="text-muted-foreground pl-7 text-center text-[11px]">{axisLabel}</p>
    </div>
  );
}
