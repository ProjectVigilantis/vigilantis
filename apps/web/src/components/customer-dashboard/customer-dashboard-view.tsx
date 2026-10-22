// 고객사 대시보드(미리보기) 본문. 첫 화면은 가독성이 먼저다. "지금 괜찮은가 → 무엇을 해야 하나 →
// 무엇이 끝났나 → 흐름은 어떤가 → 어디까지 보고 있나" 순서로 다섯 덩어리만 둔다. 목록은 5건에서 끊고
// 나머지와 자세한 근거(AI 요약 · 가드레일 · 토폴로지 · CPU)는 다음 화면(자산 · 보안 · 인시던트 상세)으로 넘긴다.
//
// 근거: 유사 서비스(Datadog Security Inbox · AWS Security Hub 요약 · Prisma Cloud Command Center)가 모두
// 상태 → 급한 목록 상위 N건(+전체 보기) → 심각도별 건수 → 추이 순서를 쓰고, 추이를 할 일과 떼어 둔다.
// 원 · 도넛 그래프는 쓰지 않는다(크기는 막대 길이로). DESIGN.md §데이터 시각화.
//
// 훅이 없는 서버 컴포넌트다. 실시간 이벤트가 오면 `router.refresh()`가 다시 그린다.

import {
  ArrowRight,
  CircleCheck,
  CircleDot,
  CircleHelp,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import Link from 'next/link';

import { ChartCard } from '@/components/chart-card';
import { StatGrid, StatTile } from '@/components/stat-tile';
import { EnumBadge } from '@/components/status-badge';
import {
  coverageCounts,
  doneNote,
  postureOf,
  recentDone,
  savingsTotal,
  threatHours,
  timeAgo,
  type Posture,
} from '@/lib/customer-dashboard';
import type { ProposalRow } from '@/lib/dashboard';
import {
  ASSET_TYPE_LABELS,
  CATEGORY_LABELS,
  INCIDENT_STATUS_LABELS,
  NO_VALUE,
  incidentTitle,
  RISK_LEVEL_LABELS,
  type EnumLabel,
} from '@/lib/enum-labels';
import { formatUsd } from '@/lib/savings';
import { statusFill } from '@/lib/status-fill';
import { cn, formatKst } from '@/lib/utils';
import type {
  AssetsResponse,
  AssetType,
  CollectionStatus,
  IncidentListItem,
  MetricsTimeseriesResponse,
} from '@/types/api';

const QUEUE_LIMIT = 5;

const POSTURE_STYLE: Record<Posture, { box: string; icon: LucideIcon; iconClass: string }> = {
  critical: { box: 'border-badge-critical-fg/30 bg-badge-critical-bg', icon: ShieldAlert, iconClass: 'text-badge-critical-fg' },
  warning: { box: 'border-badge-warning-fg/30 bg-badge-warning-bg', icon: TriangleAlert, iconClass: 'text-badge-warning-fg' },
  good: { box: 'border-badge-good-fg/30 bg-badge-good-bg', icon: ShieldCheck, iconClass: 'text-badge-good-fg' },
  unknown: { box: 'border-status-idle/50 bg-badge-idle-bg', icon: CircleHelp, iconClass: 'text-badge-idle-fg' },
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
  incidents,
  rows,
  incidentsErrorSlot,
  metrics,
  renderedAt,
}: {
  assets: AssetsResponse;
  incidents: IncidentListItem[] | null;
  /** 처리할 일(보안 위험도순 → 비용 절감액순). 인시던트 조회 실패면 null. */
  rows: ProposalRow[] | null;
  incidentsErrorSlot: React.ReactNode;
  metrics: MetricsTimeseriesResponse | null;
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
  const done = recentDone(incidents, nowMs);
  const hours = metrics === null ? null : threatHours(metrics.threat_events, metrics.generated_at);

  const accounts = [...new Set(assets.items.map((a) => a.account_id))];
  const regions = [...new Set(assets.items.map((a) => a.region))];
  const collection = COLLECTION_TEXT[assets.collection_status];
  const collectedAgo = assets.last_collected_at === null ? '수집 기록 없음' : timeAgo(assets.last_collected_at, nowMs);

  return (
    <div className="flex flex-col gap-6">
      {/* 1. 지금 괜찮은가 */}
      <section
        aria-label="관제 상태"
        className={cn('flex flex-col gap-4 rounded-2xl border p-6 sm:flex-row sm:items-center sm:justify-between', style.box)}
      >
        <div className="flex items-start gap-4">
          <style.icon aria-hidden className={cn('mt-0.5 size-9 shrink-0', style.iconClass)} />
          <div className="flex flex-col gap-1">
            <p className="text-2xl font-bold tracking-tight">{posture.headline}</p>
            <p className="text-foreground-secondary text-[15px]">{posture.detail}</p>
            <p className="text-muted-foreground text-[13px]">
              AWS 계정 {accounts.length === 0 ? NO_VALUE : accounts.join(', ')} · {regions.length === 0 ? '리전 없음' : regions.join(', ')} ·
              마지막 수집 {collectedAgo}
            </p>
          </div>
        </div>
        {queue !== null && queue.length > 0 ? (
          <a
            href="#todo"
            className="bg-primary inline-flex h-11 shrink-0 items-center gap-2 self-start rounded-md px-5 text-[15px] font-semibold text-white hover:bg-primary/90 sm:self-center"
          >
            처리할 일 보기
            <ArrowRight aria-hidden className="size-4" />
          </a>
        ) : null}
      </section>

      {/* 2. 숫자 네 개 */}
      <StatGrid>
        <StatTile
          label="승인 대기 조치"
          value={awaiting === null ? NO_VALUE : awaiting.length}
          tone={awaiting?.some((i) => i.category === 'SECOPS') ? 'critical' : undefined}
          note={
            awaiting === null
              ? '인시던트를 불러오지 못했습니다'
              : `보안 ${awaiting.filter((i) => i.category === 'SECOPS').length} · 비용 ${awaiting.filter((i) => i.category === 'FINOPS').length}`
          }
        />
        <StatTile
          label="처리 중인 보안 위협"
          value={secops === null ? NO_VALUE : secops.length}
          tone={risk.HIGH > 0 ? 'critical' : secops !== null && secops.length > 0 ? 'serious' : undefined}
          note={`위험도 높음 ${risk.HIGH} · 중간 ${risk.MEDIUM} · 낮음 ${risk.LOW}`}
        />
        <StatTile
          label="월 절감 가능액"
          value={savings === null ? NO_VALUE : formatUsd(savings.amount)}
          note={
            savings === null
              ? '인시던트를 불러오지 못했습니다'
              : `비용 제안 ${savings.counted + savings.unknown}건${savings.unknown > 0 ? ` · 추정 없음 ${savings.unknown}건` : ''}`
          }
        />
        <StatTile
          label="관제 중 자산"
          value={assets.items.length}
          tone={assets.collection_status === 'FAILED' ? 'critical' : assets.collection_status === 'PARTIAL' ? 'warning' : undefined}
          note={`${collection} · ${collectedAgo}`}
        />
      </StatGrid>

      {/* 3. 무엇을 해야 하나 | 무엇을 대신 해 줬나 */}
      <div className="flex flex-wrap gap-4">
        <ChartCard
          id="todo"
          className="flex-[3_1_520px] scroll-mt-6"
          title="지금 처리할 일"
          description="보안은 위험도순, 비용은 절감액순입니다."
          aside={
            queue !== null && queue.length > QUEUE_LIMIT ? (
              <span className="text-muted-foreground text-[13px]">
                {queue.length}건 중 {QUEUE_LIMIT}건
              </span>
            ) : null
          }
        >
          {rows === null ? (
            incidentsErrorSlot
          ) : rows.length === 0 ? (
            <EmptyLine text="지금 처리할 일이 없습니다. 새 위협이 들어오면 여기에 먼저 보입니다." />
          ) : (
            <ul className="-my-1 divide-y">
              {rows.slice(0, QUEUE_LIMIT).map((row) => (
                <QueueRow key={row.incident.incident_id} row={row} nowMs={nowMs} />
              ))}
            </ul>
          )}
          <div className="flex flex-wrap gap-x-6 gap-y-2 border-t pt-4 text-sm font-semibold">
            <Link href="/security?tab=incidents" className="text-primary inline-flex items-center gap-1 hover:underline">
              보안 인시던트 전체 <ArrowRight aria-hidden className="size-3.5" />
            </Link>
            <Link href="/assets?tab=incidents" className="text-primary inline-flex items-center gap-1 hover:underline">
              자산 인시던트 전체 <ArrowRight aria-hidden className="size-3.5" />
            </Link>
          </div>
        </ChartCard>

        {/* 끝났다는 것만 말하고 성공 여부는 말하지 않는다. 목록 응답에 실행 결과가 없다(lib/customer-dashboard.ts doneNote). */}
        <ChartCard
          className="flex-[2_1_340px]"
          title="최근 끝난 일"
          description="최근 7일 동안 끝난 건입니다. 조치 결과는 상세에서 확인합니다."
        >
          {done === null ? (
            <p className="text-muted-foreground text-sm">인시던트를 불러오지 못했습니다.</p>
          ) : done.length === 0 ? (
            <p className="text-foreground-secondary py-2 text-[15px]">최근 7일 동안 끝난 건이 없습니다.</p>
          ) : (
            <ul className="flex flex-col gap-3.5">
              {done.map((i) => (
                <li key={i.incident_id} className="flex gap-3">
                  <CircleDot aria-hidden className="text-muted-foreground mt-0.5 size-5 shrink-0" />
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <Link href={`/incidents/${encodeURIComponent(i.incident_id)}`} className="truncate text-sm font-semibold hover:underline">
                      {incidentTitle(i)}
                    </Link>
                    <span className="text-muted-foreground text-[13px]" title={formatKst(i.updated_at)}>
                      {doneNote(i)} · {timeAgo(i.updated_at, nowMs)}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </ChartCard>
      </div>

      {/* 4. 흐름 | 5. 관제 범위 */}
      <div className="flex flex-wrap gap-4">
        <ThreatHoursCard hours={hours} className="flex-[3_1_520px]" />
        <CoverageCard assets={assets} className="flex-[2_1_340px]" />
      </div>
    </div>
  );
}

function EmptyLine({ text }: { text: string }) {
  return (
    <p className="text-foreground-secondary flex items-center gap-2.5 py-2 text-[15px]">
      <CircleCheck aria-hidden className="text-badge-good-fg size-5 shrink-0" />
      {text}
    </p>
  );
}

function QueueRow({ row, nowMs }: { row: ProposalRow; nowMs: number }) {
  const { incident, savings } = row;
  const href = `/incidents/${encodeURIComponent(incident.incident_id)}`;
  const risk = incident.initial_risk_level === null ? null : RISK_LEVEL_LABELS[incident.initial_risk_level];
  const badge: EnumLabel | null =
    incident.category === 'SECOPS' && risk !== null
      ? { ...risk, label: `위험 ${risk.label}` }
      : CATEGORY_LABELS[incident.category];
  const status = INCIDENT_STATUS_LABELS[incident.status]?.label ?? incident.status;

  return (
    <li className="flex items-center gap-4 py-3.5">
      <EnumBadge entry={badge} className="w-[72px] shrink-0 justify-center" />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <Link href={href} className="truncate text-[15px] font-semibold hover:underline">
          {incidentTitle(incident)}
        </Link>
        <span className="text-muted-foreground text-[13px]" title={formatKst(incident.created_at)}>
          {status} · {timeAgo(incident.created_at, nowMs)}
          {incident.category === 'FINOPS' ? (savings === null ? ' · 절감액 추정 없음' : ` · 월 ${formatUsd(savings)} 절감`) : ''}
        </span>
      </div>
      <Link
        href={href}
        className="hover:bg-muted inline-flex h-9 shrink-0 items-center rounded-md border px-3.5 text-[13px] font-semibold"
      >
        검토
      </Link>
    </li>
  );
}

function hourLabel(iso: string): string {
  return new Date(iso).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', hour12: false });
}

function ThreatHoursCard({ hours, className }: { hours: ReturnType<typeof threatHours>; className?: string }) {
  if (hours === null) {
    return (
      <ChartCard className={className} title="최근 24시간 위협 이벤트" description="1시간 단위 발생 건수">
        <p className="text-muted-foreground text-sm">추이를 불러오지 못했습니다. 지금 상태는 위 숫자가 기준입니다.</p>
      </ChartCard>
    );
  }
  const total = hours.reduce((s, h) => s + h.total, 0);
  const max = Math.max(1, ...hours.map((h) => h.total));
  const ticks = [0, 6, 12, 18, 23];

  return (
    <ChartCard
      className={className}
      title="최근 24시간 위협 이벤트"
      description="1시간 단위 발생 건수 · 무차별 대입 · 인터넷 개방 포트"
      aside={
        <span className="text-foreground-secondary text-[13px]">
          합계 <strong className="text-foreground text-base">{total}</strong>건
        </span>
      }
    >
      <div className="flex gap-2">
        <div className="text-muted-foreground relative h-36 w-5 shrink-0 text-[11px]">
          <span className="absolute top-0 right-0 -translate-y-1/2">{max}</span>
          <span className="absolute right-0 bottom-0 translate-y-1/2">0</span>
        </div>
        <div className="border-axis relative flex h-36 flex-1 items-end gap-0.5 border-b">
          <div aria-hidden className="border-gridline absolute inset-x-0 top-0 border-t" />
          {hours.map((h) => (
            <div
              key={h.at}
              title={`${hourLabel(h.at)} · ${h.total}건`}
              className="group relative flex h-full flex-1 items-end"
            >
              <div
                className="bg-data group-hover:bg-primary w-full rounded-t-[3px] transition-colors"
                style={{ height: h.total === 0 ? 0 : `${(h.total / max) * 100}%` }}
              />
            </div>
          ))}
        </div>
      </div>
      <div className="text-muted-foreground flex justify-between pl-7 text-[11px]">
        {ticks.map((t) => (
          <span key={t}>{hourLabel(hours[t].at)}</span>
        ))}
      </div>
    </ChartCard>
  );
}

function CoverageCard({ assets, className }: { assets: AssetsResponse; className?: string }) {
  const counts = coverageCounts(assets.items);
  // 판정 대기 · 실패는 한 칸(대기 · 확인 불가의 노랑, DESIGN.md §상태 색)으로 그리고 범례에서 둘을 나눠 적는다.
  // 실패를 빨강으로 따로 그리면 같은 막대에 위협과 같은 색이 두 번 선다.
  const parts = [
    { key: 'threat', label: '위협', count: counts.threat, fill: statusFill('red').fill },
    { key: 'waste', label: '비용 후보', count: counts.waste, fill: statusFill('orange').fill },
    { key: 'ok', label: '정상 · 제외', count: counts.ok, fill: statusFill('gray').fill },
    {
      key: 'unjudged',
      label: '판정 대기 · 실패',
      count: counts.pending + counts.failed,
      detail: `대기 ${counts.pending} · 실패 ${counts.failed}`,
      fill: statusFill('yellow').fill,
    },
  ];
  const shown = parts.filter((p) => p.count > 0);

  const byType = new Map<AssetType, number>();
  for (const a of assets.items) byType.set(a.asset_type, (byType.get(a.asset_type) ?? 0) + 1);
  const types = [...byType.entries()].sort((a, b) => b[1] - a[1]);
  const maxType = Math.max(1, ...types.map(([, n]) => n));

  return (
    <ChartCard className={className} title="관제 범위" description={`판정 대상 ${counts.judged}개의 결과와 유형별 자산 수`}>
      {counts.judged > 0 ? (
        <div className="flex flex-col gap-2.5">
          <div
            role="img"
            aria-label={parts.map((p) => `${p.label} ${p.count}개${p.detail ? `(${p.detail})` : ''}`).join(', ')}
            className="flex h-6 gap-0.5"
          >
            {shown.map((p, i) => (
              <div
                key={p.key}
                title={`${p.label} ${p.count}개${p.detail ? ` (${p.detail})` : ''}`}
                className={cn(p.fill, i === 0 && 'rounded-l-[4px]', i === shown.length - 1 && 'rounded-r-[4px]')}
                style={{ width: `${(p.count / counts.judged) * 100}%` }}
              />
            ))}
          </div>
          <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[13px]">
            {parts.map((p) => (
              <li key={p.key} className="inline-flex items-center gap-1.5">
                <span aria-hidden className={cn('size-2.5 rounded-[3px]', p.fill)} />
                {p.label} <strong>{p.count}</strong>
                {p.detail && p.count > 0 ? <span className="text-muted-foreground">({p.detail})</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="flex flex-col gap-2 border-t pt-4">
        {types.map(([type, n]) => (
          <div key={type} className="grid grid-cols-[112px_minmax(0,1fr)_28px] items-center gap-2.5">
            <span className="truncate text-[13px]">{ASSET_TYPE_LABELS[type]?.label ?? type}</span>
            <div className="bg-data-track h-3.5 rounded-[3px]">
              <div className="bg-data h-3.5 rounded-r-[3px]" style={{ width: `${(n / maxType) * 100}%` }} />
            </div>
            <span className="text-right text-[13px] font-bold tabular-nums">{n}</span>
          </div>
        ))}
        {types.length === 0 ? <p className="text-muted-foreground text-sm">수집된 자산이 없습니다.</p> : null}
      </div>

      {assets.uncollected.length > 0 ? (
        <p className="text-badge-warning-fg text-[13px]">
          조회하지 못한 유형: {assets.uncollected.map((u) => `${ASSET_TYPE_LABELS[u.asset_type]?.label ?? u.asset_type}(${u.reason_code})`).join(', ')}
        </p>
      ) : null}
    </ChartCard>
  );
}
