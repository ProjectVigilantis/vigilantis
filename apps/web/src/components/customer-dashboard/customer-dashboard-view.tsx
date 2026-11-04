// 고객사 대시보드(고객사 계정의 `/`) 본문. 첫 화면은 가독성이 먼저다. "지금 괜찮은가 → 흐름과 현황은 어떤가 → 무엇을 해야 하나 →
// 무엇이 끝났나" 순서로 네 덩어리만 둔다. 목록은 5건에서 끊고
// 나머지와 자세한 근거(AI 요약 · 가드레일 · 토폴로지 · CPU)는 다음 화면(자산 · 보안 · 인시던트 상세)으로 넘긴다.
//
// 근거: 유사 서비스(Datadog Security Inbox · AWS Security Hub 요약 · Prisma Cloud Command Center)가 모두
// 상태 → 급한 목록 상위 N건(+전체 보기) → 심각도별 건수 → 추이 순서를 쓰고, 추이를 할 일과 떼어 둔다.
// 원 · 도넛 그래프는 쓰지 않는다(크기는 막대 길이로). DESIGN.md §데이터 시각화.
//
// 훅이 없는 서버 컴포넌트다. 실시간 이벤트가 오면 `router.refresh()`가 다시 그린다.

import {
  ArrowRight,
  Bell,
} from 'lucide-react';
import Link from 'next/link';

import { TIP_ANCHOR, TipBox, tipAlign } from '@/components/admin-dashboard/chart-parts';
import { AutoRefresh } from '@/components/auto-refresh';
import { ChartCard } from '@/components/chart-card';
import { DismissibleBanner } from '@/components/customer-dashboard/dismissible-banner';
import { InventoryTrendSection } from '@/components/customer-dashboard/inventory-trend-section';
import { ThreatEventsCard } from '@/components/customer-dashboard/threat-events-card';
import { StatGrid, StatTile } from '@/components/stat-tile';
import {
  assetTypeRows,
  awaitingLinks,
  postureOf,
  savingsTotal,
  timeAgo,
  type Posture,
} from '@/lib/customer-dashboard';
import {
  COST_BUCKET_LABELS,
  COST_COLOR,
  costOfType,
  costPeriodLabel,
  costSlices,
  costTotal,
  type CostLoad,
} from '@/lib/asset-costs';
import { SLICE_COLOR, sliceLabel } from '@/lib/asset-slices';
import { assetComposition, type CompositionKey, type ProposalRow } from '@/lib/dashboard';
import {
  ASSET_TYPE_LABELS,
  NO_VALUE,
} from '@/lib/enum-labels';
import { formatUsd } from '@/lib/savings';
import { cn, formatKst } from '@/lib/utils';
import type {
  AssetInventoryAxis,
  AssetsResponse,
  AssetType,
  CollectionStatus,
  MetricsTimeseriesResponse,
} from '@/types/api';

const POSTURE_STYLE: Record<Posture, { box: string; iconClass: string }> = {
  critical: { box: 'border-badge-critical-fg/30 bg-badge-critical-bg', iconClass: 'text-badge-critical-fg' },
  warning: { box: 'border-badge-warning-fg/30 bg-badge-warning-bg', iconClass: 'text-badge-warning-fg' },
  good: { box: 'border-badge-good-fg/30 bg-badge-good-bg', iconClass: 'text-badge-good-fg' },
  unknown: { box: 'border-status-idle/50 bg-badge-idle-bg', iconClass: 'text-badge-idle-fg' },
};

/**
 * 숫자 카드 설명줄의 수집 상태. 배지 사전(COLLECTION_STATUS_LABELS)은 정상(READY)을 배지로 그리지 않으려고
 * 비워 두므로, 문장 안에 넣을 말은 여기서 따로 정한다.
 */
const COLLECTION_TEXT: Record<CollectionStatus, string> = {
  NOT_COLLECTED: '아직 수집 전',
  COLLECTING: '수집 중',
  READY: '수집 정상',
  PARTIAL: '일부 수집 실패',
  FAILED: '수집 실패',
};

