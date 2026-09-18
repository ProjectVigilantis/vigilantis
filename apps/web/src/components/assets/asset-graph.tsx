'use client';

// AST-001 토폴로지 뷰 — 화면설계서 v1.5 §4.2. 배치 계산은 lib/asset-graph가 하고 여기는 그리기만 합니다.

import { StatusBadge } from '@/components/status-badge';
import {
  buildTopology,
  groupOrphansByType,
  pickRows,
  type AsgRow,
  type GraphEdge,
  type TopologyRow,
} from '@/lib/asset-graph';
import { arnShort, ASSET_TYPE_LABELS } from '@/lib/enum-labels';
import { cn } from '@/lib/utils';
import type { AssetItem, AssetType, UncollectedAssetType, Verdict } from '@/types/api';

/**
 * 노드 테두리 하이라이트(§4.2 · §3.2). 빨강은 `--danger` 토큰 하나뿐이라는 §0.3을 따른다 —
 * `StatusBadge`의 tone 표와 같은 값을 참조한다.
 *
 * 마우스를 올리면 **같은 색 링 1px**이 테두리 바깥에 더해진다(`hover:ring-1`은 아래 공통 클래스).
 * 색을 바꾸지 않고 링으로 강조하는 이유가 둘이다.
 *
 * - `THREAT`는 더 밝힐 여지가 없다. 평상시를 흐리게 깔았다가 올릴 수는 있지만, 그러면 **가만히 있을 때
 *   위협 신호가 약해진다** — 이 화면에서 가장 먼저 보여야 할 것을 마우스 위치에 맡기게 된다.
 * - 링은 테두리 **바깥**에 그려져 자리를 차지하지 않는다. 두께를 키우면 상자 안 내용이 1px씩 밀려
 *   격자 전체가 흔들린다.
 */
const VERDICT_BORDER: Partial<Record<Verdict, string>> = {
  THREAT: 'border-danger ring-danger/70',
  COST_CANDIDATE: 'border-amber-400 ring-amber-400/70 dark:border-amber-600 dark:ring-amber-500/70',
  UNUSED: 'border-amber-400 ring-amber-400/70 dark:border-amber-600 dark:ring-amber-500/70',
};

/**
 * 판정이 없는 노드. `--border`는 배경과 거의 붙어 있어 링만으로는 올렸는지 알기 어렵다 —
 * 테두리 색도 함께 올린다. 색조는 그대로 무채색이라 판정이 생긴 것처럼 읽히지 않는다.
 */
const NODE_BORDER_PLAIN = 'border-border ring-muted-foreground/50 hover:border-muted-foreground';

/** AZ는 EC2·EBS spec에만 있다 — 판별 유니온이라 키 존재로 좁힌다. */
function azOf(asset: AssetItem): string | null {
  return 'availability_zone' in asset.spec ? asset.spec.availability_zone : null;
}

/**
 * 열이 담는 자산 유형. 그 유형을 이번 회차에 **조회조차 못 했으면** 열이 비는데, 빈 열만으로는
 * "그런 자원이 없다"와 "못 가져왔다"가 구분되지 않는다 — 머리글이 그것을 밝힌다.
 * AZ 열은 EC2 spec 에서 나오는 값이라 담는 유형이 없다.
 */
const HEAD_TYPES: { label: string; types: readonly AssetType[] }[] = [
  { label: 'AZ', types: [] },
  { label: '진입 (대상 그룹)', types: ['ALB_TARGET_GROUP'] },
  { label: 'EC2 인스턴스', types: ['EC2'] },
  { label: '후속 (EBS 볼륨)', types: ['EBS'] },
  { label: '부속 (보안 그룹 · NACL · ASG)', types: ['SG', 'NACL', 'AUTO_SCALING_GROUP'] },
];

/**
 * 열 머리글 한 칸. **열 정렬이 설 때만 그린다** — 좁아서 세로로 쌓인 배치에는 열이 없으므로
 * 머리글이 아래 내용과 어긋난 거짓말이 된다(아래 `hidden @3xl/graph:contents` 참조).
 * 그래서 수집 실패 안내는 여기에만 두지 않고 격자 아래 한 줄로도 남긴다.
 */
