// AST-001 토폴로지 파생 — 화면설계서 v1.5 §4.2. 렌더와 분리해 단위 검증이 가능합니다.

// 같은 디렉터리 상대 경로를 쓴다 — `node --test`는 `@/` 별칭을 해석하지 못한다(타입 전용
// import는 스트리핑돼 사라지므로 `@/types/api`는 그대로 둔다).
import { ASSET_TYPE_ORDER } from './enum-labels.ts';
import type { AssetItem, AssetType, RelationType, Verdict } from '@/types/api';

/**
 * 엣지 하나. `asset`은 `target_arn`이 응답 `items[]`에 없으면 null이다 —
 * `collection_status`가 `PARTIAL`·`FAILED`면 실제로 일어나는 일이라(§4.2 예외)
 * 관계를 버리지 않고 ARN만이라도 남긴다.
 */
export interface GraphEdge {
  relation: RelationType;
  targetArn: string;
  asset: AssetItem | null;
}

/** EC2 한 대가 만드는 행 — `[ALB TG] ▶ [EC2] ▶ [EBS]` + 트레일링 칩(SG·NACL·ASG). */
export interface TopologyRow {
  ec2: AssetItem;
  targetGroups: GraphEdge[];
  volumes: GraphEdge[];
  chips: GraphEdge[];
}

/** `ASG ▶ 시작 템플릿` — EC2와 직접 관계가 없어 설계서가 별도 줄로 뺀 축이다. */
export interface AsgRow {
  asg: AssetItem;
  templates: GraphEdge[];
}

export interface AssetTopology {
  rows: TopologyRow[];
  asgRows: AsgRow[];
  /** 트래픽 경로 밖 — 어떤 EC2에서도 (전이적으로) 닿지 않는 자원. */
  orphans: AssetItem[];
}

/** EC2 행에서 도착 노드가 따로 열을 갖는 관계. 나머지는 트레일링 칩이다. */
const COLUMN_RELATIONS: Record<string, 'targetGroups' | 'volumes'> = {
  REGISTERED_IN: 'targetGroups',
  ATTACHED_TO: 'volumes',
};

function edgesOf(asset: AssetItem, byArn: Map<string, AssetItem>): GraphEdge[] {
  return asset.relationships.map((r) => ({
    relation: r.relation_type,
    targetArn: r.target_arn,
    asset: byArn.get(r.target_arn) ?? null,
  }));
}

/**
 * `items[]` → 3계층 배치 모델(§4.2).
 *
 * **경로 안/밖 판정은 EC2에서 출발한 도달 가능성**이다. `ASG → 시작 템플릿`처럼 한 다리
 * 건너 닿는 자원도 경로 안이다 — 직접 관계만 보면 LT가 "아예 연결이 없는 것"으로 내려가
 * 미연결 EBS와 같은 자리에 놓인다. 그 둘을 가르는 게 이 분리의 목적이다.
 *
 * EC2는 관계가 없어도 행을 갖는다 — 경로의 척추이고, 모든 `relationships`가 비어도
 * 노드는 그려야 한다(§4.2 예외 "빈 화면으로 두지 않는다").
 */
export function buildTopology(items: readonly AssetItem[]): AssetTopology {
  const byArn = new Map(items.map((a) => [a.arn, a]));
  const ec2s = items.filter((a) => a.asset_type === 'EC2');

  const rows: TopologyRow[] = ec2s.map((ec2) => {
    const row: TopologyRow = { ec2, targetGroups: [], volumes: [], chips: [] };
    for (const edge of edgesOf(ec2, byArn)) {
      const column = COLUMN_RELATIONS[edge.relation];
      if (column === undefined) row.chips.push(edge);
      else row[column].push(edge);
    }
    return row;
  });

  // EC2에서 시작해 엣지를 따라가며 경로 안을 넓힌다.
  const inPath = new Set(ec2s.map((a) => a.arn));
  const queue: AssetItem[] = [...ec2s];
  while (queue.length > 0) {
    const current = queue.pop() as AssetItem;
    for (const edge of edgesOf(current, byArn)) {
      if (inPath.has(edge.targetArn)) continue;
      inPath.add(edge.targetArn);
      if (edge.asset !== null) queue.push(edge.asset);
    }
  }

  const asgRows: AsgRow[] = items
    .filter((a) => a.asset_type === 'AUTO_SCALING_GROUP')
    .map((asg) => ({ asg, templates: edgesOf(asg, byArn).filter((e) => e.relation === 'USES') }))
    .filter((r) => r.templates.length > 0);

  return { rows, asgRows, orphans: items.filter((a) => !inPath.has(a.arn)) };
}

/**
 * 행 하나의 위험 점수. 높을수록 먼저 보여야 한다.
 *
 * **대시보드는 전수를 싣는 자리가 아니다** — EC2가 늘면 토폴로지가 카드를 넘어 페이지를 덮고,
 * 정작 함께 봐야 할 지표·추이를 아래로 밀어낸다. 그래서 대시보드는 위험한 행부터 몇 줄만 그리고
 * 전수는 자산 화면(AST-001)이 맡는다. 무엇을 남길지는 이 점수 하나가 정한다.
 *
 * EC2 자신의 판정만 보지 않는다 — **붙어 있는 자원의 판정도 그 EC2의 위험**이다. 전체 개방 SG가
 * 달린 EC2는 스스로는 `SKIP`이어도 지금 화면에 있어야 할 행이다.
 */
