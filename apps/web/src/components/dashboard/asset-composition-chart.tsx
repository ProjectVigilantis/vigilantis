'use client';

// DSH-001 「자산 분류 비율」 — 수집된 자산이 유형별로 어떻게 나뉘는지의 도넛.
//
// 옆의 「자산 현황 추이」가 **시간**으로 편 것이라면 이쪽은 **지금의 구성**이다. 셈은 `lib/dashboard`의
// `assetComposition`이 하고 여기는 그리기만 한다 — 조각을 무엇으로 접는지(판정 비대상 → 한 조각)도
// 그쪽이 정한다.
//
// **도넛은 비슷한 값을 견주기에 약하다**(EBS 5 · EC2 4 · 보안 그룹 4처럼 호 길이로는 가릴 수 없다).
// 그래서 호에만 기대지 않는다 — 범례가 조각마다 **건수와 비율을 글자로** 적고, 가운데에 전량을 둔다.
// 호는 "대략 어떤 구성인가"를, 숫자는 "정확히 몇 건인가"를 맡는다.
//
// 색은 자산을 따른다(건수 순위가 아니다). 판정 대상 3종은 검증된 계열 색의 앞 세 자리를 유형마다
// 고정해 쓰고(trend-charts.tsx `SERIES_COLORS`와 같은 토큰), 접힌 조각은 계열이 아니라 나머지라 회색이다.
// 글자는 계열 색을 입지 않는다 — 색은 옆의 표식이 나른다.

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';

import type { AssetComposition, CompositionKey, CompositionSlice } from '@/lib/dashboard';
import { ASSET_TYPE_LABELS } from '@/lib/enum-labels';
import type { AssetType } from '@/types/api';

/** 조각 색 — 옆 「자산 현황 추이」(`trend-charts.tsx` `InventoryTrendChart`)도 같은 표를 쓴다. */
export const SLICE_COLOR: Record<CompositionKey, string> = {
  EC2: 'var(--chart-series-1)',
  SG: 'var(--chart-series-2)',
  EBS: 'var(--chart-series-3)',
  OTHER: 'color-mix(in oklch, var(--muted-foreground) 55%, transparent)',
};

const typeLabel = (type: AssetType) => ASSET_TYPE_LABELS[type]?.label ?? type;
export const sliceLabel = (key: CompositionKey) => (key === 'OTHER' ? '판정 비대상' : typeLabel(key));
const percent = (ratio: number) => `${Math.round(ratio * 100)}%`;

const TOOLTIP_STYLE = {
  backgroundColor: 'var(--popover)',
  border: '1px solid var(--border)',
  borderRadius: '0.5rem',
  fontSize: '0.75rem',
  color: 'var(--popover-foreground)',
} as const;

function SliceTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: ReadonlyArray<{ payload?: CompositionSlice }>;
}) {
  const slice = payload?.[0]?.payload;
  if (!active || slice === undefined) return null;
  return (
    // 도넛 박스(size-44)가 좁아 폭을 부모에 맡기면 `EC2 인스턴스 4건 · 31%`가 줄바꿈된다 — 내용 폭으로 편다.
    <div style={TOOLTIP_STYLE} className="flex w-max flex-col gap-1 px-3 py-2 whitespace-nowrap">
      <span className="flex items-center justify-between gap-6">
        <span className="flex items-center gap-1.5">
          <span
            aria-hidden
            className="inline-block size-2 rounded-full"
            style={{ backgroundColor: SLICE_COLOR[slice.key] }}
          />
          {sliceLabel(slice.key)}
        </span>
        <span className="font-mono tabular-nums">
          {slice.count}건 · {percent(slice.ratio)}
        </span>
      </span>
      {slice.members.map((m) => (
        <span key={m.type} className="text-muted-foreground flex justify-between gap-6 pl-3.5">
          <span>{typeLabel(m.type)}</span>
          <span className="font-mono tabular-nums">{m.count}건</span>
        </span>
      ))}
    </div>
  );
}

