'use client';

// 고객사 대시보드의 "위협 이벤트"(2026-11-03, 종전 "최근 24시간 위협 이벤트" 시간별 막대를 바꿨다).
// 이벤트 하나하나가 언제 일어났는지를 보인다: 위는 기간 위 발생 시각 눈금, 아래는 최근 것부터의 목록이다.
// 기간은 하루 · 일주일 · 한 달. 계약은 types/api.ts `ThreatEventsResponse`(#572 10번). 서버가 그 경로를 내지 않으면(404 · 405) 그렇다고 적는다.

import { useEffect, useState } from 'react';

import { TIP_ANCHOR, TipBox } from '@/components/admin-dashboard/chart-parts';
import { ChartCard } from '@/components/chart-card';
import { RangeToggle } from '@/components/range-toggle';
import { ApiError, getThreatEvents } from '@/lib/api/client';
import { notYetServed } from '@/lib/support';
import {
  THREAT_KIND_LABELS,
  THREAT_RANGES,
  threatDetail,
  threatKindCounts,
  threatSource,
  timelinePercent,
  type ThreatEventItem,
  type ThreatEventsResponse,
  type ThreatRangeDays,
} from '@/lib/threat-events';
import { cn } from '@/lib/utils';
import type { ThreatEventKind } from '@/types/api';

// 무차별 대입은 실제 공격 시도라 빨강, 전체 개방은 노출 상태라 주황이다(DESIGN.md §상태 색).
const KIND_FILL: Record<ThreatEventKind, string> = {
  SSH_BRUTE_FORCE: 'bg-status-critical',
  OPEN_IP: 'bg-status-serious',
};

type Load =
  | { kind: 'loading' }
  | { kind: 'ready'; data: ThreatEventsResponse }
  | { kind: 'missing' }
  | { kind: 'failed'; message: string };

const kst = (iso: string, opts: Intl.DateTimeFormatOptions) =>
  new Date(iso).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hourCycle: 'h23', ...opts });
