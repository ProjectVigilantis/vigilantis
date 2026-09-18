// AST-001 토폴로지 파생 회귀 — `npm test`. 렌더 없이 검증되는 부분을 전부 여기서 잡습니다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  buildTopology,
  groupOrphansByType,
  pickRows,
  rowVerdicts,
  sortRowsByRisk,
} from './asset-graph.ts';
import type { AssetItem, AssetType, RelationType, Verdict } from '../types/api.ts';

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

/** 골든 시드와 같은 모양 — EC2 2대, 6종 엣지, 미연결 EBS 1건. */
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

// ── 대시보드 행 상한을 위한 위험 순서 (rowRisk · sortRowsByRisk) ──────────────────

/** 판정을 실은 자산. `asset()`은 verdict를 안 넣으므로 여기서 덧댄다. */
const judged = (base: AssetItem, verdict: Verdict | null): AssetItem =>
  ({ ...base, verdict }) as AssetItem;

function rowsOf(items: AssetItem[]) {
  return buildTopology(items).rows;
}

test('EC2 자신이 THREAT면 가장 앞이다', () => {
  const [threat, cost] = sortRowsByRisk(
    rowsOf([
      judged(asset('ec2-quiet', 'EC2'), null),
      judged(asset('ec2-cost', 'EC2'), 'COST_CANDIDATE'),
      judged(asset('ec2-threat', 'EC2'), 'THREAT'),
    ]),
  );

  assert.equal(threat.ec2.arn, 'ec2-threat');
  assert.equal(cost.ec2.arn, 'ec2-cost');
});

test('붙어 있는 자원의 THREAT도 그 EC2의 위험으로 센다', () => {
  // 전체 개방 SG가 달린 EC2는 스스로는 SKIP이어도 지금 화면에 있어야 할 행이다.
  const rows = sortRowsByRisk(
    rowsOf([
      judged(asset('ec2-cost', 'EC2'), 'COST_CANDIDATE'),
      judged(asset('ec2-open', 'EC2', [['SECURED_BY', 'sg-open']]), 'SKIP'),
      judged(asset('sg-open', 'SG'), 'THREAT'),
    ]),
  );

  assert.equal(rows[0].ec2.arn, 'ec2-open');
});

test('점수가 같으면 원래 순서를 지킨다', () => {
  // 회차마다 줄이 뒤바뀌면 어제 본 자리에서 같은 자산을 찾지 못한다.
  const rows = sortRowsByRisk(
    rowsOf([
      judged(asset('ec2-1', 'EC2'), 'SKIP'),
      judged(asset('ec2-2', 'EC2'), 'SKIP'),
      judged(asset('ec2-3', 'EC2'), 'SKIP'),
    ]),
  );

  assert.deepEqual(
    rows.map((r) => r.ec2.arn),
    ['ec2-1', 'ec2-2', 'ec2-3'],
  );
});

test('상한을 걸어도 경로 밖 판정은 전량 기준이다', () => {
  // 일부 행만 넣고 배치를 계산하면 잘린 EC2의 볼륨이 `트래픽 경로 밖`으로 내려가 없는 낭비가 생긴다.
  // 그래서 컴포넌트는 buildTopology를 전량으로 돌리고 **그리는 행만** 자른다 — 그 전제를 고정한다.
  const items = [
    asset('ec2-a', 'EC2', [['ATTACHED_TO', 'ebs-a']]),
    asset('ec2-b', 'EC2', [['ATTACHED_TO', 'ebs-b']]),
    asset('ebs-a', 'EBS'),
    asset('ebs-b', 'EBS'),
  ];
  const { rows, orphans } = buildTopology(items);

  assert.equal(orphans.length, 0);
  assert.equal(sortRowsByRisk(rows).slice(0, 1).length, 1);
  // 자르기 전 원본 rows는 그대로다 — 잘라낸 행의 볼륨이 고아가 되지 않는다.
  assert.equal(buildTopology(items).orphans.length, 0);
});

test('목록이 고른 ARN은 상한을 이긴다 — 위험 순위보다 사람이 방금 한 선택이 먼저다', () => {
  const rows = rowsOf([
    judged(asset('ec2-threat', 'EC2'), 'THREAT'),
    judged(asset('ec2-quiet', 'EC2'), 'SKIP'),
  ]);

  // 상한 1이면 위험 순으로는 ec2-threat만 남지만, 고른 것이 있으면 그것을 그린다.
  assert.deepEqual(
    pickRows(rows, { maxRows: 1 }).map((r) => r.ec2.arn),
    ['ec2-threat'],
  );
  assert.deepEqual(
    pickRows(rows, { maxRows: 1, arns: ['ec2-quiet'] }).map((r) => r.ec2.arn),
    ['ec2-quiet'],
  );
  // 없는 ARN은 조용히 무시한다 — 자산이 사라진 회차에 화면이 깨지지 않는다.
  assert.deepEqual(pickRows(rows, { arns: ['ec2-gone'] }), []);
});

test('행 배지는 붙은 자원의 판정까지 본다 — 개방 SG가 달린 EC2는 스스로 제외여도 위협이다', () => {
  const [row] = rowsOf([
    judged(asset('ec2-open', 'EC2', [['SECURED_BY', 'sg-open']]), 'SKIP'),
    judged(asset('sg-open', 'SG'), 'THREAT'),
  ]);

  // `SKIP`(제외)은 조치할 것이 없어 배지에서 뺀다 — 심각한 것부터 나온다.
  assert.deepEqual(rowVerdicts(row), ['THREAT']);
});

test('같은 판정이 여러 자원에 있어도 배지는 한 번만 나온다', () => {
  const [row] = rowsOf([
    judged(asset('ec2-w', 'EC2', [['ATTACHED_TO', 'ebs-1'], ['ATTACHED_TO', 'ebs-2']]), 'COST_CANDIDATE'),
    judged(asset('ebs-1', 'EBS'), 'UNUSED'),
    judged(asset('ebs-2', 'EBS'), 'UNUSED'),
  ]);

  assert.deepEqual(rowVerdicts(row), ['UNUSED', 'COST_CANDIDATE']);
});

test('경로 밖 묶음은 등장 순이 아니라 사전 유형 순서를 따른다', () => {
  const groups = groupOrphansByType([
    asset('lt-1', 'LAUNCH_TEMPLATE'),
    asset('ebs-1', 'EBS'),
    asset('sg-1', 'SG'),
    asset('ebs-2', 'EBS'),
  ]);

  // 사전 선언 순서(EC2 → SG → EBS → NACL → ASG → 시작 템플릿 → 대상 그룹) 그대로다.
  assert.deepEqual(
    groups.map((g) => [g.type, g.assets.length]),
    [
      ['SG', 1],
      ['EBS', 2],
      ['LAUNCH_TEMPLATE', 1],
    ],
  );
});
