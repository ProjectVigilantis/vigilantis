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
import {
  rowThreats,
  splitUndrawn,
  undrawnThreats,
  type RowThreat,
  type ThreatPath,
} from '@/lib/threat-path';
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
 * 공격 경로 열의 머리글. 자산 유형을 담지 않는 열이라 수집 실패 표시가 없다 — 이 열의 값은
 * 자산 수집이 아니라 인시던트 계약(`threat_context`)에서 온다.
 */
const THREAT_HEAD: { label: string; types: readonly AssetType[] } = {
  label: '외부 출발지 (인터넷)',
  types: [],
};

/**
 * 열 머리글 한 칸. **열 정렬이 설 때만 그린다** — 좁아서 세로로 쌓인 배치에는 열이 없으므로
 * 머리글이 아래 내용과 어긋난 거짓말이 된다(아래 `hidden …:contents` 참조. 기준 폭은 공격 경로
 * 열이 서는지에 따라 768px과 978px로 갈린다).
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

/**
 * 외부 출발지 노드 + 공격 경로 엣지 1건. **자산 노드가 아니다** — 계정 안에 없는 것이라
 * 누를 곳도, 상세로 갈 곳도 없다. 그래서 상자 모양은 자산 노드와 맞추되 버튼이 아니고,
 * 색은 `THREAT` 판정과 같은 `--danger` 하나만 쓴다(§0.3 — 빨강은 토큰 하나뿐이다).
 *
 * **문구가 값의 의미를 가른다.** 관측된 공격자 IP와 규칙이 허용한 대역은 서로 다른 사실이라
 * (`threat-path.ts`), `0.0.0.0/0`이 "여기서 공격이 왔다"로 읽히면 안 된다.
 *
 * 대상 이름은 **그 행의 EC2가 아닐 때만** 덧붙인다. 전체 개방 SG의 공격 대상은 EC2가 아니라
 * 부속 칩으로 그려진 그 보안 그룹이라, 화살표만 두면 무엇이 열려 있는지 화면이 말하지 않는다.
 *
 * **상자 폭을 176px로 못 박는다**(`w-44`. 화살표까지 합한 트랙은 194px). 값이 들어가는 폭이라
 * 늘리지도 줄이지도 않는다 — 안쪽 154px은 IPv4 CIDR 최대 길이(`255.255.255.255/32` = 151px)가
 * 들어가는 최소이며, 그보다 좁으면 **출발지 값 자체가 잘린다.** 첫 구현은 `max-w-40`(안쪽
 * 138px)이라 1300px 화면에서도 그 값이 잘렸다(PR #398 리뷰 실측). 계약은 IPv6 CIDR도 허용하므로
 * (`packages/schemas/api/incidents.py`) 그 길이는 말줄임으로 접고 전체 값은 툴팁이 맡는다.
 *
 * **고정 폭이라 이 열은 다른 열과 폭을 다투지 않는다.** 대신 열이 하나 늘어난 만큼 격자가 설
 * 최소 폭이 커지므로 그 판단은 아래 `AssetGraph`의 기준 폭(978px)이 맡는다. 관계 이름·대상을
 * 옆으로 늘어놓지 않고 상자 안에 쌓는 것도 같은 이유다. 계약값 `event_type`은 툴팁과 아래
 * 범례가 맡는다.
 */
