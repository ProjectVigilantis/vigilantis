// DSH 시계열 차트 파생 — `GET /api/v1/metrics/timeseries` 응답을 Recharts가 먹는 행 배열로 접는다.
// 렌더와 분리해 `node --test`로 검증한다. 같은 디렉터리 상대 경로만 쓴다 — `node --test`는 `@/`
// 별칭을 해석하지 못한다(타입 전용 import는 스트리핑돼 사라진다).

import type {
  CpuAxis,
  CpuSeries,
  NetworkAxis,
  NetworkSeries,
  SgExposureAxis,
  TimeseriesPoint,
} from '@/types/api';

/** 한 시각의 모든 계열 값. `key`는 `seriesKey()`가 만든 인스턴스별 열 이름이다. */
export interface CpuRow {
  /** epoch ms — Recharts의 수치 x축. 문자열로 두면 간격이 균등해져 스캔 공백이 안 보인다. */
  at: number;
  [key: string]: number;
}

export interface CpuLine {
  key: string;
  label: string;
  arn: string;
}

/** 열 이름은 ARN이 아니라 `resource_id`다 — 범례·툴팁에서 사람이 읽는 것과 같은 축을 쓴다. */
export function seriesKey(series: CpuSeries): string {
  return series.resource_id;
}

/** 범례에 쓸 이름. Name 태그가 없으면 인스턴스 ID로 대신한다(계약상 null 가능). */
export function seriesLabel(series: CpuSeries): string {
  return series.name ?? series.resource_id;
}

/**
 * EC2별 곡선(계열마다 제 점 목록)을 **시각 하나당 한 행**으로 뒤집는다 — Recharts의 입력 모양이다.
 *
 * 인스턴스마다 관측 시각이 어긋날 수 있으므로(새로 뜬 인스턴스, 일부 구간 결측) 시각의
 * 합집합으로 행을 만들고 **없는 값은 키를 넣지 않는다.** `0`으로 채우면 "CPU가 0%"가 되어
 * 실제 저활성과 구분되지 않는다 — Recharts는 키가 없는 점에서 선을 끊는다.
 */
export function toCpuRows(series: readonly CpuSeries[]): CpuRow[] {
  const byTime = new Map<number, CpuRow>();
  for (const one of series) {
    const key = seriesKey(one);
    for (const point of one.points) {
      const at = Date.parse(point.at);
      if (Number.isNaN(at)) continue;
      const row = byTime.get(at) ?? { at };
      row[key] = point.value;
      byTime.set(at, row);
    }
  }
  return [...byTime.values()].sort((a, b) => a.at - b.at);
}

/** 범례·선 정의. **관측치가 0개인 인스턴스도 남긴다** — 범례에서 빠지면 "메트릭이 없다"가 안 보인다. */
export function toCpuLines(series: readonly CpuSeries[]): CpuLine[] {
  return series.map((one) => ({ key: seriesKey(one), label: seriesLabel(one), arn: one.arn }));
}

export interface SgRow {
  at: number;
  open: number;
}

export function toSgRows(points: readonly TimeseriesPoint[]): SgRow[] {
  return points
    .map((p) => ({ at: Date.parse(p.at), open: p.value }))
    .filter((r) => !Number.isNaN(r.at))
    .sort((a, b) => a.at - b.at);
}

/**
 * 차트 한 장이 그릴 것. 실패·빈 상태를 **컴포넌트가 아니라 여기서** 가른다 — 같은 판단을
 * 두 차트가 각자 하면 한쪽만 고쳐져 갈린다.
 *
 * `UNAVAILABLE`과 `READY`인데 점이 0개인 것은 다르다. 앞은 조회가 실패한 것이고 뒤는
 * 관측이 없는 것이라, 화면이 할 말과 관제자가 할 행동이 다르다.
 */
export type ChartState<T> =
  | { kind: 'UNAVAILABLE'; reason: string }
  | { kind: 'EMPTY' }
  | { kind: 'READY'; data: T };

export interface CpuChart {
  rows: CpuRow[];
  lines: CpuLine[];
  threshold: number;
}

export function cpuChartState(
  axis: CpuAxis,
  /**
   * 그릴 자산. `null`이면 축의 전량(대시보드)이고, 집합을 주면 그 자산만 그린다 — 자산 화면이
   * 유형·리전 필터에 맞춰 곡선을 좁히는 데 쓴다. 필터와 곡선이 따로 놀면 화면 위쪽에서 EC2를
   * 한 대로 좁혀 놓고 아래 차트는 네 대를 그리게 된다.
   */
  arns: ReadonlySet<string> | null = null,
): ChartState<CpuChart> {
  if (axis.status === 'UNAVAILABLE') {
    return { kind: 'UNAVAILABLE', reason: axis.reason_code ?? '알 수 없는 오류' };
  }
  const series = arns === null ? axis.series : axis.series.filter((s) => arns.has(s.arn));
  const rows = toCpuRows(series);
  if (rows.length === 0) return { kind: 'EMPTY' };
  return {
    kind: 'READY',
    // 임계선 값은 서버가 싣는다. READY면 계약이 non-null을 보장하지만, 타입상 null이 열려
    // 있으므로 좁혀 둔다 — 여기서 상수를 다시 적으면 판정 기준과 선이 갈린다.
    data: { rows, lines: toCpuLines(series), threshold: axis.idle_cpu_avg_threshold ?? 0 },
  };
}

