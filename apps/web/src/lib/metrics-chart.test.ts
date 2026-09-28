// DSH 시계열 파생 회귀 — `npm test`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  cpuChartState,
  cpuPointsFor,
  formatThroughput,
  networkChartState,
  networkRowsFor,
  toAggregateNetworkRows,
  toNetworkRows,
  formatTick,
  sgChartState,
  toCpuLines,
  toCpuRows,
  toSgRows,
} from './metrics-chart.ts';
import type { CpuAxis, CpuSeries, NetworkAxis, NetworkSeries, SgExposureAxis } from '@/types/api';

function series(over: Partial<CpuSeries> = {}): CpuSeries {
  return {
    arn: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa',
    resource_id: 'i-0aaa',
    name: 'seed-idle',
    points: [
      { at: '2026-09-18T00:00:00Z', value: 2 },
      { at: '2026-09-18T01:00:00Z', value: 3 },
    ],
    ...over,
  };
}

function cpuAxis(over: Partial<CpuAxis> = {}): CpuAxis {
  return {
    status: 'READY',
    period_seconds: 3600,
    window_start: '2026-09-18T00:00:00Z',
    window_end: '2026-09-18T01:00:00Z',
    idle_cpu_avg_threshold: 5,
    series: [series()],
    reason_code: null,
    ...over,
  };
}

test('여러 인스턴스가 시각 하나당 한 행으로 접힌다', () => {
  const rows = toCpuRows([
    series(),
    series({ resource_id: 'i-0bbb', name: 'seed-normal', points: [{ at: '2026-09-18T01:00:00Z', value: 35 }] }),
  ]);

  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0], { at: Date.parse('2026-09-18T00:00:00Z'), 'i-0aaa': 2 });
  assert.deepEqual(rows[1], { at: Date.parse('2026-09-18T01:00:00Z'), 'i-0aaa': 3, 'i-0bbb': 35 });
});

test('관측이 없는 구간은 0으로 채우지 않는다', () => {
  // 0을 채우면 "CPU가 0%"가 되어 실제 저활성과 구분되지 않는다 — 선이 끊겨야 한다.
  const rows = toCpuRows([
    series({ points: [{ at: '2026-09-18T00:00:00Z', value: 2 }] }),
    series({ resource_id: 'i-0bbb', points: [{ at: '2026-09-18T01:00:00Z', value: 9 }] }),
  ]);

  assert.equal('i-0bbb' in rows[0], false);
  assert.equal('i-0aaa' in rows[1], false);
});

test('행은 시각 오름차순이다', () => {
  const rows = toCpuRows([
    series({
      points: [
        { at: '2026-09-18T02:00:00Z', value: 1 },
        { at: '2026-09-18T00:00:00Z', value: 2 },
      ],
    }),
  ]);
  assert.deepEqual(
    rows.map((r) => r.at),
    [Date.parse('2026-09-18T00:00:00Z'), Date.parse('2026-09-18T02:00:00Z')],
  );
});

test('Name 태그가 없으면 인스턴스 ID로 범례를 만든다', () => {
  const [line] = toCpuLines([series({ name: null })]);
  assert.equal(line.label, 'i-0aaa');
});

test('관측치가 0개인 인스턴스도 범례에 남는다', () => {
  // 범례에서 빠지면 "CPU가 0"인지 "메트릭이 없는 인스턴스"인지 화면에서 구분되지 않는다.
  const lines = toCpuLines([series({ points: [] })]);
  assert.equal(lines.length, 1);
});

test('조회 실패와 관측 0건을 가른다', () => {
  assert.deepEqual(cpuChartState(cpuAxis({ status: 'UNAVAILABLE', reason_code: 'AccessDenied', series: [], period_seconds: null, window_start: null, window_end: null, idle_cpu_avg_threshold: null })), {
    kind: 'UNAVAILABLE',
    reason: 'AccessDenied',
  });
  assert.deepEqual(cpuChartState(cpuAxis({ series: [series({ points: [] })] })), { kind: 'EMPTY' });

  const ready = cpuChartState(cpuAxis());
  assert.equal(ready.kind, 'READY');
  assert.equal(ready.kind === 'READY' && ready.data.threshold, 5);
});

test('SG 축도 같은 규칙으로 갈린다', () => {
  const axis: SgExposureAxis = { status: 'READY', points: [], reason_code: null };
  assert.deepEqual(sgChartState(axis), { kind: 'EMPTY' });
  assert.deepEqual(
    sgChartState({ status: 'UNAVAILABLE', points: [], reason_code: 'OperationalError' }),
    { kind: 'UNAVAILABLE', reason: 'OperationalError' },
  );

  const rows = toSgRows([
    { at: '2026-09-18T01:00:00Z', value: 2 },
    { at: '2026-09-18T00:00:00Z', value: 1 },
  ]);
  assert.deepEqual(
    rows.map((r) => r.open),
    [1, 2],
  );
});

