'use client';

// DSH 시계열 차트 2종 — EC2별 CPU 추이(+저활성 임계선) · 인터넷 개방 SG 건수 추이.
//
// **두 축을 한 차트에 겹치지 않는다.** 단위(%와 건수)가 달라 y축이 둘이 되면 두 곡선의
// 교차가 아무 뜻도 없으면서 관계처럼 읽힌다. 카드 두 장으로 나란히 세운다.
//
// 클라이언트 컴포넌트인 이유는 Recharts가 브라우저 측정(ResponsiveContainer)에 기대서다.
// 조회·상태 판정은 서버에서 끝내고(`lib/metrics-chart.ts`) 여기로는 **그릴 것만** 넘어온다.

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import {
  cpuChartState,
  formatThroughput,
  formatTick,
  networkChartState,
  sgChartState,
  type ChartState,
  type CpuLine,
} from '@/lib/metrics-chart';
import type { CpuAxis, NetworkAxis, SgExposureAxis } from '@/types/api';

/**
 * 계열 색 — 검증된 4색 고정 순서다(dataviz 팔레트 검사 통과: 명도대·채도 하한·CVD 분리·대비).
 * 값은 `globals.css`의 `--chart-series-*`이고 라이트·다크 각각 따로 검증한 단계를 쓴다.
 * JS로 테마를 읽지 않는 이유는 서버 렌더와 하이드레이션이 서로 다른 색을 칠하지 않게 하려는 것이다.
 *
 * **순서를 돌려 쓰지 않는다.** 인스턴스가 늘거나 줄어도 남은 계열의 색이 바뀌면 안 된다 —
 * 색이 자산이 아니라 목록 순위를 따라가면 어제 본 선과 오늘 본 선이 같은 것인지 알 수 없다.
 * 5대 이상이면 색이 아니라 화면을 나눠야 한다(작은 배수) — 지금 시연 범위는 4대다.
 */
const SERIES_COLORS = [
  'var(--chart-series-1)',
  'var(--chart-series-2)',
  'var(--chart-series-3)',
  'var(--chart-series-4)',
] as const;

/** 단색 UI 토큰 위에 얹는 차트 잉크. 축·격자는 물러나고 데이터가 앞에 온다. */
const AXIS_INK = 'var(--muted-foreground)';
const GRID_INK = 'color-mix(in oklch, var(--muted-foreground) 25%, transparent)';

function seriesColor(index: number): string {
  return SERIES_COLORS[index % SERIES_COLORS.length];
}

function ChartFrame({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-56 w-full">
      <ResponsiveContainer width="100%" height="100%">
        {children as React.ReactElement}
      </ResponsiveContainer>
    </div>
  );
}

function Fallback({ state, empty }: { state: ChartState<unknown>; empty: string }) {
  if (state.kind === 'UNAVAILABLE') {
    return (
      <p className="text-muted-foreground flex h-56 items-center justify-center text-center text-sm">
        추이를 불러오지 못했습니다
        {/* 사유는 AWS 오류 코드 원문 그대로 — 인벤토리 패널의 수집 실패 표기와 같은 규칙이다. */}
        <span className="text-amber-400" title={`조회 실패: ${state.reason}`}>
          &nbsp;({state.reason})
        </span>
      </p>
    );
  }
  return (
    <p className="text-muted-foreground flex h-56 items-center justify-center text-sm">{empty}</p>
  );
}

const TOOLTIP_STYLE = {
  backgroundColor: 'var(--popover)',
  border: '1px solid var(--border)',
  borderRadius: '0.5rem',
  fontSize: '0.75rem',
  color: 'var(--popover-foreground)',
} as const;

