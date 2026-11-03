'use client';

// 고객사 대시보드 자산 현황 카드의 위 구역 "유형별 자산 추이"(2026-11-03, 종전 별도 카드 "자산 수 흐름"). 기간은 하루 · 3일 · 일주일 · 2주다. 서버 `/metrics/timeseries`가
// `hours`를 1-336으로 막아 2주가 끝이다. 3일(72)은 서버 기본 창이라 페이지가 그린 값을 그대로 쓴다.

import { useEffect, useState } from 'react';

import { InventoryTrendChart, type InventoryCosts } from '@/components/dashboard/trend-charts';
import { RangeToggle } from '@/components/range-toggle';
import { getMetricsTimeseries } from '@/lib/api/client';
import type { AssetInventoryAxis } from '@/types/api';

const RANGES = [
  { value: 24, label: '하루' },
  { value: 72, label: '3일' },
  { value: 168, label: '일주일' },
  { value: 336, label: '2주' },
] as const;
type RangeHours = (typeof RANGES)[number]['value'];
const SERVER_DEFAULT: RangeHours = 72;

type Load = { kind: 'loading' } | { kind: 'ready'; axis: AssetInventoryAxis } | { kind: 'failed' };

export function InventoryTrendSection({ initial, costs = null }: { initial: AssetInventoryAxis | null; costs?: InventoryCosts | null }) {
  const [hours, setHours] = useState<RangeHours>(SERVER_DEFAULT);
  // 기간마다 받은 축을 기억한다. 돌아갈 때 다시 부르지 않는다.
  const [loads, setLoads] = useState<Partial<Record<RangeHours, Load>>>({
    [SERVER_DEFAULT]: initial === null ? { kind: 'failed' } : { kind: 'ready', axis: initial },
  });
  const load: Load = loads[hours] ?? { kind: 'loading' };

  useEffect(() => {
    if (loads[hours] !== undefined) return;
    let alive = true;
    getMetricsTimeseries(hours)
      .then((m) => alive && setLoads((s) => ({ ...s, [hours]: { kind: 'ready', axis: m.asset_inventory } })))
      .catch(() => alive && setLoads((s) => ({ ...s, [hours]: { kind: 'failed' } })));
    return () => {
      alive = false;
    };
  }, [hours, loads]);

  return (
    <section aria-labelledby="inventory-trend-title" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="inventory-trend-title" className="text-sm font-semibold">
          유형별 자산 추이
        </h3>
        <RangeToggle options={RANGES} value={hours} onChange={setHours} />
      </div>
      {load.kind === 'failed' ? (
        <p className="text-muted-foreground text-sm">추이를 불러오지 못했습니다.</p>
      ) : (
        <div className="relative h-36">
          {load.kind === 'loading' ? (
            <div aria-busy className="bg-muted/50 absolute inset-0 animate-pulse rounded-md" />
          ) : (
            <InventoryTrendChart axis={load.axis} fill costs={costs} />
          )}
        </div>
      )}
    </section>
  );
}
