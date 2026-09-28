// approvalAssetFacts — ACT-001 `조치 대상` 블록이 쓰는 자산 사실값 선별 (#183).
// 값이 없을 때 빈 줄을 그리지 않는가, 그리고 `0`을 없는 값으로 접지 않는가가 요점이다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

// 값(value) import는 상대 경로 + `.ts`여야 한다 — `node --test`는 `@/` 별칭을 해석하지 못한다.
import { approvalAssetFacts } from './enum-labels.ts';
import type { AssetItem } from '@/types/api';

const BASE = {
  arn: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0a1b2c3d4e5f60001',
  resource_id: 'i-0a1b2c3d4e5f60001',
  resource_role: 'PRIMARY' as const,
  name: 'vigilantis-web-01',
  account_id: '123456789012',
  region: 'ap-northeast-2',
  state: 'running',
  relationships: [],
  evaluation_status: 'COMPLETED' as const,
  health_score: null,
  verdict: null,
  skip_reason_code: null,
  collected_at: '2026-09-07T00:00:00Z',
};

const ec2 = (instance_type: string | null): AssetItem => ({
  ...BASE,
  asset_type: 'EC2',
  spec: {
    instance_type,
    availability_zone: 'ap-northeast-2a',
    vpc_id: null,
    subnet_id: null,
    private_ip: null,
  },
});

const ebs = (size_gib: number | null, volume_type: string | null): AssetItem => ({
  ...BASE,
  asset_type: 'EBS',
  spec: {
    volume_type,
    size_gib,
    availability_zone: 'ap-northeast-2a',
    encrypted: false,
    attached_instance_ids: [],
  },
});

test('EC2는 instance_type을 변경 폭 근거로 낸다', () => {
  assert.deepEqual(approvalAssetFacts(ec2('t3.large')), [
    { key: 'instance_type', label: '인스턴스 유형', value: 't3.large' },
  ]);
});

test('값이 없으면 줄을 만들지 않는다 — 빈 줄이 근거로 읽히면 안 된다', () => {
  assert.deepEqual(approvalAssetFacts(ec2(null)), []);
  assert.deepEqual(approvalAssetFacts(ebs(null, null)), []);
});

test('EBS는 삭제 규모 2종을 낸다', () => {
  assert.deepEqual(approvalAssetFacts(ebs(100, 'gp2')), [
    { key: 'size_gib', label: '크기 (GiB)', value: '100' },
    { key: 'volume_type', label: '볼륨 유형', value: 'gp2' },
  ]);
});

test('크기 0은 없는 값이 아니다 — falsy로 접으면 0 GiB가 화면에서 사라진다', () => {
  assert.deepEqual(approvalAssetFacts(ebs(0, 'gp3')), [
    { key: 'size_gib', label: '크기 (GiB)', value: '0' },
    { key: 'volume_type', label: '볼륨 유형', value: 'gp3' },
  ]);
});

test('판정에 쓸 값을 정하지 않은 유형은 빈 배열이다 — 누락이 아니라 판정이다', () => {
  const sg: AssetItem = {
    ...BASE,
    asset_type: 'SG',
    spec: { description: null, vpc_id: null, attached: true, open_to_world: [] },
  };
  assert.deepEqual(approvalAssetFacts(sg), []);
});