function Head({ label, failed }: { label: string; failed: UncollectedAssetType[] }) {
  return (
    <span className="text-muted-foreground border-b pb-1.5 text-[10px] whitespace-nowrap">
      {label}
      {failed.length > 0 ? (
        <span
          className="ml-1.5 text-amber-400"
          title={failed
            .map((u) => `${ASSET_TYPE_LABELS[u.asset_type]?.label ?? u.asset_type}: ${u.reason_code}`)
            .join(' · ')}
        >
          수집 실패
        </span>
      ) : null}
    </span>
  );
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
      // 이름이 잘리면 확인할 길이 없어진다 — 툴팁에 전체 이름과 리소스 ID를 남긴다.
      title={asset.name !== null ? `${asset.name} · ${asset.resource_id}` : asset.resource_id}
      className={cn(
        // `max-w-56`이 이름이 상자를 뚫는 것을 막는 실제 장치다. 격자의 열은 `auto`라 **내용의
        // max-content까지 자란다** — 이름이 길면 EC2 열이 그만큼 벌어져 상자가 카드 밖으로 나가고,
        // 마지막 `1fr` 열(부속)은 남는 폭이 없어 찌부러진다. 상자 폭을 묶으면 열도 함께 묶인다.
        // 실측: 170자 이름에서 상자가 1314px로 자라 카드를 186px 뚫고 나갔다.
        'bg-card flex max-w-56 min-w-0 flex-col items-start gap-1 rounded-md border px-2.5 py-1.5 text-left',
        // 링 색은 판정 표에서 오고, 켜는 것은 hover뿐이다 — 색과 조건을 한 군데서 읽히게 갈라 둔다.
        'hover:ring-1',
        VERDICT_BORDER[asset.verdict as Verdict] ?? NODE_BORDER_PLAIN,
        dimmed && 'opacity-40',
      )}
    >
      {/* 첫 줄은 **분류**(유형 · 판정)다. 짧고 길이가 정해져 있어 상자 폭을 흔들지 않는다.
          둘째 줄이 **이름**이다 — 길이를 예측할 수 없는 값은 제 줄을 통째로 쓰게 두고 넘치면
          자른다. 이름을 배지 옆에 두면 배지가 밀려 상자 밖으로 글자가 삐져나온다. */}
      <span className="flex w-full min-w-0 flex-wrap items-center gap-1.5">
        <StatusBadge field="asset_type" value={asset.asset_type} />
        {asset.verdict !== null ? <StatusBadge field="verdict" value={asset.verdict} /> : null}
      </span>
      {/* `w-full min-w-0`이 있어야 truncate가 산다 — 부모 폭을 모르면 브라우저는 자를 지점을
          정하지 못하고 글자를 그대로 흘려보낸다. */}
      <span className="w-full min-w-0 truncate text-sm font-medium">
        {asset.name ?? asset.resource_id}
      </span>
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
      {/* 관계 이름은 줄바꿈하지 않는다 — 두 줄로 접히면 엣지 한 줄이 노드 상자보다 높아져,
          좁은 열에서 화살표가 무엇을 가리키는지 흐려진다.
          `PROTECTED_BY`가 직접 부착이 아니라 **서브넷 일치로 파생된 관계**라는 사실(§4.2)은
          아래 범례 한 줄이 맡는다 — 엣지마다 `(파생)`을 달면 관계 이름보다 꼬리표가 길어진다. */}
      <span className="text-muted-foreground font-mono text-[10px] whitespace-nowrap">
        {edge.relation}
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
      <span className="flex min-w-0 flex-wrap items-center gap-2 self-center">
        {row.targetGroups.map((e) => (
          <Edge key={e.targetArn} edge={e} towardLeft onSelect={onSelect} dimmedArns={dimmedArns} />
        ))}
      </span>
      <span className="min-w-0 self-center">
        <Node
          asset={row.ec2}
          onSelect={onSelect}
          dimmed={dimmedArns !== null && !dimmedArns.has(row.ec2.arn)}
        />
      </span>
      <span className="flex min-w-0 flex-wrap items-center gap-2 self-center">
        {row.volumes.map((e) => (
          <Edge key={e.targetArn} edge={e} onSelect={onSelect} dimmedArns={dimmedArns} />
        ))}
      </span>
      <span className="flex min-w-0 flex-wrap items-center gap-2 self-center">
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

/** 유형 한 줄에 이름을 몇 개까지 펼칠지. 넘치면 건수로 접는다 — 전수는 자산 화면(AST-001)이 맡는다. */
const OFF_PATH_NAMES = 6;

/**
 * 트래픽 경로 밖 목록. **노드 상자로 나열하지 않는다.**
 *
 * 상자는 경로 위 노드(EC2·SG·EBS)와 같은 무게라, 연결이 없어 볼 것이 적은 자원이 경로의 주인공과
 * 같은 크기로 늘어선다. 게다가 이 목록은 길어지기 쉽다 — 한 번 늘면 패널을 통째로 덮어 정작
 * 토폴로지를 밀어낸다. 그래서 **유형별 한 줄 + 건수**로 접고, 이름은 조용한 글자 링크로만 둔다.
 *
 * 판정이 붙은 자원(미사용 EBS 등)은 배지를 남긴다 — 그 한 건이 이 목록에서 **조치할 것**이다.
 *
 * **대시보드도 이 컴포넌트를 그대로 쓴다**(`topology-picker.tsx`) — 자리만 다르고 목록은 같다.
 * 그래서 바깥 여백·구분선만 `className`으로 받고, 안쪽 구조는 호출부가 건드리지 않는다.
 */
export function OffPath({
  orphans,
  onSelect,
  focusedArns = null,
  className,
}: {
  orphans: readonly AssetItem[];
  onSelect: (asset: AssetItem) => void;
  focusedArns?: ReadonlySet<string> | null;
  /** 바깥 여백·구분선만 갈아 끼운다. 기본값은 그래프 아래에 붙는 모양이다. */
  className?: string;
}) {
  const groups = groupOrphansByType(orphans);

  return (
    <div className={cn('flex min-w-0 flex-col gap-2', className ?? 'border-border border-t pt-3')}>
      <p className="text-muted-foreground text-xs">
        트래픽 경로 밖 <span className="tabular-nums">{orphans.length}</span>건 — 어떤 EC2와도 연결이
        없습니다. 비용만 발생합니다.
      </p>
      <ul className="flex flex-col gap-1">
        {groups.map(({ type, assets }) => (
          <li key={type} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
            {/* 유형과 건수를 고정폭으로 세워 줄이 여럿이어도 이름 시작점이 맞는다. */}
            <span className="text-muted-foreground flex w-32 shrink-0 items-baseline justify-between gap-2">
              <span>{ASSET_TYPE_LABELS[type]?.label ?? type}</span>
              <span className="tabular-nums">{assets.length}</span>
            </span>
            {assets.slice(0, OFF_PATH_NAMES).map((asset) => (
              <button
                key={asset.arn}
                type="button"
                onClick={() => onSelect(asset)}
                className={cn(
                  'text-muted-foreground hover:text-foreground flex items-center gap-1.5 font-mono underline-offset-2 hover:underline',
                  focusedArns !== null && !focusedArns.has(asset.arn) && 'opacity-40',
                )}
              >
                {asset.name ?? asset.resource_id}
                {asset.verdict !== null ? (
                  <StatusBadge field="verdict" value={asset.verdict} />
                ) : null}
              </button>
            ))}
            {assets.length > OFF_PATH_NAMES ? (
              <span className="text-muted-foreground">
                외 {assets.length - OFF_PATH_NAMES}개 — 자산 화면에서 전체 보기
              </span>
            ) : null}
          </li>
        ))}
      </ul>
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
  uncollected = [],
  maxRows,
  rowArns,
  showOffPath = true,
}: {
  items: readonly AssetItem[];
  onSelect: (asset: AssetItem) => void;
  focusedArns?: ReadonlySet<string> | null;
  /**
   * 이번 회차에 조회를 못 한 유형(`GET /assets` 봉투). 빈 열을 "없음"으로 읽히게 두지 않으려고
   * 받는다 — 기본값이 빈 배열이라 이 정보를 안 넘기는 호출부는 지금까지처럼 그린다.
   */
  uncollected?: readonly UncollectedAssetType[];
  /**
   * 그릴 EC2 행의 상한. **대시보드 전용 장치다** — 자산 화면은 전수를 봐야 하므로 넘기지 않는다.
   *
   * 상한을 걸면 위험한 행부터 남기고(`sortRowsByRisk`) 나머지는 건수로 접는다. 자르는 것은
   * **그리는 행뿐이고 배치 계산은 전량으로 한다** — 일부만 넣고 계산하면 잘린 EC2에 달린
   * 볼륨·보안 그룹이 갈 곳을 잃어 `트래픽 경로 밖`으로 내려간다. 없는 낭비를 만들어 내는 셈이다.
   */
  maxRows?: number;
  /**
   * 그릴 EC2 행을 ARN으로 직접 고른다. **대시보드가 목록으로 그래프를 몰 때 쓴다** — 사람이
   * 고른 것이므로 `maxRows`보다 우선하고, 몇 대를 접었는지 세는 안내도 그리지 않는다(무엇이
   * 화면 밖에 있는지는 그 목록이 이미 보여 준다).
   */
  rowArns?: readonly string[];
  /**
   * `트래픽 경로 밖` 목록을 이 그래프 아래에 그릴지. **대시보드만 끈다** — 거기서는 같은 목록을
   * 인스턴스 선택 목록 옆에 세우기 때문이다. 끈다고 계산이 달라지지는 않는다(경로 안/밖 판정은
   * 언제나 전량 기준이다). 그리는 자리만 옮기는 스위치다.
   */
  showOffPath?: boolean;
}) {
  const { rows: allRows, asgRows, orphans } = buildTopology(items);
  const rows = pickRows(allRows, { maxRows, arns: rowArns });
  // 목록이 그래프를 몰면 접힌 행을 여기서 세지 않는다 — 그 자리는 목록이 맡는다.
  const hiddenRows = rowArns === undefined ? allRows.length - rows.length : 0;

  return (
    <div className="@container/graph flex flex-col gap-4">
      {/* 열 = AZ · 진입(TG) · EC2 · 후속(EBS) · 부속(SG·NACL·ASG). 좁은 화면에서는
          열 정렬을 포기하고 세로로 쌓는다.
          기준은 **뷰포트가 아니라 이 그래프가 실제로 받은 폭**이다(`@container/graph`). 대시보드는
          토폴로지를 전폭이 아닌 본문 열에 두고 그 옆에 목록 두 줄을 세워(dashboard-topology.tsx)
          같은 1920px 화면에서도 자산 화면(AST-001, 전폭)의 절반쯤을 받는다 — 뷰포트로 걸면
          좁은 쪽에서 5열이 터진다.
          접는 기준이 1024px가 아니라 **768px**인 이유: 노드 상자에 폭 상한(`max-w-56`)과 말줄임이
          생겨 "억지로 밀어 넣으면 이름이 잘린다"는 옛 걱정이 사라졌다. 접힌 배치가 이 카드에서
          가장 높으므로, 열이 설 수 있는 폭이면 세우는 편이 낫다. */}
      <div className="grid gap-x-4 gap-y-3 @3xl/graph:grid-cols-[auto_auto_auto_auto_minmax(0,1fr)] @3xl/graph:items-start">
        {/* 머리글도 `contents`로 얹어야 아래 행들과 같은 열 트랙을 쓴다. 열이 없는 접힌 배치에서는
            통째로 감춘다 — 그때는 각 행이 `AZ → 진입 → EC2 → 후속 → 부속` 순서로 쌓이므로
            머리글이 첫 행에만 붙은 것처럼 보이게 된다. */}
        <div className="hidden @3xl/graph:contents">
          {HEAD_TYPES.map(({ label, types }) => (
            <Head
              key={label}
              label={label}
              failed={uncollected.filter((u) => types.includes(u.asset_type))}
            />
          ))}
        </div>

        {rows.map((row) => (
          <Row key={row.ec2.arn} row={row} onSelect={onSelect} dimmedArns={focusedArns} />
        ))}
      </div>

      {/* 접은 행을 **숨기지 않고 센다** — 몇 대가 화면 밖에 있는지 모르면 이 그래프가 전부라고 읽힌다. */}
      {hiddenRows > 0 ? (
        <p className="text-muted-foreground text-xs">
          위험 신호가 큰 <span className="tabular-nums">{rows.length}</span>대만 그렸습니다 — 나머지{' '}
          <span className="tabular-nums">{hiddenRows}</span>대는 자산 목록에서 전체 보기
        </p>
      ) : null}

      {/* 열 정렬이 접히면 위 머리글이 통째로 감춰지므로, 같은 사실을 폭과 무관하게 한 번 더 남긴다.
          빈 열을 "그런 자원이 없다"로 읽고 넘어가면 조치가 필요한 상태가 조용히 지나간다. */}
      {uncollected.length > 0 ? (
        <p className="text-xs text-amber-400">
          이번 수집에서 조회하지 못한 유형이 있습니다 —{' '}
          {uncollected
            .map(
              (u) =>
                `${ASSET_TYPE_LABELS[u.asset_type]?.label ?? u.asset_type}(${u.reason_code})`,
            )
            .join(' · ')}
          . 해당 열이 비어 있는 것은 <strong>자원이 없다는 뜻이 아닙니다.</strong>
        </p>
      ) : null}

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

      {showOffPath && orphans.length > 0 ? (
        <OffPath orphans={orphans} onSelect={onSelect} focusedArns={focusedArns} />
      ) : null}

      <p className="text-muted-foreground text-xs">
        ▶ 관계 방향(출발 → 도착) · 위협 빨강 · 낭비 후보 노랑 · <code>PROTECTED_BY</code>는 서브넷
        일치 파생 관계입니다(직접 부착 아님).
      </p>
    </div>
  );
}