export function sgChartState(axis: SgExposureAxis): ChartState<SgRow[]> {
  if (axis.status === 'UNAVAILABLE') {
    return { kind: 'UNAVAILABLE', reason: axis.reason_code ?? '알 수 없는 오류' };
  }
  const rows = toSgRows(axis.points);
  if (rows.length === 0) return { kind: 'EMPTY' };
  return { kind: 'READY', data: rows };
}

// ── 네트워크 축(축 3)

/** 한 시각의 In·Out 한 쌍. 단위는 **초당 바이트**다 — 행을 만들 때 `period_seconds`로 나눈다. */
export interface NetworkRow {
  at: number;
  in?: number;
  out?: number;
}

/**
 * 자산 1건의 네트워크 곡선을 시각 순 행으로 접는다. **여기서 초당으로 환산한다** —
 * 계약은 period당 바이트를 싣고(서버는 관측값을 가공하지 않는다), 사람이 읽는 단위로
 * 바꾸는 일은 화면 몫이다. period가 없으면(계약상 UNAVAILABLE) 나누지 않고 원값을 쓴다.
 *
 * 두 방향의 관측 시각이 어긋날 수 있어 합집합으로 행을 만들고 **없는 쪽 키는 넣지 않는다** —
 * 0으로 채우면 "트래픽이 없었다"가 되어 결측과 구분되지 않는다.
 */
export function toNetworkRows(series: NetworkSeries, periodSeconds: number | null): NetworkRow[] {
  const divisor = periodSeconds && periodSeconds > 0 ? periodSeconds : 1;
  const byTime = new Map<number, NetworkRow>();
  const put = (points: readonly TimeseriesPoint[], key: 'in' | 'out') => {
    for (const point of points) {
      const at = Date.parse(point.at);
      if (Number.isNaN(at)) continue;
      const row = byTime.get(at) ?? { at };
      row[key] = point.value / divisor;
      byTime.set(at, row);
    }
  };
  put(series.in_points, 'in');
  put(series.out_points, 'out');
  return [...byTime.values()].sort((a, b) => a.at - b.at);
}

/**
 * 여러 자산의 곡선을 **시각별로 합산**한다. 자산 화면의 추이 카드가 쓴다 — 인스턴스마다 선을
 * 두 줄씩(In·Out) 그리면 4대만 돼도 8줄이라 아무것도 안 읽힌다. 합계 2줄로 "이 계정이 지금
 * 얼마나 주고받는가"를 보이고, 자산 1건의 곡선은 상세 패널이 따로 그린다.
 *
 * **없는 값은 0으로 치지 않는다.** 그 시각에 관측이 있은 자산만 더하므로, 인스턴스가 뒤늦게
 * 생겨 앞 구간이 비어도 합계가 그만큼 꺼져 보이지 않는다.
 */
export function toAggregateNetworkRows(
  series: readonly NetworkSeries[],
  periodSeconds: number | null,
): NetworkRow[] {
  const byTime = new Map<number, NetworkRow>();
  for (const one of series) {
    for (const row of toNetworkRows(one, periodSeconds)) {
      const sum = byTime.get(row.at) ?? { at: row.at };
      if (row.in !== undefined) sum.in = (sum.in ?? 0) + row.in;
      if (row.out !== undefined) sum.out = (sum.out ?? 0) + row.out;
      byTime.set(row.at, sum);
    }
  }
  return [...byTime.values()].sort((a, b) => a.at - b.at);
}

export function networkChartState(
  axis: NetworkAxis,
  /** 합산 대상 ARN. `null`이면 축의 모든 자산 — 자산 화면의 유형·리전 필터가 이 집합을 준다. */
  arns: ReadonlySet<string> | null = null,
): ChartState<NetworkRow[]> {
  if (axis.status === 'UNAVAILABLE') {
    return { kind: 'UNAVAILABLE', reason: axis.reason_code ?? '알 수 없는 오류' };
  }
  const picked = arns === null ? axis.series : axis.series.filter((s) => arns.has(s.arn));
  const rows = toAggregateNetworkRows(picked, axis.period_seconds);
  if (rows.length === 0) return { kind: 'EMPTY' };
  return { kind: 'READY', data: rows };
}

/**
 * 초당 바이트를 사람이 읽는 단위로. 1024가 아니라 1000 배수를 쓴다 — CloudWatch·AWS 청구서가
 * 네트워크를 10진 단위로 적는다.
 */
export function formatThroughput(bytesPerSecond: number): string {
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
  let value = bytesPerSecond;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/**
 * 자산 1건의 곡선만 뽑는다 — 상세 패널(AST-002)이 쓴다. 축이 UNAVAILABLE이거나 그 자산의
 * 줄이 없으면 null이다. **null과 빈 배열을 가른다**: 앞은 "조회를 못 했다", 뒤는 "관측이 없다"다.
 */
export function cpuPointsFor(axis: CpuAxis, arn: string): TimeseriesPoint[] | null {
  if (axis.status === 'UNAVAILABLE') return null;
  return axis.series.find((s) => s.arn === arn)?.points ?? null;
}

export function networkRowsFor(axis: NetworkAxis, arn: string): NetworkRow[] | null {
  if (axis.status === 'UNAVAILABLE') return null;
  const series = axis.series.find((s) => s.arn === arn);
  return series ? toNetworkRows(series, axis.period_seconds) : null;
}

/**
 * x축 눈금 — `9/17 07:19` 꼴의 KST 표기. 차트는 축과 툴팁에서 같은 문자열을 써야
 * 관제자가 두 곳을 대조할 수 있다.
 */
export function formatTick(at: number): string {
  return new Date(at).toLocaleString('ko-KR', {
    timeZone: 'Asia/Seoul',
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