/** 축 1 — EC2별 CPU 추이. 임계선 아래에 머무는 선이 곧 다운사이징 후보다. */
export function CpuTrendChart({
  axis,
  arns = null,
}: {
  axis: CpuAxis;
  /** 그릴 자산을 좁힌다. 자산 화면이 유형·리전 필터 결과를 넘긴다(`null`이면 전량 = 대시보드). */
  arns?: ReadonlySet<string> | null;
}) {
  const state = cpuChartState(axis, arns);
  if (state.kind !== 'READY') return <Fallback state={state} empty="CPU 관측치가 없습니다" />;

  const { rows, lines, threshold } = state.data;
  return (
    <ChartFrame>
      <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
        <CartesianGrid stroke={GRID_INK} strokeDasharray="3 3" vertical={false} />
        <XAxis
          dataKey="at"
          type="number"
          scale="time"
          // 공백이 보이도록 실제 시각 축이다 — 회차 순번으로 찍으면 스캔이 멈춘 구간이 사라진다.
          domain={['dataMin', 'dataMax']}
          tickFormatter={formatTick}
          stroke={AXIS_INK}
          tick={{ fontSize: 11 }}
          minTickGap={48}
        />
        <YAxis
          unit="%"
          domain={[0, 'auto']}
          stroke={AXIS_INK}
          tick={{ fontSize: 11 }}
          width={56}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(at) => formatTick(Number(at))}
          formatter={(value, key) => [
            `${value}%`,
            lines.find((l: CpuLine) => l.key === String(key))?.label ?? String(key),
          ]}
        />
        <Legend
          formatter={(key) => (
            <span className="text-muted-foreground text-xs">
              {lines.find((l: CpuLine) => l.key === String(key))?.label ?? String(key)}
            </span>
          )}
        />
        {/* 임계선은 계열이 아니라 주석이다 — 계열 색을 쓰지 않고 파선으로 물러선다.
            값은 서버가 실어 준 판정 임계치(IDLE_CPU_AVG)다.
            **선 위에 글자를 얹지 않는다** — 임계치(5%)가 축 바닥에 가까워 라벨이 x축 눈금과
            겹친다. 값은 카드 설명이 말하고 여기서는 선만 긋는다. */}
        <ReferenceLine y={threshold} stroke={AXIS_INK} strokeDasharray="4 4" />
        {lines.map((line, index) => (
          <Line
            key={line.key}
            type="monotone"
            dataKey={line.key}
            name={line.key}
            stroke={seriesColor(index)}
            strokeWidth={2}
            dot={false}
            // 관측이 빠진 구간에서 선을 잇지 않는다 — 없는 값을 0으로 읽게 두지 않는다.
            connectNulls={false}
            activeDot={{ r: 4 }}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ChartFrame>
  );
}

/**
 * 축 3 — 네트워크 처리량 추이(수신·송신 **합계** 2줄). 자산 관제(AST-001)가 쓴다.
 *
 * 인스턴스마다 두 줄씩 그리면 4대에 8줄이라 아무것도 안 읽혀서 합산한다. 한 대의 곡선은
 * 상세 패널(AST-002)이 스파크라인으로 따로 그린다.
 *
 * **CPU 카드와 나란히 두되 y축을 공유하지 않는다** — 단위가 %와 B/s로 달라서다. CPU가 낮은데
 * 이 선이 살아 있으면 저활성 판정의 반례다(프록시·NAT처럼 CPU를 거의 안 쓰는 워크로드).
 */
export function NetworkTrendChart({
  axis,
  arns = null,
}: {
  axis: NetworkAxis;
  /** 합산 대상. 자산 화면의 필터 결과를 그대로 넘긴다(`null`이면 전량). */
  arns?: ReadonlySet<string> | null;
}) {
  const state = networkChartState(axis, arns);
  if (state.kind !== 'READY') return <Fallback state={state} empty="네트워크 관측치가 없습니다" />;

  return (
    <ChartFrame>
      <LineChart data={state.data} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
        <CartesianGrid stroke={GRID_INK} strokeDasharray="3 3" vertical={false} />
        <XAxis
          dataKey="at"
          type="number"
          scale="time"
          domain={['dataMin', 'dataMax']}
          tickFormatter={formatTick}
          stroke={AXIS_INK}
          tick={{ fontSize: 11 }}
          minTickGap={48}
        />
        {/* 바이트는 자릿수가 커서 원값 눈금이 축을 밀어낸다 — 눈금에서 단위를 접는다. */}
        <YAxis
          domain={[0, 'auto']}
          tickFormatter={(v: number) => formatThroughput(v)}
          stroke={AXIS_INK}
          tick={{ fontSize: 11 }}
          width={72}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(at) => formatTick(Number(at))}
          formatter={(value, key) => [
            formatThroughput(Number(value)),
            key === 'in' ? '수신' : '송신',
          ]}
        />
        <Legend
          formatter={(key) => (
            <span className="text-muted-foreground text-xs">{key === 'in' ? '수신' : '송신'}</span>
          )}
        />
        {/* 송신은 파선이다 — 두 값이 같은 구간(시드 데이터·대칭 트래픽)에서 선이 정확히 겹치면
            색만으로는 한 줄로 보여 "송신이 없다"로 읽힌다. */}
        {(['in', 'out'] as const).map((key, index) => (
          <Line
            key={key}
            type="monotone"
            dataKey={key}
            stroke={seriesColor(index)}
            strokeWidth={2}
            strokeDasharray={key === 'out' ? '5 3' : undefined}
            dot={false}
            connectNulls={false}
            activeDot={{ r: 4 }}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ChartFrame>
  );
}

/** 축 2 — 인터넷 개방 SG 건수 추이. 조치가 먹히면 이 선이 내려간다. */
export function SgExposureTrendChart({ axis }: { axis: SgExposureAxis }) {
  const state = sgChartState(axis);
  if (state.kind !== 'READY') return <Fallback state={state} empty="수집 회차가 아직 없습니다" />;

  return (
    <ChartFrame>
      <LineChart data={state.data} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
        <CartesianGrid stroke={GRID_INK} strokeDasharray="3 3" vertical={false} />
        <XAxis
          dataKey="at"
          type="number"
          scale="time"
          domain={['dataMin', 'dataMax']}
          tickFormatter={formatTick}
          stroke={AXIS_INK}
          tick={{ fontSize: 11 }}
          minTickGap={48}
        />
        {/* 건수는 정수다 — 소수 눈금이 서면 "1.5건"이 읽힌다. */}
        <YAxis
          allowDecimals={false}
          domain={[0, (max: number) => Math.max(1, max)]}
          stroke={AXIS_INK}
          tick={{ fontSize: 11 }}
          width={56}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelFormatter={(at) => formatTick(Number(at))}
          formatter={(value) => [`${value}건`, '인터넷 개방 SG']}
        />
        {/* 계열이 하나라 범례를 두지 않는다 — 카드 제목이 이미 그 이름이다. */}
        <Line
          type="stepAfter"
          dataKey="open"
          stroke={seriesColor(0)}
          strokeWidth={2}
          dot={false}
          activeDot={{ r: 4 }}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartFrame>
  );
}