const stamp = (iso: string) => kst(iso, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
const axisLabel = (iso: string, days: ThreatRangeDays) =>
  days === 1 ? kst(iso, { hour: '2-digit', minute: '2-digit' }) : kst(iso, { month: 'numeric', day: 'numeric' });

export function ThreatEventsCard({ assetNames, className }: { assetNames: Record<string, string>; className?: string }) {
  const [days, setDays] = useState<ThreatRangeDays>(1);
  // 기간마다 받은 응답을 기억한다. 돌아갈 때 다시 부르지 않는다.
  const [loads, setLoads] = useState<Partial<Record<ThreatRangeDays, Load>>>({});
  const load: Load = loads[days] ?? { kind: 'loading' };

  useEffect(() => {
    if (loads[days] !== undefined) return;
    let alive = true;
    getThreatEvents(days)
      .then((data) => alive && setLoads((m) => ({ ...m, [days]: { kind: 'ready', data } })))
      .catch((cause: unknown) => {
        if (!alive) return;
        const next: Load =
          cause instanceof ApiError && notYetServed(cause.httpStatus)
            ? { kind: 'missing' }
            : { kind: 'failed', message: cause instanceof ApiError ? cause.message : '위협 이벤트를 불러오지 못했습니다.' };
        setLoads((m) => ({ ...m, [days]: next }));
      });
    return () => {
      alive = false;
    };
  }, [days, loads]);

  const total = load.kind === 'ready' ? load.data.total : null;

  return (
    <ChartCard
      className={className}
      title="위협 이벤트"
      description="발생 시각 · 무차별 대입 · 전체 개방 포트"
      aside={
        <div className="flex items-center gap-3">
          {total !== null ? (
            <span className="text-foreground-secondary text-[13px] whitespace-nowrap">
              <strong className="text-foreground text-base">{total}</strong>건
            </span>
          ) : null}
          <RangeToggle options={THREAT_RANGES.map((r) => ({ value: r.days, label: r.label }))} value={days} onChange={setDays} />
        </div>
      }
    >
      {load.kind === 'loading' ? (
        <div aria-busy className="bg-muted/50 h-64 animate-pulse rounded-md" />
      ) : load.kind === 'missing' ? (
        <p className="text-muted-foreground text-sm">위협 이벤트 목록 API가 서버에 아직 없습니다.</p>
      ) : load.kind === 'failed' ? (
        <p className="text-muted-foreground text-sm">{load.message}</p>
      ) : (
        <Timeline data={load.data} days={days} assetNames={assetNames} />
      )}
    </ChartCard>
  );
}

function Timeline({ data, days, assetNames }: { data: ThreatEventsResponse; days: ThreatRangeDays; assetNames: Record<string, string> }) {
  const { items, window_start: start, window_end: end } = data;
  const counts = threatKindCounts(items);
  const mid = new Date((Date.parse(start) + Date.parse(end)) / 2).toISOString();
  const name = (arn: string) => assetNames[arn] ?? arn.split('/').pop() ?? arn;

  if (items.length === 0) {
    return <p className="text-foreground-secondary py-2 text-[15px]">이 기간에 들어온 위협 이벤트가 없습니다.</p>;
  }

  return (
    <div className="flex flex-1 flex-col gap-4">
      {/* 발생 시각 눈금. 유형마다 한 줄이라 겹쳐도 색이 섞이지 않는다. */}
      <div className="flex flex-col gap-1.5">
        {(Object.keys(THREAT_KIND_LABELS) as ThreatEventKind[]).map((kind) => (
          <div key={kind} className="flex items-center gap-3">
            <span className="text-muted-foreground w-34 shrink-0 text-xs whitespace-nowrap">
              {THREAT_KIND_LABELS[kind]} <strong className="text-foreground">{counts[kind]}</strong>
            </span>
            <div className="bg-muted/60 relative h-6 flex-1 rounded-sm">
              {items
                .filter((i) => i.event_type === kind)
                .map((i) => {
                  const left = timelinePercent(i.occurred_at, start, end);
                  return (
                    <span
                      key={i.threat_event_id}
                      className={cn(TIP_ANCHOR, 'absolute inset-y-0 -ml-[3px] w-1.5 hover:z-10')}
                      style={{ left: `${left}%` }}
                    >
                      <span className={cn('absolute inset-y-1 left-1/2 w-0.5 -translate-x-1/2 rounded-full opacity-80', KIND_FILL[kind])} />
                      <TipBox align={left < 33 ? 'start' : left > 66 ? 'end' : 'center'}>
                        <span className="font-medium tabular-nums">{stamp(i.occurred_at)}</span>
                        <span className="text-muted-foreground">
                          {threatSource(i).value} → {name(i.target_arn)}
                        </span>
                      </TipBox>
                    </span>
                  );
                })}
            </div>
          </div>
        ))}
        <div className="text-muted-foreground flex justify-between pl-37 text-[11px] tabular-nums">
          <span>{axisLabel(start, days)}</span>
          <span>{axisLabel(mid, days)}</span>
          <span>{axisLabel(end, days)}</span>
        </div>
      </div>

      {/* 최근 것부터. 목록은 카드의 남은 높이를 채우고(옆 칸이 길면 같이 길어진다, 최소 h-64) 넘치면 안에서 스크롤한다.
          absolute 스크롤 상자라 기간이 길어져도 카드가 늘어나지 않고, 안쪽 sr-only(absolute)도 이 상자 밖으로 빠지지 않는다. */}
      <div className="relative min-h-64 flex-1 border-t">
        <div className="absolute inset-0 overflow-y-auto">
          <table className="w-full text-[13px]">
            <caption className="sr-only">위협 이벤트 목록(최근 것부터)</caption>
            <thead className="bg-card text-muted-foreground sticky top-0 text-left text-xs">
              <tr>
                <th className="py-2 pr-3 font-semibold">발생 시각(KST)</th>
                <th className="py-2 pr-3 font-semibold">유형</th>
                <th className="py-2 pr-3 font-semibold">출발지</th>
                <th className="py-2 font-semibold">대상</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {items.map((i) => (
                <EventRow key={i.threat_event_id} item={i} target={name(i.target_arn)} />
              ))}
            </tbody>
          </table>
          {data.total > items.length ? (
            <p className="text-muted-foreground py-2 text-xs">최근 {items.length}건만 보입니다(전체 {data.total}건).</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function EventRow({ item, target }: { item: ThreatEventItem; target: string }) {
  const source = threatSource(item);
  return (
    <tr className="align-top">
      <td className="py-2 pr-3 font-mono text-xs whitespace-nowrap tabular-nums">{stamp(item.occurred_at)}</td>
      <td className="py-2 pr-3 whitespace-nowrap">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden className={cn('size-2 rounded-full', KIND_FILL[item.event_type])} />
          {THREAT_KIND_LABELS[item.event_type]}
        </span>
        <span className="text-muted-foreground block text-xs">{threatDetail(item)}</span>
      </td>
      <td className="py-2 pr-3 font-mono text-xs whitespace-nowrap">
        <span className="sr-only">{source.label} </span>
        {source.value}
      </td>
      <td className="py-2 break-all">{target}</td>
    </tr>
  );
}
