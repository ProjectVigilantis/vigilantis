'use client';

// AST-001 토폴로지 뷰 — 화면설계서 v1.5 §4.2. 배치 계산은 lib/asset-graph가 하고 여기는 그리기만 합니다.

import { StatusBadge } from '@/components/status-badge';
import { buildTopology, type AsgRow, type GraphEdge, type TopologyRow } from '@/lib/asset-graph';
import { arnShort } from '@/lib/enum-labels';
import { cn } from '@/lib/utils';
import type { AssetItem, Verdict } from '@/types/api';

/**
 * 노드 테두리 하이라이트(§4.2 · §3.2). 빨강은 `--danger` 토큰 하나뿐이라는 §0.3을 따른다 —
 * `StatusBadge`의 tone 표와 같은 값을 참조한다.
 */
const VERDICT_BORDER: Partial<Record<Verdict, string>> = {
  THREAT: 'border-danger',
  COST_CANDIDATE: 'border-orange-400 dark:border-orange-700',
  UNUSED: 'border-orange-400 dark:border-orange-700',
};

/** AZ는 EC2·EBS spec에만 있다 — 판별 유니온이라 키 존재로 좁힌다. */
function azOf(asset: AssetItem): string | null {
  return 'availability_zone' in asset.spec ? asset.spec.availability_zone : null;
}

function Node({
  asset,
  onSelect,
  dimmed,
}: {
  asset: AssetItem;
  onSelect: (asset: AssetItem) => void;
  dimmed: boolean;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(asset)}
      className={cn(
        'bg-card flex min-w-0 flex-col items-start gap-1 rounded-md border px-2.5 py-1.5 text-left',
        VERDICT_BORDER[asset.verdict as Verdict] ?? 'border-border',
        dimmed && 'opacity-40',
      )}
    >
      <span className="flex flex-wrap items-center gap-1.5">
        <StatusBadge field="asset_type" value={asset.asset_type} />
        <span className="truncate text-sm font-medium">{asset.name ?? asset.resource_id}</span>
      </span>
      {asset.verdict !== null ? <StatusBadge field="verdict" value={asset.verdict} /> : null}
    </button>
  );
}

/**
 * 엣지 하나. **방향 기호는 계약의 출발→도착을 가리킨다**(§4.2 6종 전부 방향이 정의돼 있다).
 * 3계층 배치는 트래픽 흐름(TG → EC2 → EBS)이라 TG는 EC2 왼쪽에 놓이는데, 관계 자체는
 * `REGISTERED_IN`(EC2 → TG)이다. 그래서 왼쪽 열만 `◀`로 그려 **배치와 방향이 서로를
 * 반박하지 않게** 한다. 둘 다 ▶로 쓰면 화면이 계약에 없는 방향을 주장하게 된다.
 */
function Edge({
  edge,
  towardLeft = false,
  onSelect,
  dimmedArns,
}: {
  edge: GraphEdge;
  towardLeft?: boolean;
  onSelect: (asset: AssetItem) => void;
  dimmedArns: ReadonlySet<string> | null;
}) {
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <span aria-hidden className="text-muted-foreground text-xs">
        {towardLeft ? '◀' : '▶'}
      </span>
      <span className="text-muted-foreground font-mono text-[10px]">
        {edge.relation}
        {/* NACL은 EC2에 직접 부착된 것이 아니라 서브넷 일치로 파생된 관계다(§4.2). */}
        {edge.relation === 'PROTECTED_BY' ? <span className="ml-1">(파생)</span> : null}
      </span>
      {edge.asset !== null ? (
        <Node
          asset={edge.asset}
          onSelect={onSelect}
          dimmed={dimmedArns !== null && !dimmedArns.has(edge.asset.arn)}
        />
      ) : (
        // 응답에 없는 target_arn — 관계를 버리지 않고 ARN만 남긴다(§4.2 예외).
        <span className="text-muted-foreground border-border rounded-md border border-dashed px-2 py-1 font-mono text-xs">
          {arnShort(edge.targetArn)}
        </span>
      )}
    </span>
  );
}

