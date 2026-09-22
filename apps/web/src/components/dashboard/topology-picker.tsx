'use client';

// DSH-001 토폴로지 카드의 **오른쪽 두 칸** — 왼쪽이 인스턴스 선택, 오른쪽이 트래픽 경로 밖이다.
// 카드는 `그래프 │ 인스턴스 │ 경로 밖` 세 칸이 나란히 선 구조다.
//
// 그래프는 **한 번에 EC2 한 대**를 그린다. 전수를 한 화면에 그리면 자산이 늘 때마다 카드가
// 페이지를 덮고, 정작 한 대의 경로(대상 그룹 → EC2 → EBS)와 보호 계층은 더 안 보인다.
// 그래서 "무엇을 그릴지"를 인스턴스 칸이 정한다 — 갈래는 **EC2 인스턴스** 하나뿐이다.
//
// 고르기 전에 알아야 할 것은 "이 대에 볼 것이 있나"이므로 항목마다 **조치 대상 판정을 배지로**
// 단다. 그 판정은 EC2 자신의 것만이 아니라 **붙은 자원의 것까지** 합친 값이다(`rowVerdicts`) —
// 전체 개방 SG가 달린 EC2는 스스로는 `제외`여도 지금 볼 대상이다.
//
// **두 칸을 가른 이유**: 왼쪽은 고를 수 있는 것, 오른쪽은 **어느 EC2에도 걸리지 않아 그래프에
// 그릴 자리가 없는 것**이다. 한 목록에 이어 붙이면 경로 밖 자원도 고를 수 있는 것처럼 읽힌다.

import { StatusBadge } from '@/components/status-badge';
import { groupOrphansByType, rowVerdicts, type TopologyRow } from '@/lib/asset-graph';
import { ASSET_TYPE_LABELS } from '@/lib/enum-labels';
import { assetThreats, rowThreats, type ThreatPath } from '@/lib/threat-path';
import { cn } from '@/lib/utils';
import type { AssetItem } from '@/types/api';

/** 배지를 줄 높이 안에 눕히는 치수. 사전(§3.2)의 문구·색은 그대로 쓴다. */
const BADGE = 'shrink-0 px-1 py-0 text-[10px] font-normal';

/**
 * 이 자산으로 들어오는 **외부 출발지 경로** 수. **판정 배지가 아니다** — 판정은 규칙 엔진이
 * 자산에 내린 것이고 이것은 인시던트 계약(`threat_context`)에서 온 경로 수라, 사전(§3.2)의
 * 배지 어휘를 빌리지 않는다. 색은 `THREAT`와 같은 `--danger` 하나만 쓴다(§0.3).
 *
 * 그래프는 한 번에 EC2 한 대만 그리므로 **고르기 전에는 그 대에 들어오는 경로가 보이지 않는다.**
 * 이 표시가 그 자리를 메운다 — 어느 줄을 골라야 그 경로가 그려지는지 목록이 말해 준다.
 *
 * **`공격 N`이라고 적지 않는다**(PR #398 리뷰). 이 수에는 실제로 **관측된** SSH 시도와, 보안 그룹
 * 규칙이 **허용만 한** 대역(`OPEN_IP`의 `exposed_cidr`)이 함께 들어 있다 — `0.0.0.0/0`은 전부
 * 열렸다는 뜻이지 누가 들어왔다는 뜻이 아니어서, `공격`으로 적으면 목록이 계약에 없는 사실을
 * 주장한다. 그래서 둘을 함께 덮는 말로 두고, 문구는 그래프 열 이름(`외부 출발지 (인터넷)`)과
 * 같은 어휘를 쓴다.
 *
 * **관측·노출 구분은 툴팁이 맡는다.** 이 목록 칸은 240px뿐이고 줄에는 이름과 판정 배지가 함께
 * 서므로, 문구를 늘리거나 유형별로 칸을 나누면 그만큼 **이름이 잘린다**(실측: 같은 줄에서 이름이
 * `위협 경로 N` 한 칸은 52px, `관측 N`·`노출 N` 두 칸은 30px, 지금 형태는 73px). 대신 그래프
 * 노드는 두 의미를 문구로 갈라 그리므로, 고른 뒤에는 화면이 그 차이를 말한다.
 */
function ThreatMark({ paths }: { paths: readonly ThreatPath[] }) {
  if (paths.length === 0) return null;
  return (
    <span
      className="text-danger border-danger shrink-0 rounded border px-1 text-[10px] whitespace-nowrap"
      title={`외부 출발지 ${paths.length}건 — ${paths
        .map((p) => `${p.observed ? '관측 출발지' : '노출 대역'} ${p.source} (${p.eventType})`)
        .join(' · ')}`}
    >
      외부 <span className="tabular-nums">{paths.length}</span>
    </span>
  );
}

/** 좁으면 그래프 아래로 내려간다. 옆에 설 때만 세로선으로 가른다 — 접힌 배치에서 세로선은 여백만 먹는다. */
const COLUMN =
  'border-border flex min-h-0 min-w-0 flex-col gap-2 border-t pt-3 @5xl/topology:border-t-0 @5xl/topology:border-l @5xl/topology:pt-0 @5xl/topology:pl-3';