export function AssetCompositionChart({ composition }: { composition: AssetComposition }) {
  const { total, slices, uncollected } = composition;

  if (total === 0) {
    return (
      <p className="text-muted-foreground flex h-56 items-center justify-center text-sm">
        수집된 자산이 없습니다
      </p>
    );
  }

  return (
    // 높이를 옆 추이 차트(`ChartFrame` h-56)와 맞춘다 — 두 카드의 아랫변이 어긋나지 않게.
    <div className="flex min-h-56 flex-wrap items-center justify-center gap-x-6 gap-y-4">
      <div className="relative size-44 shrink-0">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Tooltip
              content={(props) => <SliceTooltip {...props} />}
              // 박스 밖으로 나갈 수 있어야 넓어진 툴팁이 도넛 가장자리에서 잘리거나 밀려 접히지 않는다.
              allowEscapeViewBox={{ x: true, y: true }}
              // 가운데 전량 라벨(아래 absolute)보다 위에 뜬다.
              wrapperStyle={{ zIndex: 20 }}
              // Recharts 기본은 위치 이동을 transition으로 그려, 첫 등장 때 원점(왼쪽 위)에서 포인터까지
              // 끌려온다. 포인터 자리에 바로 선다.
              isAnimationActive={false}
            />
            <Pie
              data={slices}
              dataKey="count"
              nameKey="key"
              innerRadius="64%"
              outerRadius="100%"
              // 12시에서 시계 방향 — 첫 조각이 위에서 시작해야 순서가 범례와 같게 읽힌다.
              startAngle={90}
              endAngle={-270}
              // 조각 사이는 테두리가 아니라 **카드 색 틈**으로 가른다.
              stroke="var(--card)"
              strokeWidth={2}
              isAnimationActive={false}
            >
              {slices.map((slice) => (
                <Cell key={slice.key} fill={SLICE_COLOR[slice.key]} />
              ))}
            </Pie>
          </PieChart>
        </ResponsiveContainer>
        {/* 가운데 전량 — 지표 띠의 `전체 자산`과 같은 수다. 호버를 가로채지 않게 포인터를 뺀다.
            z-0으로 툴팁(z-20) 아래에 깔린다 — DOM상 뒤에 있어 그대로 두면 툴팁 위로 올라온다. */}
        <div className="pointer-events-none absolute inset-0 z-0 flex flex-col items-center justify-center">
          <span className="font-mono text-2xl font-medium tabular-nums">{total}</span>
          <span className="text-muted-foreground text-xs">전체 자산</span>
        </div>
      </div>

      {/* 범례 — 조각마다 건수와 비율을 글자로 적는다(파일 머리말). 접힌 조각은 무엇이 접혔는지 풀어 적는다. */}
      <ul className="flex min-w-40 flex-col gap-2 text-xs">
        {slices.map((slice) => (
          <li key={slice.key} className="flex flex-col gap-0.5">
            <span className="flex items-center justify-between gap-4">
              <span className="flex items-center gap-1.5">
                <span
                  aria-hidden
                  className="inline-block size-2.5 rounded-sm"
                  style={{ backgroundColor: SLICE_COLOR[slice.key] }}
                />
                {sliceLabel(slice.key)}
              </span>
              <span className="font-mono tabular-nums">
                {slice.count}
                <span className="text-muted-foreground"> · {percent(slice.ratio)}</span>
              </span>
            </span>
            {slice.members.length > 0 ? (
              <span className="text-muted-foreground pl-4">
                {slice.members.map((m) => `${typeLabel(m.type)} ${m.count}`).join(' · ')}
              </span>
            ) : null}
          </li>
        ))}
        {/* 조회를 못 한 유형은 0으로 적지 않는다 — 비율에 없다는 사실을 말한다(자산 인벤토리와 같은 규칙). */}
        {uncollected.length > 0 ? (
          <li className="border-t pt-2 text-amber-400">
            수집 실패 {uncollected.length}종은 비율에 없습니다
            <span className="text-muted-foreground block">
              {uncollected.map(typeLabel).join(' · ')}
            </span>
          </li>
        ) : null}
      </ul>
    </div>
  );
}
