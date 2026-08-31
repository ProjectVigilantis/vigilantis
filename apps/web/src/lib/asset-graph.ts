// AST-001 토폴로지 파생 — 화면설계서 v1.5 §4.2. 렌더와 분리해 단위 검증이 가능합니다.

// 같은 디렉터리 상대 경로를 쓴다 — `node --test`는 `@/` 별칭을 해석하지 못한다(타입 전용
// import는 스트리핑돼 사라지므로 `@/types/api`는 그대로 둔다).
import type { AssetItem, RelationType } from '@/types/api';

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