export function rowRisk(row: TopologyRow): number {
  const attached = [...row.targetGroups, ...row.volumes, ...row.chips]
    .map((e) => e.asset?.verdict ?? null)
    .filter((v): v is Verdict => v !== null);
  const verdicts: Verdict[] = [...(row.ec2.verdict ? [row.ec2.verdict] : []), ...attached];

  if (row.ec2.verdict === 'THREAT') return 4;
  if (verdicts.includes('THREAT')) return 3;
  if (row.ec2.verdict === 'COST_CANDIDATE' || row.ec2.verdict === 'UNUSED') return 2;
  if (verdicts.some((v) => v === 'COST_CANDIDATE' || v === 'UNUSED')) return 1;
  return 0;
}

/**
 * 위험한 행부터 정렬한다. **같은 점수는 원래 순서를 지킨다**(안정 정렬) — 회차마다 줄 순서가
 * 흔들리면 관제자가 어제 본 자리에서 같은 자산을 찾지 못한다.
 */
export function sortRowsByRisk(rows: readonly TopologyRow[]): TopologyRow[] {
  return rows
    .map((row, index) => ({ row, index, risk: rowRisk(row) }))
    .sort((a, b) => b.risk - a.risk || a.index - b.index)
    .map((r) => r.row);
}

/**
 * **실제로 그릴 행**을 고른다. 고르는 방법은 둘이고, `arns`가 있으면 그쪽이 이긴다.
 *
 * - `arns` — 사람이 목록에서 고른 EC2. 대시보드가 쓰는 길이다. 위험 순위보다 **사람이 방금 한
 *   선택**이 우선이므로 상한을 보지 않는다. 없는 ARN은 조용히 무시한다(자산이 사라진 회차).
 * - `maxRows` — 상한. 위험한 행부터 채운다(`sortRowsByRisk`).
 *
 * 둘 다 없거나 행이 상한보다 적으면 **원래 순서 그대로** 둔다 — 고를 필요가 없는데 순서만 바꾸면
 * 회차마다 줄이 흔들려 관제자가 어제 본 자리에서 같은 자산을 찾지 못한다.
 */
export function pickRows(
  rows: readonly TopologyRow[],
  options: { maxRows?: number; arns?: readonly string[] } = {},
): TopologyRow[] {
  const { maxRows, arns } = options;
  if (arns !== undefined) return rows.filter((row) => arns.includes(row.ec2.arn));
  if (maxRows === undefined || rows.length <= maxRows) return [...rows];
  return sortRowsByRisk(rows).slice(0, maxRows);
}

/** 목록 배지로 그릴 판정 — 심각한 것부터. `SKIP`은 조치할 것이 없어 뺀다. */
const ACTIONABLE: readonly Verdict[] = ['THREAT', 'UNUSED', 'COST_CANDIDATE'];

/**
 * 이 행이 담은 **조치 대상 판정**. EC2 자신과 붙은 자원(대상 그룹·볼륨·보안 그룹·NACL)을 합쳐 본다.
 *
 * 목록에서 EC2 한 대를 고르기 전에 알아야 할 것은 "이 대에 볼 것이 있나"인데, 그 근거가 EC2
 * 자신의 판정만은 아니다 — **전체 개방 SG가 달린 EC2는 스스로는 `SKIP`이어도 지금 볼 대상**이다.
 * 같은 이유로 `rowRisk`도 붙은 자원의 판정을 본다.
 */
export function rowVerdicts(row: TopologyRow): Verdict[] {
  const found = new Set<Verdict>();
  if (row.ec2.verdict !== null) found.add(row.ec2.verdict);
  for (const edge of [...row.targetGroups, ...row.volumes, ...row.chips]) {
    if (edge.asset?.verdict != null) found.add(edge.asset.verdict);
  }
  return ACTIONABLE.filter((v) => found.has(v));
}

/**
 * 트래픽 경로 밖 자원을 **유형별로 묶는다.** 순서는 등장 순이 아니라 사전 선언 순서(`ASSET_TYPE_ORDER`)로
 * 고정한다 — 회차마다 줄이 뒤바뀌면 관제자가 어제 본 자리에서 같은 유형을 찾지 못한다.
 *
 * 묶는 일을 여기 두는 이유는 **그리는 자리가 둘**이라서다 — 자산 화면은 그래프 아래 가로로,
 * 대시보드는 인스턴스 목록 옆 좁은 칸에 세로로 그린다. 순서가 갈리면 같은 데이터가 두 화면에서
 * 다르게 읽힌다.
 */
export function groupOrphansByType(
  orphans: readonly AssetItem[],
): { type: AssetType; assets: AssetItem[] }[] {
  const byType = new Map<AssetType, AssetItem[]>();
  for (const asset of orphans) {
    const bucket = byType.get(asset.asset_type);
    if (bucket) bucket.push(asset);
    else byType.set(asset.asset_type, [asset]);
  }
  return [...byType.entries()]
    .map(([type, assets]) => ({ type, assets }))
    .sort((a, b) => ASSET_TYPE_ORDER.indexOf(a.type) - ASSET_TYPE_ORDER.indexOf(b.type));
}
