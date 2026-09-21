'use client';

// 자산 1건의 작은 곡선 — AST-002 상세 패널이 쓴다. 축·격자·범례가 없는 **모양만 보는 차트**다.
//
// 큰 차트와 나누는 기준: 추이 카드(trend-charts.tsx)는 "언제 무슨 일이 있었나"를 읽는 자리라
// 시각 눈금과 값이 필요하고, 여기는 상세 패널의 한 줄 옆에서 "이 자산이 계속 한가한가"만
// 답하면 된다. 그래서 축을 지우고 높이를 40–56px로 눌렀다 — 패널 한 칸을 넘기면 옆의
// 판정·헬스 값과 같은 화면에서 못 본다.
//
// Recharts를 그대로 쓰는 이유는 큰 차트와 **같은 데이터 파생**(lib/metrics-chart)을 먹기
// 때문이다. SVG를 손으로 그리면 결측 구간을 잇는 규칙이 두 벌이 된다.

import { Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, YAxis } from 'recharts';

import { formatThroughput, formatTick, type NetworkRow } from '@/lib/metrics-chart';
import type { TimeseriesPoint } from '@/types/api';

const TOOLTIP_STYLE = {
  backgroundColor: 'var(--popover)',
  border: '1px solid var(--border)',
  borderRadius: '0.5rem',
  fontSize: '0.75rem',
  color: 'var(--popover-foreground)',
} as const;

const INK = 'var(--muted-foreground)';

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-14 w-full">
      <ResponsiveContainer width="100%" height="100%">
        {children as React.ReactElement}
      </ResponsiveContainer>
    </div>
  );
}

/** 값이 없을 때의 한 줄. 상세 패널의 다른 "확인 불가"와 같은 무게로 물러선다. */
function Muted({ children }: { children: React.ReactNode }) {
  return <p className="text-muted-foreground py-3 text-center text-xs">{children}</p>;
}

/**
 * CPU 스파크라인 + 저활성 임계선. `points`가 null이면 **조회 실패**, 빈 배열이면 관측이 없는
 * 것이다 — 두 경우의 문구를 다르게 준다(0%로 그리지 않는다).
 */
export function CpuSparkline({
  points,
  threshold,
}: {
  points: TimeseriesPoint[] | null;
  threshold: number | null;
}) {
  if (points === null) return <Muted>CPU 추이를 불러오지 못했습니다</Muted>;
  if (points.length === 0) return <Muted>CPU 관측치가 없습니다</Muted>;

  const rows = points.map((p) => ({ at: Date.parse(p.at), cpu: p.value }));
  return (
    <Frame>
      <LineChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
        {/* 도메인을 0부터 잡는다 — 자동 범위로 두면 1%대 변동이 화면을 꽉 채워 급등으로 읽힌다. */}
        <YAxis hide domain={[0, (max: number) => Math.max(max, (threshold ?? 0) * 1.5, 10)]} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(at) => formatTick(Number(at))}
          formatter={(value) => [`${value}%`, 'CPU']}
        />
        {threshold !== null ? (
          <ReferenceLine y={threshold} stroke={INK} strokeDasharray="3 3" />
        ) : null}
        <Line
          type="monotone"
          dataKey="cpu"
          stroke="var(--chart-series-1)"
          strokeWidth={1.5}
          dot={false}
          connectNulls={false}
          isAnimationActive={false}
        />
      </LineChart>
    </Frame>
  );
}

/** 네트워크 스파크라인(수신·송신 2줄). 값은 초당 처리량으로 이미 환산돼 온다. */
export function NetworkSparkline({ rows }: { rows: NetworkRow[] | null }) {
  if (rows === null) return <Muted>네트워크 추이를 불러오지 못했습니다</Muted>;
  if (rows.length === 0) return <Muted>네트워크 관측치가 없습니다</Muted>;

  return (
    <Frame>
      <LineChart data={rows} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
        <YAxis hide domain={[0, 'auto']} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(at) => formatTick(Number(at))}
          formatter={(value, key) => [formatThroughput(Number(value)), key === 'in' ? '수신' : '송신']}
        />
        {/* 송신은 파선 — 두 값이 겹치는 구간에서 한 줄로 보이지 않게(추이 카드와 같은 규칙). */}
        {(['in', 'out'] as const).map((key, index) => (
          <Line
            key={key}
            type="monotone"
            dataKey={key}
            stroke={index === 0 ? 'var(--chart-series-2)' : 'var(--chart-series-3)'}
            strokeDasharray={key === 'out' ? '4 2' : undefined}
            strokeWidth={1.5}
            dot={false}
            connectNulls={false}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </Frame>
  );
}