export function CustomerDashboardView({
  assets,
  rows,
  metrics,
  costs,
  renderedAt,
}: {
  assets: AssetsResponse;
  /** 처리할 일(보안 위험도순 → 비용 절감액순). 인시던트 조회 실패면 null. */
  rows: ProposalRow[] | null;
  metrics: MetricsTimeseriesResponse | null;
  /** 자산 유형별 최근 30일 비용(FE 제안 계약, lib/asset-costs.ts). */
  costs: CostLoad;
  renderedAt: string;
}) {
  const nowMs = Date.parse(renderedAt);
  const queue = rows === null ? null : rows.map((r) => r.incident);
  const threatAssets = assets.items.filter((a) => a.verdict === 'THREAT').length;
  const posture = postureOf({ queue, threatAssets, collection: assets.collection_status });
  const style = POSTURE_STYLE[posture.level];

  const awaiting = queue?.filter((i) => i.status === 'AWAITING_APPROVAL') ?? null;
  const secops = queue?.filter((i) => i.category === 'SECOPS') ?? null;
  const risk = { HIGH: 0, MEDIUM: 0, LOW: 0 };
  for (const i of secops ?? []) if (i.initial_risk_level !== null) risk[i.initial_risk_level] += 1;
  const savings = rows === null ? null : savingsTotal(rows);
  // AI 추천 조치는 축마다 그 건이 모두 보이는 관제 목록으로 간다(lib/customer-dashboard.ts `awaitingLinks`). 두 축에 다 있으면
  // 카드 전체를 한 곳에 걸 수 없어 설명줄의 축별 건수를 각각 링크로 둔다.
  const aiLinks = queue === null ? [] : awaitingLinks(queue);
  const AI_EMPTY_HREF = '/monitoring?tab=incidents&preset=pending';
  const aiHref = aiLinks.length === 0 ? AI_EMPTY_HREF : aiLinks.length === 1 ? aiLinks[0].href : undefined;
  // 상태 알림의 [보기]: 승인 대기가 있으면 그 목록(보안 먼저), 없으면 진행 중 목록이다.
  const bannerHref =
    aiLinks[0]?.href ?? (secops !== null && secops.length > 0 ? '/monitoring?axis=security&tab=incidents' : '/monitoring?tab=incidents');
  // 위협 이벤트 대상 ARN을 이름으로 읽는다. 이름 없는 자산은 리소스 ID로.
  const assetNames = Object.fromEntries(assets.items.map((a) => [a.arn, a.name ?? a.resource_id]));

  const accounts = [...new Set(assets.items.map((a) => a.account_id))];
  const regions = [...new Set(assets.items.map((a) => a.region))];
  const collection = COLLECTION_TEXT[assets.collection_status];
  const collectedAgo = assets.last_collected_at === null ? '수집 기록 없음' : timeAgo(assets.last_collected_at, nowMs);

  return (
    <div className="flex flex-col gap-6">
      {/* 1. 지금 괜찮은가: 제목 옆 작은 알림(2026-11-03). 자세한 문장과 계정 · 리전은 알림에 마우스를 올리면 보인다. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <h1 className="text-2xl font-bold tracking-tight">대시보드</h1>
          <DismissibleBanner
            dismissKey={`${posture.level}:${posture.headline}:${posture.detail}`}
            label="관제 상태"
            className={cn('inline-flex max-w-full items-center gap-2 rounded-full border py-1 pr-1.5 pl-3 text-[13px]', style.box)}
          >
            <span className={cn(TIP_ANCHOR, 'inline-flex min-w-0 items-center gap-1.5')}>
              <Bell aria-hidden className={cn('size-4 shrink-0', style.iconClass)} />
              <span className="truncate font-semibold">{posture.headline}</span>
              <TipBox align="start">
                <span>{posture.detail}</span>
                <span className="text-muted-foreground">
                  AWS 계정 {accounts.length === 0 ? NO_VALUE : accounts.join(', ')} · {regions.length === 0 ? '리전 없음' : regions.join(', ')} · 마지막
                  수집 {collectedAgo}
                </span>
              </TipBox>
            </span>
            {queue !== null && queue.length > 0 ? (
              <Link href={bannerHref} className="text-primary inline-flex shrink-0 items-center gap-0.5 font-semibold hover:underline">
                보기
                <ArrowRight aria-hidden className="size-3.5" />
              </Link>
            ) : null}
          </DismissibleBanner>
        </div>
        <AutoRefresh renderedAt={renderedAt} />
      </div>

      {/* 2. 숫자 네 개. 누르면 그 숫자의 목록으로 간다. AI 추천 조치는 승인을 기다리는 AI 제안 건이다(계약상 승인 대기 = 제안 1개 이상). */}
      <StatGrid>
        <StatTile
          label="관제 중 자산"
          href="/monitoring"
          value={assets.items.length}
          tone={assets.collection_status === 'FAILED' ? 'critical' : assets.collection_status === 'PARTIAL' ? 'warning' : undefined}
          note={`${collection} · ${collectedAgo}`}
        />
        <StatTile
          label="월 절감 가능액"
          href="/monitoring?tab=incidents"
          value={savings === null ? NO_VALUE : formatUsd(savings.amount)}
          note={
            savings === null
              ? '인시던트를 불러오지 못했습니다'
              : `비용 제안 ${savings.counted + savings.unknown}건${savings.unknown > 0 ? ` · 추정 없음 ${savings.unknown}건` : ''}`
          }
        />
        <StatTile
          label="처리 중인 보안 위협"
          href="/monitoring?axis=security&tab=incidents"
          value={secops === null ? NO_VALUE : secops.length}
          tone={risk.HIGH > 0 ? 'critical' : secops !== null && secops.length > 0 ? 'serious' : undefined}
          note={`위험도 높음 ${risk.HIGH} · 중간 ${risk.MEDIUM} · 낮음 ${risk.LOW}`}
        />
        <StatTile
          label="AI 추천 조치"
          href={aiHref}
          value={awaiting === null ? NO_VALUE : awaiting.length}
          tone={awaiting?.some((i) => i.category === 'SECOPS') ? 'critical' : undefined}
          note={
            awaiting === null ? (
              '인시던트를 불러오지 못했습니다'
            ) : aiHref === undefined ? (
              // 카드가 링크가 아닐 때만 안쪽 링크를 둔다(링크 안의 링크는 HTML이 허용하지 않는다).
              <>
                {aiLinks.map((l, n) => (
                  <span key={l.axis}>
                    {n > 0 ? ' · ' : null}
                    <Link href={l.href} className="text-primary font-semibold hover:underline">
                      {l.axis === 'security' ? '보안' : '자산'} {l.count}건
                    </Link>
                  </span>
                ))}
              </>
            ) : (
              `보안 ${awaiting.filter((i) => i.category === 'SECOPS').length}건 · 자산 ${awaiting.filter((i) => i.category === 'FINOPS').length}건`
            )
          }
        />
      </StatGrid>

      {/* 3. 흐름: 왼쪽은 자산 현황(위 추이 · 아래 판정과 유형), 오른쪽은 위협 이벤트(2026-11-03 PM 지시로 숫자 바로 아래). */}
      <div className="flex flex-wrap gap-4">
        <AssetStatusCard assets={assets} nowMs={nowMs} inventory={metrics?.asset_inventory ?? null} costs={costs} className="flex-[1_1_480px]" />
        <ThreatEventsCard assetNames={assetNames} className="flex-[1_1_480px]" />
      </div>
    </div>
  );
}

