// DSH-001 공격 경로 파생 회귀 — `npm test`. 계약(`threat_context`)이 가른 것을 화면이 도로
// 합치지 않는지, 그리지 않은 경로를 세는지가 이 파일의 몫입니다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { buildTopology } from './asset-graph.ts';
import {
  assetThreats,
  rowThreats,
  splitUndrawn,
  threatPaths,
  undrawnThreats,
} from './threat-path.ts';
import type {
  AssetItem,
  AssetType,
  IncidentListItem,
  IncidentStatus,
  RelationType,
  ThreatContext,
} from '../types/api.ts';

const asset = (
  arn: string,
  asset_type: AssetType,
  rels: [RelationType, string][] = [],
): AssetItem =>
  ({
    arn,
    asset_type,
    name: arn,
    resource_id: arn,
    relationships: rels.map(([relation_type, target_arn]) => ({ relation_type, target_arn })),
  }) as AssetItem;

/** 골든 SecOps 입력과 같은 모양 — SSH 대상은 EC2, 전체 개방 대상은 그 EC2에 붙은 SG다. */
const inventory = (): AssetItem[] => [
  asset('ec2-a', 'EC2', [
    ['SECURED_BY', 'sg-open'],
    ['ATTACHED_TO', 'ebs-attached'],
  ]),
  asset('ec2-b', 'EC2'),
  asset('sg-open', 'SG'),
  asset('sg-unused', 'SG'),
  asset('ebs-attached', 'EBS'),
];

const incident = (
  incident_id: string,
  subject_arn: string,
  threat_context: ThreatContext | null,
  status: IncidentStatus = 'AWAITING_APPROVAL',
): IncidentListItem =>
  ({
    incident_id,
    title: incident_id,
    subject_arn,
    category: threat_context === null ? 'FINOPS' : 'SECOPS',
    status,
    threat_context,
  }) as IncidentListItem;

const ssh = (source_ip: string): ThreatContext => ({ event_type: 'SSH_BRUTE_FORCE', source_ip });
const openIp = (exposed_cidr: string): ThreatContext => ({ event_type: 'OPEN_IP', exposed_cidr });

test('SSH는 관측 출발지, 전체 개방은 노출 대역으로 의미가 갈린다', () => {
  const paths = threatPaths([
    incident('inc-1', 'ec2-a', ssh('203.0.113.10')),
    incident('inc-2', 'sg-open', openIp('0.0.0.0/0')),
  ]);

  assert.deepEqual(
    paths.map((p) => [p.targetArn, p.source, p.observed]),
    [
      ['ec2-a', '203.0.113.10', true],
      ['sg-open', '0.0.0.0/0', false],
    ],
    '0.0.0.0/0을 관측된 공격자 IP와 같은 값으로 내보내면 화면이 계약에 없는 사실을 주장한다',
  );
});

test('문맥이 없는 인시던트와 조회 실패(null)는 경로를 만들지 않는다', () => {
  assert.deepEqual(threatPaths(null), []);
  assert.deepEqual(threatPaths([]), []);
  assert.deepEqual(threatPaths([incident('inc-fin', 'ec2-a', null)]), []);
});

test('종료된 인시던트는 빼고 조치 실패는 남긴다', () => {
  const paths = threatPaths([
    incident('inc-done', 'ec2-a', ssh('203.0.113.10'), 'RESOLVED'),
    incident('inc-failed', 'ec2-b', ssh('203.0.113.20'), 'FAILED'),
  ]);

  assert.deepEqual(
    paths.map((p) => p.incidentId),
    ['inc-failed'],
    '조치가 실패한 건은 위협이 그대로라 화면에서 사라지면 안 된다',
  );
});

test('같은 대상·출발지·유형은 인시던트가 여럿이어도 한 선이다', () => {
  const paths = threatPaths([
    incident('inc-1', 'ec2-a', ssh('203.0.113.10')),
    incident('inc-2', 'ec2-a', ssh('203.0.113.10')),
    incident('inc-3', 'ec2-a', ssh('203.0.113.99')),
  ]);

  assert.deepEqual(
    paths.map((p) => p.source),
    ['203.0.113.10', '203.0.113.99'],
  );
});