function ThreatSource({ threat, ec2Arn }: { threat: RowThreat; ec2Arn: string }) {
  const { path, target } = threat;
  const targetName = target !== null ? (target.name ?? target.resource_id) : arnShort(path.targetArn);

  return (
    <span className="flex items-center gap-1.5">
      <span
        title={`${path.source} → ${targetName} (${path.eventType})`}
        className="border-danger bg-card flex w-44 shrink-0 flex-col items-start gap-0.5 rounded-md border px-2.5 py-1.5"
      >
        <span className="text-danger text-[10px] whitespace-nowrap">
          {path.observed ? '관측 출발지' : '노출 대역'}
        </span>
        <span className="w-full min-w-0 truncate font-mono text-sm font-medium">{path.source}</span>
        {path.targetArn !== ec2Arn ? (
          <span className="text-muted-foreground w-full min-w-0 truncate font-mono text-[10px]">
            → {targetName}
          </span>
        ) : null}
      </span>
      <span aria-hidden className="text-danger text-xs">
        ▶
      </span>
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
  threats,
}: {
  row: TopologyRow;
  onSelect: (asset: AssetItem) => void;
  dimmedArns: ReadonlySet<string> | null;
  /**
   * 이 행으로 들어오는 공격 경로. **null이면 열 자체가 없다** — 빈 배열과 다르다:
   * 빈 배열은 "공격 경로 열이 있는 그래프인데 이 행에는 경로가 없다"이므로 셀을 비워 두고,
   * null은 열이 서지 않은 그래프라 셀을 아예 그리지 않는다(그리면 열이 하나씩 밀린다).
   */
  threats: RowThreat[] | null;
}) {
  const az = azOf(row.ec2);
  // `contents`로 셀을 부모 그리드에 직접 얹는다 — 행마다 따로 그리드를 만들면 열 너비가
  // 행끼리 안 맞아 3계층(TG → EC2 → EBS)이 성립하지 않는다.
  return (
    <div className="contents">
      {threats !== null ? (
        // **경로는 세로로 쌓는다.** 한 행에 경로가 여럿이어도(그 EC2와 그 대에 붙은 SG가 함께
        // 대상인 경우) 열 폭은 상자 하나(194px)에 머문다 — 옆으로 흘리면 경로 수만큼 열이 넓어져
        // 나머지 열이 그만큼 좁아진다.
        //
        // **`min-w-0`을 걸지 않는 것도 의도다.** 걸면 이 셀의 최소 폭이 0이 되어 트랙이 상자보다
        // 좁아지고, 고정 폭 상자가 옆 열 위로 삐져나온다(PR #398 리뷰 실측: 780px에서 트랙이
        // 121px로 줄어 상자가 AZ 열을 덮었다).
        <span className="flex flex-col items-start gap-1.5 self-center">
          {threats.map((threat) => (
            <ThreatSource
              key={`${threat.path.targetArn}:${threat.path.source}`}
              threat={threat}
              ec2Arn={row.ec2.arn}
            />
          ))}
        </span>
      ) : null}
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
 * 자산 그래프. DSH-001 통합 위협 토폴로지의 **외부 출발지 노드와 공격 경로 엣지도 이 컴포넌트가
 * 그린다**(설계서 §8 · #362) — `threatPaths`를 받으면 격자 왼쪽에 열이 하나 더 서고, 경로가 향한
 * 자산을 담은 행에 `[출발지] ▶ [대상]`이 덧붙는다. 별도 컴포넌트로 가르지 않는 이유는 그 엣지의
 * 도착점이 **이 그래프가 이미 배치한 자산 노드**라서다. 따로 그리면 두 배치가 어긋난다.
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
  threatPaths = [],
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
   * 공개 계약 `threat_context`에서 파생한 **외부 공격 경로**(`lib/threat-path`). 있으면 격자 왼쪽에
   * `외부 출발지` 열이 서고, 경로가 향한 자산을 담은 행에 `[출발지] ▶ [대상]`이 덧붙는다.
   *
   * **DSH-001 전용이다** — 자산 화면(AST-001)은 인시던트를 조회하지 않아 넘기지 않는다. 기본값이
   * 빈 배열이라 안 넘기는 호출부는 지금까지처럼 5열로 그린다.
   */
  threatPaths?: readonly ThreatPath[];
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

  // 열은 **경로가 하나라도 있을 때만** 세운다. 늘 세우면 위협이 없는 계정에서 빈 열이 그래프
  // 왼쪽을 차지해, 정작 5열 정렬이 좁은 카드에서 먼저 접힌다.
  const hasThreatColumn = threatPaths.length > 0;
  // 그리지 않은 경로 — 고르지 않은 EC2와 경로 밖 자원(미사용 SG)으로 향한 것이다. 세어서
  // 밝히지 않으면 지금 그려진 선이 전부라고 읽힌다. **두 갈래로 가른다** — 고르면 그려지는 것과
  // 그릴 행이 없는 것의 안내가 서로 다르다(`splitUndrawn`).
  const undrawn = splitUndrawn(hasThreatColumn ? undrawnThreats(threatPaths, rows) : [], allRows);

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
      {/* 공격 경로 열이 서면 트랙이 하나 늘고 **기준 폭도 함께 오른다** — 5열 기준 768px + 출발지
          트랙 194px + 격자 간격 16px = **978px**. 그 아래에서 6열을 세우면 열을 하나 더 끼울 폭이
          없어 어딘가는 잘리는데, 잘릴 자리를 고를 수 없다(PR #398 리뷰 실측: 963px에서 부속 열이
          93px로, 820px에서는 0px로 눌려 SG·NACL 칩이 그래프 밖으로 밀려났다). 그래서 **6열이 못
          서는 폭이면 세우지 않고 세로로 쌓는다** — 접힌 배치는 높지만 아무것도 가리지 않는다.
          실측(1536px 뷰포트 = 그래프 폭 987px)에서 대시보드는 6열을 유지한다.

          마지막 열은 `minmax(0,1fr)`이 아니라 **`minmax(min-content,1fr)`** 이다. 부속 열의 칩은
          관계 이름이 `whitespace-nowrap`이라 줄지 못하는데 `0` 바닥값을 주면 격자가 **줄 수 없는
          이 열부터** 깎아 칩을 그래프 밖으로 밀어낸다. 바닥값을 주면 부족한 폭은 노드 이름 열이
          받고, 그쪽은 폭 상한(`max-w-56`)·말줄임·툴팁이 있어 줄어도 읽을 길이 남는다.

          두 문자열을 통째로 갈아 끼우는 이유는 Tailwind가 **소스에 적힌 클래스 문자열**만 찾아
          만들기 때문이다 — 조각을 이어 붙이면 그 클래스가 빌드에서 사라져 격자가 통째로 무너진다. */}
      <div
        className={cn(
          'grid gap-x-4 gap-y-3',
          hasThreatColumn
            ? '@min-[978px]/graph:grid-cols-[auto_auto_auto_auto_auto_minmax(min-content,1fr)] @min-[978px]/graph:items-start'
            : '@3xl/graph:grid-cols-[auto_auto_auto_auto_minmax(min-content,1fr)] @3xl/graph:items-start',
        )}
      >
        {/* 머리글도 `contents`로 얹어야 아래 행들과 같은 열 트랙을 쓴다. 열이 없는 접힌 배치에서는
            통째로 감춘다 — 그때는 각 행이 `AZ → 진입 → EC2 → 후속 → 부속` 순서로 쌓이므로
            머리글이 첫 행에만 붙은 것처럼 보이게 된다. */}
        <div
          className={
            hasThreatColumn ? 'hidden @min-[978px]/graph:contents' : 'hidden @3xl/graph:contents'
          }
        >
          {(hasThreatColumn ? [THREAT_HEAD, ...HEAD_TYPES] : HEAD_TYPES).map(({ label, types }) => (
            <Head
              key={label}
              label={label}
              failed={uncollected.filter((u) => types.includes(u.asset_type))}
            />
          ))}
        </div>

        {rows.map((row) => (
          <Row
            key={row.ec2.arn}
            row={row}
            onSelect={onSelect}
            dimmedArns={focusedArns}
            threats={hasThreatColumn ? rowThreats(row, threatPaths) : null}
          />
        ))}
      </div>

      {/* 접은 행을 **숨기지 않고 센다** — 몇 대가 화면 밖에 있는지 모르면 이 그래프가 전부라고 읽힌다. */}
      {hiddenRows > 0 ? (
        <p className="text-muted-foreground text-xs">
          위험 신호가 큰 <span className="tabular-nums">{rows.length}</span>대만 그렸습니다 — 나머지{' '}
          <span className="tabular-nums">{hiddenRows}</span>대는 자산 목록에서 전체 보기
        </p>
      ) : null}

      {/* 그리지 않은 공격 경로. 대시보드는 한 번에 EC2 한 대만 그리므로(dashboard-topology.tsx)
          고르지 않은 대로 향한 경로는 선이 없다 — 숨기지 않고 **센다.** 대상 이름까지 적어 두어야
          목록에서 무엇을 골라야 그 선이 보이는지 알 수 있다.

          문구가 `공격 경로`가 아니라 **`외부 출발지 경로`** 인 이유는 이 수에 노출 대역이 함께
          세어지기 때문이다(`topology-picker.tsx`의 같은 판단).

          **두 줄로 가른다**(PR #398 리뷰). 위는 목록에서 고르면 이 그래프에 선이 그려지는 것이고,
          아래는 어떤 EC2에도 붙지 않아 그릴 행 자체가 없는 것이다 — 후자를 누르면 그래프가 바뀌지
          않고 자산 상세(`/assets?asset=…`)로 이동하며, 그 화면은 인시던트를 조회하지 않아 공격
          경로를 그리지 않는다. 한 문장으로 둘을 함께 안내하면 절반이 거짓이 된다. */}
      {undrawn.selectable.length > 0 ? (
        <p className="text-xs text-amber-400">
          이 그래프에 그리지 않은 외부 출발지 경로{' '}
          <span className="tabular-nums">{undrawn.selectable.length}</span>건 —{' '}
          {undrawn.selectable.map((p) => `${p.source} → ${arnShort(p.targetArn)}`).join(' · ')}. 대상
          EC2를 목록에서 고르면 이 그래프에 경로가 그려집니다.
        </p>
      ) : null}
      {undrawn.offPath.length > 0 ? (
        <p className="text-xs text-amber-400">
          그릴 행이 없는 외부 출발지 경로{' '}
          <span className="tabular-nums">{undrawn.offPath.length}</span>건 —{' '}
          {undrawn.offPath.map((p) => `${p.source} → ${arnShort(p.targetArn)}`).join(' · ')}. 어떤
          EC2에도 연결되지 않은 자원이라 그래프에 그릴 자리가 없습니다 — 목록에서 누르면 자산
          상세로 열리며, 그 화면에는 이 경로를 그리지 않습니다.
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
        {hasThreatColumn ? (
          <>
            {' '}
            외부 출발지는 인시던트 계약의 <code>threat_context</code>에서 옵니다 —{' '}
            <strong>관측 출발지</strong>는 SSH 시도에서 실제로 관측된 IP(<code>SSH_BRUTE_FORCE</code>),{' '}
            <strong>노출 대역</strong>은 보안 그룹 규칙이 허용한 범위(<code>OPEN_IP</code>)이며 공격자
            IP가 아닙니다.
          </>
        ) : null}
      </p>
    </div>
  );
}