/**
 * 자산 현황(2026-11-03): 위 구역은 유형별 자산 추이, 아래 구역은 유형별 최근 30일 비용 막대와 유형별 요약(개수 · 금액 · 상태)이다.
 * 비용(Cost Explorer)을 못 읽으면 막대 · 요약을 자산 개수 비율로 그린다.
 */
function AssetStatusCard({
  assets,
  nowMs,
  inventory,
  costs,
  className,
}: {
  assets: AssetsResponse;
  nowMs: number;
  costs: CostLoad;
  /** 위 구역 추이의 첫 값(서버 기본 창 72시간). 시계열 조회 실패면 null. */
  inventory: AssetInventoryAxis | null;
  className?: string;
}) {
  const rows = assetTypeRows(assets.items, assets.uncollected);
  // 비율 막대는 위 추이 그래프와 같은 조각 · 색이다(EC2 · 보안 그룹 · EBS · 판정 비대상). 같은 카드 안에서 색이 갈리지 않게 한다.
  const composition = assetComposition(assets.items, assets.uncollected);
  const sliceOf = (type: AssetType): CompositionKey => (composition.slices.some((sl) => sl.key === type) ? (type as CompositionKey) : 'OTHER');
  const pct = (n: number) => (assets.items.length === 0 ? 0 : Math.round((n / assets.items.length) * 100));

  // 비용(Cost Explorer 최근 30일)을 읽었으면 막대 · 요약을 금액으로 그리고, 못 읽었으면 개수 비율로 그린 뒤 그 사유를 한 줄 적는다.
  const ready = costs.kind === 'ready' && costs.data.status === 'READY' ? costs.data : null;
  const costSlicesNow = ready === null ? [] : costSlices(ready);
  const readyTotal = ready === null ? null : costTotal(ready);
  const costView =
    ready !== null && costSlicesNow.length > 0 && readyTotal !== null
      ? { data: ready, total: readyTotal, other: costSlicesNow.find((c) => c.bucket === 'OTHER')?.amount ?? 0 }
      : null;
  const segments =
    costView === null
      ? composition.slices.map((sl) => ({
          key: sl.key,
          color: SLICE_COLOR[sl.key],
          ratio: sl.ratio,
          aria: `${sliceLabel(sl.key)} ${sl.count}개 · ${pct(sl.count)}%`,
          sub: sl.members.length > 0 ? sl.members.map((m) => `${ASSET_TYPE_LABELS[m.type]?.label ?? m.type} ${m.count}`).join(' · ') : null,
        }))
      : costSlicesNow.map((c) => ({
          key: c.bucket,
          color: COST_COLOR[c.bucket],
          ratio: c.ratio,
          aria: `${COST_BUCKET_LABELS[c.bucket]} ${formatUsd(c.amount)} · ${Math.round(c.ratio * 100)}%`,
          sub: null,
        }));
  const dotColor = (type: AssetType) =>
    costView === null
      ? SLICE_COLOR[sliceOf(type)]
      : type === 'EC2' || type === 'EBS' || type === 'ALB_TARGET_GROUP'
        ? COST_COLOR[type]
        : 'var(--border)';
  // 요약 줄. 비용을 읽었으면 금액 내림차순(과금 없음은 0, 못 읽은 금액은 맨 뒤), 아니면 개수 내림차순. 같으면 개수 · 이름순.
  const summary = [
    ...rows.map((r) => ({
      key: r.type as string,
      label: ASSET_TYPE_LABELS[r.type]?.label ?? r.type,
      color: dotColor(r.type),
      count: r.uncollectedReason === null ? r.count : null,
      cost: costView === null ? null : costOfType(costView.data, r.type),
      detail: r.uncollectedReason === null ? r.detail : `수집 실패 · ${r.uncollectedReason}`,
    })),
    ...(costView !== null && costView.other > 0
      ? [{ key: 'OTHER', label: COST_BUCKET_LABELS.OTHER, color: COST_COLOR.OTHER, count: null, cost: costView.other, detail: '자산 유형에 묶이지 않는 요금' }]
      : []),
  ].sort((x, y) => {
    const money = (c: number | 'FREE' | null) => (c === 'FREE' ? 0 : c === null ? -1 : c);
    const byCost = costView === null ? 0 : money(y.cost) - money(x.cost);
    return byCost || (y.count ?? -1) - (x.count ?? -1) || x.label.localeCompare(y.label, 'ko');
  });
  const costNote =
    costView !== null
      ? `Cost Explorer 기준 · ${costPeriodLabel(costView.data)} · ${timeAgo(costView.data.retrieved_at, nowMs)} 받음`
      : costs.kind === 'missing'
        ? '비용 API가 서버에 아직 없습니다.'
        : costs.kind === 'failed'
          ? '비용을 불러오지 못했습니다.'
          : `비용을 읽지 못했습니다${costs.data.reason_code ? `(${costs.data.reason_code})` : ''}.`;

  return (
    <ChartCard
      className={cn('gap-3', className)}
      title="자산 현황"
      description={`마지막 수집 ${assets.last_collected_at === null ? '기록 없음' : `${formatKst(assets.last_collected_at)} (${timeAgo(assets.last_collected_at, nowMs)})`}`}
      aside={
        <span className="text-foreground-secondary text-[13px] whitespace-nowrap">
          <strong className="text-foreground text-base">{assets.items.length}</strong>개
        </span>
      }
    >
      <InventoryTrendSection
        initial={inventory}
        costs={
          costView === null
            ? null
            : { total: costView.total, byType: Object.fromEntries(rows.map((r) => [r.type, costOfType(costView.data, r.type) ?? undefined])) }
        }
      />
      {composition.total === 0 || rows.length === 0 ? (
        <p className="text-muted-foreground border-t pt-3 text-sm">수집된 자산이 없습니다.</p>
      ) : (
        <div className="flex flex-col gap-2.5 border-t pt-3">
          {/* 막대는 제목 줄 오른쪽에 24px 띄워 둔다(최대 440px). 정확한 값은 아래 요약이 적는다. */}
          <div className="flex items-center justify-between gap-6 pr-6">
            <p className="text-muted-foreground shrink-0 text-xs font-semibold">
              {costView === null ? (
                '유형별 비율'
              ) : (
                <>
                  최근 30일 비용 <strong className="text-foreground text-sm">{formatUsd(costView.total)}</strong>
                </>
              )}
            </p>
            <div role="img" aria-label={segments.map((g) => g.aria).join(', ')} className="flex h-3 w-full max-w-110 gap-0.5">
              {segments.map((g, i) => (
                <div
                  key={g.key}
                  className={cn(TIP_ANCHOR, i === 0 && 'rounded-l-[3px]', i === segments.length - 1 && 'rounded-r-[3px]')}
                  style={{ width: `${g.ratio * 100}%`, backgroundColor: g.color }}
                >
                  <TipBox align={tipAlign(i, segments.length)}>
                    <span className="font-medium">{g.aria}</span>
                    {g.sub ? <span className="text-muted-foreground">{g.sub}</span> : null}
                  </TipBox>
                </div>
              ))}
            </div>
          </div>
          {/* 유형별 요약. 비용을 읽었으면 개수 · 금액, 못 읽었으면 개수 · 비율이다. 점 색은 위 막대의 조각 색이다.
              큰 것부터(비용, 없으면 개수) 왼쪽 열 위에서 아래로, 이어서 오른쪽 열로 채운다(CSS columns). */}
          <ul className="columns-1 gap-x-6 text-[13px] sm:columns-2">
            {summary.map((it) => (
              <li key={it.key} className="mb-2 flex min-w-0 break-inside-avoid flex-col">
                <span className="flex items-center gap-1.5">
                  <span aria-hidden className="size-2.5 shrink-0 rounded-[3px]" style={{ backgroundColor: it.color }} />
                  <span className="truncate">{it.label}</span>
                  {costView === null ? (
                    <>
                      <strong className="ml-auto tabular-nums">{it.count ?? NO_VALUE}</strong>
                      <span className="text-muted-foreground w-9 text-right tabular-nums">{it.count === null ? '' : `${pct(it.count)}%`}</span>
                    </>
                  ) : (
                    <>
                      <span className="text-muted-foreground ml-auto tabular-nums">{it.count === null ? '' : `${it.count}개`}</span>
                      <strong className="w-18 text-right tabular-nums">
                        {it.cost === 'FREE' ? <span className="text-muted-foreground font-normal">과금 없음</span> : it.cost === null ? NO_VALUE : formatUsd(it.cost)}
                      </strong>
                    </>
                  )}
                </span>
                <span className="text-muted-foreground truncate pl-4 text-xs">{it.detail}</span>
              </li>
            ))}
          </ul>
          {costNote ? <p className="text-muted-foreground text-xs">{costNote}</p> : null}
        </div>
      )}
    </ChartCard>
  );
}
