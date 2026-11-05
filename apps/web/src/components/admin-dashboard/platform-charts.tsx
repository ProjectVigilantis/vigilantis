'use client';

// 플랫폼 상태의 두 그래프(수집 주기 소요 시간 · 시간별 AI 호출). 폭을 따라 늘고 줄며, 막대에 올리면 값이 뜬다.
// 축 · 격자 · 말풍선 잉크는 대시보드 추이 그래프(components/dashboard/trend-charts.tsx)와 같다.

import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import { BAR_CURSOR, TOOLTIP_ITEM_STYLE, TOOLTIP_LABEL_STYLE, TOOLTIP_STYLE } from '@/components/charts/tooltip-style';
import type { HourlyCount, ScanCycle } from '@/lib/platform-status';

const AXIS_INK = 'var(--muted-foreground)';
const GRID_INK = 'color-mix(in oklch, var(--muted-foreground) 25%, transparent)';
const OK_FILL = 'var(--primary)';
const FAIL_FILL = 'var(--status-critical)';

const kst = (iso: string, withMinute: boolean) =>
  new Date(iso).toLocaleTimeString('ko-KR', {
    timeZone: 'Asia/Seoul',
    hour: '2-digit',
    hourCycle: 'h23',
    ...(withMinute ? { minute: '2-digit' } : {}),
  });

/** 수집 주기별 소요 시간(초). 실패한 고객사가 있던 주기는 빨강이다. */
export function ScanDurationChart({ cycles }: { cycles: readonly ScanCycle[] }) {
  const rows = cycles.map((c) => ({
    label: kst(c.started_at, true),
    seconds: c.duration_seconds,
    failed: c.tenants_failed,
    total: c.tenants_total,
  }));
  return (
    <div className="h-44 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_INK} />
          <XAxis dataKey="label" stroke={AXIS_INK} tick={{ fontSize: 11 }} interval="preserveStartEnd" minTickGap={24} />
          <YAxis
            stroke={AXIS_INK}
            tick={{ fontSize: 11 }}
            width={44}
            allowDecimals={false}
            unit="초"
          />
          <Tooltip
            isAnimationActive={false}
            cursor={BAR_CURSOR}
            contentStyle={TOOLTIP_STYLE}
            itemStyle={TOOLTIP_ITEM_STYLE}
            labelStyle={TOOLTIP_LABEL_STYLE}
            labelFormatter={(label) => `${label} 시작`}
            formatter={(value, _key, item) => {
              const row = item.payload as (typeof rows)[number];
              return [`${value}초 · 실패 ${row.failed}/${row.total}곳`, '소요'];
            }}
          />
          <Bar dataKey="seconds" isAnimationActive={false} radius={[2, 2, 0, 0]}>
            {rows.map((r) => (
              <Cell key={r.label} fill={r.failed > 0 ? FAIL_FILL : OK_FILL} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** 시간별 AI 호출 건수. 성공 위에 실패를 쌓는다. */
export function AiCallsChart({ hourly }: { hourly: readonly HourlyCount[] }) {
  // ko-KR은 시만 고르면 "09시"로 이미 '시'를 붙여 준다(종전에 한 번 더 붙여 "09시시"였다)
  const rows = hourly.map((h) => ({ label: kst(h.at, false), ok: h.requests - h.failures, failed: h.failures }));
  return (
    <div className="h-36 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
          <CartesianGrid vertical={false} stroke={GRID_INK} />
          <XAxis dataKey="label" stroke={AXIS_INK} tick={{ fontSize: 11 }} interval="preserveStartEnd" minTickGap={20} />
          <YAxis
            stroke={AXIS_INK}
            tick={{ fontSize: 11 }}
            width={44}
            allowDecimals={false}
            unit="건"
          />
          <Tooltip
            isAnimationActive={false}
            cursor={BAR_CURSOR}
            contentStyle={TOOLTIP_STYLE}
            itemStyle={TOOLTIP_ITEM_STYLE}
            labelStyle={TOOLTIP_LABEL_STYLE}
            itemSorter={(item) => (item.dataKey === 'ok' ? 0 : 1)}
            formatter={(value, key) => [`${value}건`, key === 'ok' ? '성공' : '실패']}
          />
          <Bar dataKey="ok" stackId="ai" fill={OK_FILL} isAnimationActive={false} />
          <Bar dataKey="failed" stackId="ai" fill={FAIL_FILL} isAnimationActive={false} radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