export function TopologyPicker({
  rows,
  orphans,
  selectedArn,
  onSelect,
  onOpen,
  threatPaths = [],
}: {
  /** 위험 순으로 이미 정렬된 EC2 행 목록(`sortRowsByRisk`). */
  rows: readonly TopologyRow[];
  /** 트래픽 경로 밖 자원 — 어떤 EC2에서도 닿지 않아 그래프에 그릴 자리가 없다. */
  orphans: readonly AssetItem[];
  selectedArn: string;
  onSelect: (arn: string) => void;
  onOpen: (asset: AssetItem) => void;
  /** 외부 공격 경로(`lib/threat-path`). 목록 항목마다 몇 건이 들어오는지 표시한다. */
  threatPaths?: readonly ThreatPath[];
}) {
  return (
    <>
      {/* 인스턴스 — 폭을 고정한다. 240px은 `이름 + 판정 배지`가 한 줄에 서는 최소 폭이다(실측:
          가장 긴 시연 이름 + `최적화 후보` = 221px). 카드가 전폭이라 이만큼 줘도 그래프는
          1300px 넘게 남는다 — 5열 정렬(768px)에 한참 여유가 있다.

          **경로 밖 칸과 조건을 나눈다.** EC2가 0대면 고를 것이 없어 이 칸은 빠지지만, 그때도
          미연결 EBS·미사용 SG는 남아 있어야 한다 — 그것까지 함께 숨기면 EC2가 없는 계정은
          비용만 내는 자원을 홈에서 영영 못 본다. */}
      {rows.length > 0 ? (
        <div className={cn(COLUMN, '@5xl/topology:w-60 shrink-0')}>
          <p className="text-muted-foreground text-xs">
            EC2 <span className="tabular-nums">{rows.length}</span>대
          </p>

          {/* **목록만 스크롤한다.** 옆에 설 때는 칸 높이를 채우고(`flex-1`) 그 안에서만 넘친다 —
              자산이 늘어도 카드가 자라지 않는다. 세로로 쌓인 배치에는 채울 높이가 없으므로
              고정 상한(`max-h-60`)으로 같은 일을 한다. */}
          <ul className="flex max-h-60 min-h-0 flex-col overflow-y-auto @5xl/topology:max-h-none @5xl/topology:flex-1">
            {rows.map((row) => {
              const selected = row.ec2.arn === selectedArn;
              return (
                <li key={row.ec2.arn}>
                  <button
                    type="button"
                    onClick={() => onSelect(row.ec2.arn)}
                    aria-pressed={selected}
                    title={row.ec2.resource_id}
                    className={cn(
                      // `min-h-8` — 배지가 붙은 줄만 4px 높아지면 짧은 목록에서 줄 간격이 들쭉날쭉해진다.
                      'flex min-h-8 w-full min-w-0 items-center gap-1.5 rounded px-1.5 py-1 text-left',
                      selected ? 'bg-muted' : 'hover:bg-muted/50',
                    )}
                  >
                    <span
                      className={cn(
                        'min-w-0 flex-1 truncate text-xs',
                        selected ? 'font-medium' : 'text-muted-foreground',
                      )}
                    >
                      {row.ec2.name ?? row.ec2.resource_id}
                    </span>
                    <ThreatMark paths={rowThreats(row, threatPaths).map((t) => t.path)} />
                    {rowVerdicts(row).map((verdict) => (
                      <StatusBadge key={verdict} field="verdict" value={verdict} className={BADGE} />
                    ))}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {/* 경로 밖 — 자산 화면은 같은 목록을 가로 한 줄(`유형 │ 건수 │ 이름들`)로 그리지만, 이 칸은
          224px뿐이라 그 형식으로는 이름 시작점도 못 잡는다. 유형 머리줄 아래 이름을 세로로 쌓는다.
          묶고 정렬하는 일은 두 화면이 `groupOrphansByType` 하나를 공유한다. */}
      {orphans.length > 0 ? (
        <div className={cn(COLUMN, '@5xl/topology:w-56 shrink-0')}>
          {/* 한 줄로 줄여 목록 창을 그만큼 넓힌다 — 두 줄이면 5건짜리 목록이 벌써 스크롤된다.
              덜어낸 설명("어떤 EC2와도 연결이 없다")은 툴팁으로 남긴다. */}
          <p
            className="text-muted-foreground truncate text-xs"
            title="어떤 EC2와도 연결이 없는 자원입니다. 트래픽을 받지 않고 비용만 발생합니다."
          >
            트래픽 경로 밖 <span className="tabular-nums">{orphans.length}</span>건 · 비용만 발생
          </p>
          <ul className="flex max-h-60 min-h-0 flex-col gap-1 overflow-y-auto @5xl/topology:max-h-none @5xl/topology:flex-1">
            {groupOrphansByType(orphans).map(({ type, assets }) => (
              <li key={type} className="flex min-w-0 flex-col">
                <span className="text-muted-foreground flex items-baseline justify-between gap-2 text-[10px]">
                  <span>{ASSET_TYPE_LABELS[type]?.label ?? type}</span>
                  <span className="tabular-nums">{assets.length}</span>
                </span>
                {assets.map((asset) => (
                  <button
                    key={asset.arn}
                    type="button"
                    onClick={() => onOpen(asset)}
                    title={asset.resource_id}
                    className="text-muted-foreground hover:text-foreground hover:bg-muted/50 flex w-full min-w-0 items-center gap-1.5 rounded px-1.5 py-0.5 text-left"
                  >
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
                      {asset.name ?? asset.resource_id}
                    </span>
                    {/* 경로 밖 자원도 공격 대상이 된다 — 어떤 EC2에도 안 붙은 전체 개방 SG가 그
                        자리다. 그래프에 그릴 행이 없어 선이 없으므로 여기서만 알릴 수 있다. */}
                    <ThreatMark paths={assetThreats(asset.arn, threatPaths)} />
                    {/* 판정이 붙은 자원(미사용 EBS 등)은 배지를 남긴다 — 이 목록에서 조치할 것이다. */}
                    {asset.verdict !== null ? (
                      <StatusBadge field="verdict" value={asset.verdict} className={BADGE} />
                    ) : null}
                  </button>
                ))}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}