test('눈금은 KST 월/일 시:분이다', () => {
  // 2026-09-18T00:00Z = KST 09:00 — 서버가 주는 UTC를 그대로 찍으면 관제자가 9시간 어긋난 걸 본다.
  const label = formatTick(Date.parse('2026-09-18T00:00:00Z'));
  assert.match(label, /9.*18.*09:00/);
});

// ── 네트워크 축(축 3)

function netSeries(over: Partial<NetworkSeries> = {}): NetworkSeries {
  return {
    arn: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0aaa',
    resource_id: 'i-0aaa',
    name: 'seed-idle',
    in_points: [
      { at: '2026-09-18T00:00:00Z', value: 3600 },
      { at: '2026-09-18T01:00:00Z', value: 7200 },
    ],
    out_points: [{ at: '2026-09-18T00:00:00Z', value: 1800 }],
    ...over,
  };
}

function netAxis(over: Partial<NetworkAxis> = {}): NetworkAxis {
  return {
    status: 'READY',
    period_seconds: 3600,
    window_start: '2026-09-18T00:00:00Z',
    window_end: '2026-09-18T01:00:00Z',
    series: [netSeries()],
    reason_code: null,
    ...over,
  };
}

test('네트워크 값은 period로 나눠 초당으로 환산한다', () => {
  // 계약은 period(1시간)당 바이트를 싣는다 — 3600B/시간 = 1B/s. 나누지 않으면 축이 3600배 뜬다.
  const rows = toNetworkRows(netSeries(), 3600);
  assert.deepEqual(
    rows.map((r) => [r.in, r.out]),
    [
      [1, 0.5],
      [2, undefined],
    ],
  );
});

test('관측이 없는 방향은 0으로 채우지 않는다', () => {
  // 0으로 채우면 "트래픽이 없었다"가 되어 결측과 구분되지 않는다.
  const rows = toNetworkRows(netSeries(), 3600);
  assert.equal('out' in rows[1], false);
});

test('합산은 그 시각에 관측이 있은 자산만 더한다', () => {
  // 늦게 뜬 인스턴스의 빈 앞 구간을 0으로 세면 합계가 그만큼 꺼져 보인다.
  const late = netSeries({
    arn: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0bbb',
    resource_id: 'i-0bbb',
    in_points: [{ at: '2026-09-18T01:00:00Z', value: 3600 }],
    out_points: [],
  });
  const rows = toAggregateNetworkRows([netSeries(), late], 3600);
  assert.deepEqual(
    rows.map((r) => [r.at, r.in]),
    [
      [Date.parse('2026-09-18T00:00:00Z'), 1],
      [Date.parse('2026-09-18T01:00:00Z'), 3],
    ],
  );
});

test('네트워크 축도 조회 실패와 관측 0건을 가른다', () => {
  assert.deepEqual(
    networkChartState({ ...netAxis(), status: 'UNAVAILABLE', period_seconds: null, window_start: null, window_end: null, series: [], reason_code: 'Throttling' }),
    { kind: 'UNAVAILABLE', reason: 'Throttling' },
  );
  assert.equal(networkChartState(netAxis({ series: [] })).kind, 'EMPTY');
});

test('ARN 집합을 주면 그 자산만 그린다 — CPU·네트워크 같은 규칙', () => {
  // 자산 화면의 리전·낭비 후보 필터가 이 집합을 준다. 필터와 곡선이 따로 놀면 안 된다.
  const other = series({ arn: 'arn:other', resource_id: 'i-0bbb' });
  const cpu = cpuChartState(cpuAxis({ series: [series(), other] }), new Set(['arn:other']));
  assert.equal(cpu.kind === 'READY' && cpu.data.lines.length, 1);

  const net = networkChartState(
    netAxis({ series: [netSeries(), netSeries({ arn: 'arn:other', resource_id: 'i-0bbb' })] }),
    new Set(['arn:other']),
  );
  assert.equal(net.kind === 'READY' && net.data[0].in, 1);
});

test('자산 1건 조회 — 축 실패(null)와 관측 없음을 가른다', () => {
  const arn = netSeries().arn;
  assert.equal(cpuPointsFor(cpuAxis({ status: 'UNAVAILABLE', period_seconds: null, window_start: null, window_end: null, idle_cpu_avg_threshold: null, series: [], reason_code: 'AccessDenied' }), arn), null);
  assert.equal(cpuPointsFor(cpuAxis(), 'arn:없는자산'), null);
  assert.equal(networkRowsFor(netAxis(), arn)?.length, 2);
});

test('처리량 표기는 1000 배수를 쓴다 — AWS가 네트워크를 10진으로 적는다', () => {
  assert.equal(formatThroughput(999), '999 B/s');
  assert.equal(formatThroughput(1500), '1.5 KB/s');
  assert.equal(formatThroughput(2_500_000), '2.5 MB/s');
});
