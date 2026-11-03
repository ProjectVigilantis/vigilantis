import assert from 'node:assert/strict';
import { test } from 'node:test';

import { TENANT_AGGREGATE_UNSUPPORTED, scopeAssets, scopeIncidents, scopeMetrics } from './monitoring-scope.ts';
import type { AssetsResponse, IncidentListItem, MetricsTimeseriesResponse } from '../types/api.ts';

const A = '111122223333';
const B = '444455556666';
const arn = (account: string, id: string) => `arn:aws:ec2:ap-northeast-2:${account}:instance/${id}`;

test('자산은 account_id가 고객사 계정인 것만 남긴다', () => {
  const data = {
    collection_status: 'READY',
    last_collected_at: null,
    uncollected: [],
    items: [{ account_id: A, arn: arn(A, 'i-1') }, { account_id: B, arn: arn(B, 'i-2') }],
  } as unknown as AssetsResponse;
  assert.deepEqual(
    scopeAssets(data, A).items.map((a) => a.arn),
    [arn(A, 'i-1')],
  );
});

test('인시던트는 대상 ARN의 계정 칸으로 고른다', () => {
  const incidents = [{ subject_arn: arn(A, 'i-1') }, { subject_arn: arn(B, 'i-2') }, { subject_arn: 'arn:aws:s3:::bucket' }] as IncidentListItem[];
  assert.deepEqual(
    scopeIncidents(incidents, A).map((i) => i.subject_arn),
    [arn(A, 'i-1')],
  );
});

test('CPU · 네트워크는 고객사 계열만 남기고, 나눌 수 없는 집계 축은 미지원으로 바꾼다', () => {
  const ready = { status: 'READY', points: [{ at: '2026-11-02T00:00:00Z', value: 3 }], reason_code: null };
  const metrics = {
    generated_at: '2026-11-02T00:00:00Z',
    cpu: { status: 'READY', series: [{ arn: arn(A, 'i-1') }, { arn: arn(B, 'i-2') }] },
    network: { status: 'READY', series: [{ arn: arn(B, 'i-2') }] },
    sg_exposure: ready,
    asset_status: ready,
    asset_inventory: ready,
    threat_events: ready,
  } as unknown as MetricsTimeseriesResponse;
  const scoped = scopeMetrics(metrics, A);
  assert.deepEqual(scoped.cpu.series.map((s) => s.arn), [arn(A, 'i-1')]);
  assert.equal(scoped.network.series.length, 0);
  for (const axis of [scoped.sg_exposure, scoped.asset_status, scoped.asset_inventory, scoped.threat_events]) {
    assert.equal(axis.status, 'UNAVAILABLE');
    assert.equal(axis.reason_code, TENANT_AGGREGATE_UNSUPPORTED);
    assert.equal(axis.points.length, 0);
  }
});