function Row({
  row,
  onSelect,
  dimmedArns,
}: {
  row: TopologyRow;
  onSelect: (asset: AssetItem) => void;
  dimmedArns: ReadonlySet<string> | null;
}) {
  const az = azOf(row.ec2);
  // `contents`로 셀을 부모 그리드에 직접 얹는다 — 행마다 따로 그리드를 만들면 열 너비가
  // 행끼리 안 맞아 3계층(TG → EC2 → EBS)이 성립하지 않는다.
  return (
    <div className="contents">
      <span className="text-muted-foreground bg-muted self-center justify-self-start rounded px-1.5 py-0.5 font-mono text-[10px]">
        {az ?? 'AZ 미상'}
      </span>
      <span className="flex flex-wrap items-center gap-2 self-center">
        {row.targetGroups.map((e) => (
          <Edge key={e.targetArn} edge={e} towardLeft onSelect={onSelect} dimmedArns={dimmedArns} />
        ))}
      </span>
      <span className="self-center">
        <Node
          asset={row.ec2}
          onSelect={onSelect}
          dimmed={dimmedArns !== null && !dimmedArns.has(row.ec2.arn)}
        />
      </span>
      <span className="flex flex-wrap items-center gap-2 self-center">
        {row.volumes.map((e) => (
          <Edge key={e.targetArn} edge={e} onSelect={onSelect} dimmedArns={dimmedArns} />
        ))}
      </span>
      <span className="flex flex-wrap items-center gap-2 self-center">
        {row.chips.map((e) => (
          <Edge
            key={`${e.relation}:${e.targetArn}`}
            edge={e}
            onSelect={onSelect}
            dimmedArns={dimmedArns}
          />
        ))}
      </span>
    </div>
  );
}

/**
 * 자산 그래프. DSH-001 통합 위협 토폴로지가 이 컴포넌트에 외부 Source IP 노드와 공격 경로
 * 엣지를 덧붙이는 구조라(설계서 §8), 배치 계산과 렌더를 여기서 닫아 둔다.
 *
 * `dimmedArns`는 **숨김이 아니라 초점**이다 — 유형 필터로 노드를 빼면 엣지의 도착 노드가
 * 사라져 그래프가 끊어진 것처럼 보인다. 연결성은 유지하고 필터 밖 노드만 흐리게 둔다.
 */
export function AssetGraph({
  items,
  onSelect,
  focusedArns = null,
}: {
  items: readonly AssetItem[];
  onSelect: (asset: AssetItem) => void;
  focusedArns?: ReadonlySet<string> | null;
}) {
  const { rows, asgRows, orphans } = buildTopology(items);

  return (
    <div className="flex flex-col gap-4">
      {/* 열 = AZ · 진입(TG) · EC2 · 후속(EBS) · 부속(SG·NACL·ASG). 좁은 화면에서는
          열 정렬을 포기하고 세로로 쌓는다 — 억지로 밀어 넣으면 노드 이름이 잘린다. */}
      <div className="grid gap-x-4 gap-y-3 xl:grid-cols-[auto_auto_auto_auto_minmax(0,1fr)] xl:items-start">
        {rows.map((row) => (
          <Row key={row.ec2.arn} row={row} onSelect={onSelect} dimmedArns={focusedArns} />
        ))}
      </div>

      {asgRows.length > 0 ? (
        <ul className="border-border flex flex-col gap-2 border-t pt-3">
          {asgRows.map((r: AsgRow) => (
            <li key={r.asg.arn} className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <Node
                asset={r.asg}
                onSelect={onSelect}
                dimmed={focusedArns !== null && !focusedArns.has(r.asg.arn)}
              />
              {r.templates.map((e) => (
                <Edge key={e.targetArn} edge={e} onSelect={onSelect} dimmedArns={focusedArns} />
              ))}
              <span className="text-muted-foreground text-xs">EC2와 직접 관계 없음</span>
            </li>
          ))}
        </ul>
      ) : null}

      {orphans.length > 0 ? (
        <div className="border-border flex flex-col gap-2 border-t pt-3">
          <p className="text-muted-foreground text-xs">
            트래픽 경로 밖 — 어떤 EC2와도 연결이 없습니다. 비용만 발생합니다.
          </p>
          <ul className="flex flex-wrap gap-2">
            {orphans.map((asset) => (
              <li key={asset.arn}>
                <Node
                  asset={asset}
                  onSelect={onSelect}
                  dimmed={focusedArns !== null && !focusedArns.has(asset.arn)}
                />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <p className="text-muted-foreground text-xs">
        ▶ 관계 방향(출발 → 도착) · 위협 빨강 · 낭비 후보 주황 · <code>PROTECTED_BY</code>는 서브넷
        일치 파생 관계입니다(직접 부착 아님).
      </p>
    </div>
  );
}