test('순서는 목록 정렬이 아니라 경로(대상 → 출발지)로 고정한다', () => {
  const forward = threatPaths([
    incident('inc-1', 'ec2-a', ssh('203.0.113.10')),
    incident('inc-2', 'sg-open', openIp('0.0.0.0/0')),
  ]);
  const reversed = threatPaths([
    incident('inc-2', 'sg-open', openIp('0.0.0.0/0')),
    incident('inc-1', 'ec2-a', ssh('203.0.113.10')),
  ]);

  assert.deepEqual(
    forward.map((p) => p.targetArn),
    reversed.map((p) => p.targetArn),
    '목록 정렬이 바뀔 때마다 그래프의 노드 순서가 흔들리면 어제 본 자리에서 같은 경로를 못 찾는다',
  );
});

test('행에 붙은 SG로 향한 경로도 그 EC2 행이 받는다', () => {
  const { rows } = buildTopology(inventory());
  const rowA = rows.find((r) => r.ec2.arn === 'ec2-a');
  assert.ok(rowA);

  const paths = threatPaths([
    incident('inc-1', 'ec2-a', ssh('203.0.113.10')),
    incident('inc-2', 'sg-open', openIp('0.0.0.0/0')),
    incident('inc-3', 'sg-unused', openIp('10.0.0.0/8')),
  ]);
  const threats = rowThreats(rowA, paths);

  assert.deepEqual(
    threats.map((t) => [t.path.targetArn, t.target?.arn ?? null]),
    [
      ['ec2-a', 'ec2-a'],
      ['sg-open', 'sg-open'],
    ],
    'EC2만 대조하면 인터넷에 열린 SG 경로가 그래프에서 통째로 빠진다',
  );
});

test('그리지 않은 행·경로 밖 자원으로 향한 경로를 센다', () => {
  const { rows, orphans } = buildTopology(inventory());
  const paths = threatPaths([
    incident('inc-1', 'ec2-a', ssh('203.0.113.10')),
    incident('inc-2', 'ec2-b', ssh('203.0.113.20')),
    incident('inc-3', 'sg-unused', openIp('0.0.0.0/0')),
  ]);

  // 대시보드는 한 번에 한 대만 그린다 — 고른 대가 ec2-a인 상황.
  const drawn = rows.filter((r) => r.ec2.arn === 'ec2-a');
  assert.deepEqual(
    undrawnThreats(paths, drawn).map((p) => p.targetArn),
    ['ec2-b', 'sg-unused'],
  );

  // 경로 밖 자원은 그릴 행이 아예 없어 목록 표시로만 알린다.
  assert.equal(orphans.some((a) => a.arn === 'sg-unused'), true);
  assert.deepEqual(
    assetThreats('sg-unused', paths).map((p) => p.source),
    ['0.0.0.0/0'],
  );
});

test('그리지 않은 경로를 "고르면 그려지는 것"과 "그릴 행이 없는 것"으로 가른다', () => {
  const { rows } = buildTopology(inventory());
  const paths = threatPaths([
    incident('inc-1', 'ec2-b', ssh('203.0.113.20')),
    incident('inc-2', 'sg-unused', openIp('0.0.0.0/0')),
  ]);

  // 고른 대가 ec2-a라 두 경로 모두 지금은 선이 없다 — 그 둘의 **안내가 서로 다르다.**
  const drawn = rows.filter((r) => r.ec2.arn === 'ec2-a');
  const split = splitUndrawn(undrawnThreats(paths, drawn), rows);

  assert.deepEqual(
    split.selectable.map((p) => p.targetArn),
    ['ec2-b'],
    '목록에서 고르면 그려지는 경로다 — "고르면 그려집니다" 안내가 맞는 것은 이쪽뿐이다',
  );
  assert.deepEqual(
    split.offPath.map((p) => p.targetArn),
    ['sg-unused'],
    '어떤 EC2에도 안 붙은 자원은 눌러도 그래프가 아니라 자산 상세로 가므로 같은 안내가 거짓이 된다',
  );
});

test('행에 붙은 SG로 향한 경로는 그 EC2를 고르면 그려지므로 selectable이다', () => {
  const { rows } = buildTopology(inventory());
  const paths = threatPaths([incident('inc-1', 'sg-open', openIp('0.0.0.0/0'))]);

  const split = splitUndrawn(undrawnThreats(paths, []), rows);

  assert.deepEqual(
    split.selectable.map((p) => p.targetArn),
    ['sg-open'],
    'sg-open은 ec2-a 행의 부속 칩이라 그 대를 고르면 경로가 그려진다 — 경로 밖으로 세면 안 된다',
  );
  assert.deepEqual(split.offPath, []);
});
