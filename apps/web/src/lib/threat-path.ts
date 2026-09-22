// DSH-001 통합 위협 토폴로지 — 공개 계약의 `threat_context`(#362 · PR #374)를 **그릴 수 있는 경로**로
// 바꾼다. 렌더와 분리해 단위 검증이 가능하다(`asset-graph.ts`와 같은 이유).
//
// 경로 하나는 `[외부 출발지] ▶ [공격 대상 자산]` 한 쌍이다. 출발지 값의 **의미가 위협 유형마다
// 다르다** — SSH 무차별 대입은 실제로 관측된 공격자 IP이고, 전체 개방 SG는 규칙이 허용한 대역이다
// (`0.0.0.0/0`은 누가 들어왔다는 뜻이 아니다). 그래서 값만 넘기지 않고 `observed`로 그 차이를 함께
// 넘긴다 — 화면이 둘을 같은 말로 덮으면 계약이 가른 것을 화면이 도로 합치게 된다.

// 같은 디렉터리 상대 경로를 쓴다 — `node --test`는 `@/` 별칭을 해석하지 못한다(타입 전용
// import는 스트리핑돼 사라지므로 `@/types/api`는 그대로 둔다).
import type { TopologyRow } from './asset-graph.ts';
import type { AssetItem, IncidentListItem, IncidentStatus, ThreatContext } from '@/types/api';

/** 그릴 수 있는 공격 경로 1건. */
export interface ThreatPath {
  incidentId: string;
  /** 공격이 향한 자산. SSH 무차별 대입은 EC2, 전체 개방 SG는 그 보안 그룹이다. */
  targetArn: string;
  eventType: ThreatContext['event_type'];
  /** 화면에 그릴 출발지 문자열 — 관측 IP 또는 노출 대역. */
  source: string;
  /**
   * `true`면 **관측된** 출발지(SSH 시도의 공격자 IP), `false`면 규칙이 **허용한** 대역이다.
   * 노드 문구를 가르는 값이라 화면이 이 구분을 직접 다시 계산하지 않는다.
   */
  observed: boolean;
  status: IncidentStatus;
}

/** 출발지 값과 그 의미를 위협 유형에서 꺼낸다. 유형이 늘면 여기 한 곳만 는다. */
function sourceOf(context: ThreatContext): { source: string; observed: boolean } {
  return context.event_type === 'SSH_BRUTE_FORCE'
    ? { source: context.source_ip, observed: true }
    : { source: context.exposed_cidr, observed: false };
}

/**
 * 인시던트 목록 → 공격 경로.
 *
 * - **조회 실패(null)와 0건을 가르지 않는다** — 둘 다 그릴 경로가 없다. 그 구분은 대시보드
 *   상태줄과 지표가 이미 맡고 있어 여기서 또 갈라 봐야 화면에 쓸 곳이 없다.
 * - **종료된 인시던트는 뺀다.** 관제자가 닫은 건의 경로까지 그리면 지금 열려 있는 공격과
 *   끝난 공격이 같은 굵기로 겹쳐 그려진다. 조치가 실패한 건(`FAILED`)은 **남긴다** — 위협은
 *   그대로이므로 화면에서 사라지면 안 된다.
 * - **같은 대상·같은 출발지·같은 유형은 한 번만 그린다.** 같은 위협으로 인시던트가 여러 건
 *   열려도 그래프에서는 같은 선 하나다.
 *
 * 순서는 인시던트 목록 순서를 따르지 않고 **경로 자체로 정한다**(대상 ARN → 출발지) — 목록 정렬은
 * 화면마다 다른데, 그때마다 그래프의 노드 순서가 흔들리면 관제자가 어제 본 자리에서 같은 경로를
 * 찾지 못한다.
 */
export function threatPaths(incidents: readonly IncidentListItem[] | null): ThreatPath[] {
  if (incidents === null) return [];

  const byKey = new Map<string, ThreatPath>();
  for (const incident of incidents) {
    if (incident.threat_context === null) continue;
    if (incident.status === 'RESOLVED') continue;

    const { source, observed } = sourceOf(incident.threat_context);
    const key = `${incident.subject_arn}|${incident.threat_context.event_type}|${source}`;
    if (byKey.has(key)) continue;
    byKey.set(key, {
      incidentId: incident.incident_id,
      targetArn: incident.subject_arn,
      eventType: incident.threat_context.event_type,
      source,
      observed,
      status: incident.status,
    });
  }

  return [...byKey.values()].sort(
    (a, b) => a.targetArn.localeCompare(b.targetArn) || a.source.localeCompare(b.source),
  );
}

/** 이 행이 담은 자산의 ARN 전부 — EC2와 그 행에 걸린 엣지의 도착 자산까지. */
function rowArns(row: TopologyRow): Set<string> {
  const arns = new Set<string>([row.ec2.arn]);
  for (const edge of [...row.targetGroups, ...row.volumes, ...row.chips]) arns.add(edge.targetArn);
  return arns;
}

/** 경로 1건 + 그 행에서 찾은 공격 대상 노드. 응답에 없는 ARN이면 `target`이 null이다. */
export interface RowThreat {
  path: ThreatPath;
  target: AssetItem | null;
}

/**
 * 이 EC2 행으로 들어오는 공격 경로.
 *
 * **EC2 자신만 보지 않는다** — 전체 개방 SG의 대상은 그 보안 그룹이고, 그 SG는 EC2 행의
 * 부속 칩으로 그려진다. EC2만 대조하면 정작 인터넷에 열린 경로가 그래프에서 통째로 빠진다.
 */
export function rowThreats(row: TopologyRow, paths: readonly ThreatPath[]): RowThreat[] {
  const arns = rowArns(row);
  const assets = new Map<string, AssetItem>([[row.ec2.arn, row.ec2]]);
  for (const edge of [...row.targetGroups, ...row.volumes, ...row.chips]) {
    if (edge.asset !== null) assets.set(edge.targetArn, edge.asset);
  }

  return paths
    .filter((path) => arns.has(path.targetArn))
    .map((path) => ({ path, target: assets.get(path.targetArn) ?? null }));
}

/**
 * **그리지 않은 경로.** 대시보드는 한 번에 EC2 한 대만 그리므로(`dashboard-topology.tsx`) 고르지
 * 않은 대의 경로와 트래픽 경로 밖 자원(미사용 SG)의 경로는 화면에 선이 없다. 세어서 밝히지 않으면
 * 지금 그려진 경로가 전부라고 읽힌다.
 */
export function undrawnThreats(
  paths: readonly ThreatPath[],
  drawnRows: readonly TopologyRow[],
): ThreatPath[] {
  const drawn = new Set<string>();
  for (const row of drawnRows) {
    for (const arn of rowArns(row)) drawn.add(arn);
  }
  return paths.filter((path) => !drawn.has(path.targetArn));
}

/** 이 자산을 향한 경로 — 목록 칸이 "고르면 보인다"를 표시하는 데 쓴다. */
export function assetThreats(arn: string, paths: readonly ThreatPath[]): ThreatPath[] {
  return paths.filter((path) => path.targetArn === arn);
}
