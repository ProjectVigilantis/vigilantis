// AST-001 토폴로지 파생 회귀 — `npm test`. 렌더 없이 검증되는 부분을 전부 여기서 잡습니다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { buildTopology } from './asset-graph.ts';
import type { AssetItem, AssetType, RelationType } from '../types/api.ts';

const asset = (
  arn: string,
  asset_type: AssetType,
  rels: [RelationType, string][] = [],
): AssetItem =>
  ({
    arn,
    asset_type,
    relationships: rels.map(([relation_type, target_arn]) => ({ relation_type, target_arn })),
  }) as AssetItem;

/** mock 시드와 같은 모양 — EC2 2대, 6종 엣지, 미연결 EBS 1건. */
const inventory = (): AssetItem[] => [
  asset('ec2-a', 'EC2', [
    ['SECURED_BY', 'sg-open'],
    ['ATTACHED_TO', 'ebs-attached'],
    ['MEMBER_OF', 'asg'],
    ['REGISTERED_IN', 'tg'],
    ['PROTECTED_BY', 'nacl'],
  ]),
  asset('ec2-b', 'EC2', [
    ['SECURED_BY', 'sg-unused'],
    ['PROTECTED_BY', 'nacl'],
  ]),
  asset('sg-open', 'SG'),
  asset('sg-unused', 'SG'),
  asset('ebs-attached', 'EBS'),
  asset('ebs-unattached', 'EBS'),
  asset('nacl', 'NACL'),
  asset('asg', 'AUTO_SCALING_GROUP', [['USES', 'lt']]),
  asset('lt', 'LAUNCH_TEMPLATE'),
  asset('tg', 'ALB_TARGET_GROUP'),
];

test('EC2 행이 열(TG·EBS)과 트레일링 칩으로 갈린다', () => {
  const { rows } = buildTopology(inventory());
  assert.equal(rows.length, 2, 'EC2 대수만큼 행이 선다');
  assert.deepEqual(
    rows[0].targetGroups.map((e) => e.targetArn),
    ['tg'],
  );
  assert.deepEqual(
    rows[0].volumes.map((e) => e.targetArn),
    ['ebs-attached'],
  );
  assert.deepEqual(
    rows[0].chips.map((e) => e.relation),
    ['SECURED_BY', 'MEMBER_OF', 'PROTECTED_BY'],
  );
});

test('ASG → 시작 템플릿은 별도 줄로 나온다', () => {
  const { asgRows } = buildTopology(inventory());
  assert.equal(asgRows.length, 1);
  assert.deepEqual(
    asgRows[0].templates.map((e) => e.targetArn),
    ['lt'],
  );
});

test('트래픽 경로 밖은 EC2에서 못 닿는 자원뿐이다', () => {
  // lt는 asg를 한 다리 건너 닿으므로 경로 안이다 — 직접 관계만 보면 미연결 EBS와
  // 같은 자리로 내려가 "연결됐는데 안 쓰는 것"과 "아예 연결이 없는 것"이 섞인다.
  const { orphans } = buildTopology(inventory());
  assert.deepEqual(
    orphans.map((a) => a.arn),
    ['ebs-unattached'],
  );
});

test('relationships가 전부 비어도 EC2 행은 선다 — 빈 화면으로 두지 않는다', () => {
  const flat = [asset('ec2-a', 'EC2'), asset('sg-open', 'SG')];
  const { rows, orphans } = buildTopology(flat);
  assert.equal(rows.length, 1);
  assert.deepEqual(rows[0].chips, []);
  assert.deepEqual(
    orphans.map((a) => a.arn),
    ['sg-open'],
  );
});

test('target_arn이 응답에 없으면 ARN만 남기고 관계를 버리지 않는다', () => {
  // collection_status가 PARTIAL·FAILED면 실제로 일어난다(§4.2 예외).
  const { rows } = buildTopology([asset('ec2-a', 'EC2', [['SECURED_BY', 'sg-missing']])]);
  assert.equal(rows[0].chips.length, 1);
  assert.equal(rows[0].chips[0].targetArn, 'sg-missing');
  assert.equal(rows[0].chips[0].asset, null);
});
