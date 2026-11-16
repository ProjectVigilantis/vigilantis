// 관리자 대시보드 맨 위의 플랫폼 상태(중앙 관리계정 전용). 계약은 서버 `GET /api/v1/platform/overview?hours=24`
// (types/api.ts `PlatformOverviewResponse`, 계산은 lib/platform-status.ts)다. 조회에 실패해도 아래 고객사 운영은 그대로 뜬다.
//
// 구성: 수집 회차(최근 24시간, 결과별) · 인시던트(상태별) → 고객사 API 호출(고객사 x 시간 히트맵 + 기준 초과 고객사와 그 시각).
// 응답의 `tenants`(연결 상태별 고객사 수)는 그리지 않는다. 같은 숫자를 아래 "고객사 운영"의 고객사 상태가 GET /tenants로 이미 센다.
//
// 훅이 없어 `'use client'`를 붙이지 않는다.

import Link from 'next/link';

import { BarRows, ChartSection, ShareBar, TIP_ANCHOR, TipBox, tipAlign } from '@/components/admin-dashboard/chart-parts';
import { ChartCard } from '@/components/chart-card';
import { INCIDENT_STATUS_LABELS } from '@/lib/enum-labels';
import { exceedsThreshold, hasOverCallThreshold, overCallTenants, usageLevel } from '@/lib/platform-status';
import { cn } from '@/lib/utils';
import {
  INCIDENT_STATUSES,
  type ApiUsageSummary,
  type IncidentStatus,
  type PlatformCollectionCounts,
  type PlatformIncidentCounts,
  type PlatformOverviewResponse,
} from '@/types/api';

function CollectionCard({ collection }: { collection: PlatformCollectionCounts }) {
  const { success, partial, failed, in_progress, window_hours } = collection;
  const total = success + partial + failed + in_progress;
  return (
    <ChartCard
      title="수집 회차"
      description={`최근 ${window_hours}시간 · 고객사 x 리전 단위`}
      aside={
        <span className={cn('text-sm font-bold tabular-nums', failed > 0 && 'text-badge-critical-fg')}>
          {failed > 0 ? `실패 ${failed}회` : `${total}회`}
        </span>
      }
    >
      {total === 0 ? (
        <p className="text-muted-foreground text-sm">최근 {window_hours}시간 동안 수집 기록이 없습니다.</p>
      ) : (
        <ChartSection title="회차 결과">
          <ShareBar
            parts={[
              { key: 'ok', label: '성공', value: success, fill: 'bg-status-good' },
              { key: 'partial', label: '일부 실패', value: partial, fill: 'bg-status-warning' },
              { key: 'fail', label: '실패', value: failed, fill: 'bg-status-critical' },
              { key: 'run', label: '진행 중', value: in_progress, fill: 'bg-data' },
            ]}
          />
        </ChartSection>
      )}
    </ChartCard>
  );
}

/** 상태 점 색. 배지 색(enum-labels.ts)과 같은 계열이고, 종료는 일감이 아니라 옅게 둔다. */
const INCIDENT_DOT: Record<IncidentStatus, string> = {
  ANALYZING: 'bg-status-idle',
  AWAITING_APPROVAL: 'bg-status-warning',
  ACTION_IN_PROGRESS: 'bg-data',
  AWAITING_CLOSURE: 'bg-status-good',
  RESOLVED: 'bg-foreground/25',
  FAILED: 'bg-status-critical',
};

function IncidentsCard({ incidents }: { incidents: PlatformIncidentCounts }) {
  return (
    <ChartCard
      title="인시던트"
      description="전 고객사 · 상태별"
      aside={<span className="text-sm font-bold tabular-nums">{incidents.total.toLocaleString('ko-KR')}건</span>}
    >
      {incidents.total === 0 ? (
        <p className="text-muted-foreground text-sm">인시던트가 없습니다.</p>
      ) : (
        <BarRows
          labelWidth={112}
          rows={INCIDENT_STATUSES.map((s) => ({
            key: s,
            label: INCIDENT_STATUS_LABELS[s]?.label ?? s,
            value: incidents.by_status[s] ?? 0,
            dot: INCIDENT_DOT[s],
          }))}
        />
      )}
    </ChartCard>
  );
}

/** 히트맵 행 수 상한. 고객사가 늘어도 높이가 고정된다. */
const USAGE_ROWS = 10;
/** 과다 호출 목록 상한(대시보드 상위 5곳 규칙). */
const OVER_ROWS = 5;
const LEVEL_FILL = [
  'var(--data-track)',
  'color-mix(in oklch, var(--data) 22%, var(--data-track))',
  'color-mix(in oklch, var(--data) 45%, var(--data-track))',
  'color-mix(in oklch, var(--data) 70%, var(--data-track))',
  'var(--data)',
] as const;

const kstParts = (iso: string) => {
  const parts = new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul',
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(iso));
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
  return { md: `${get('month')}/${get('day')}`, hour: get('hour') };
};
/** "11/3 14시" — 시간 칸 하나를 가리킨다. */
const kstHourLabel = (iso: string) => {
  const p = kstParts(iso);
  return `${p.md} ${p.hour}시`;
};

function ApiUsageCard({ usage }: { usage: ApiUsageSummary }) {
  const judged = hasOverCallThreshold(usage.items);
  // 서버가 total_calls 내림차순으로 준다. 상위 N곳만 그리고 나머지는 수만 적는다.
  const rows = usage.items.slice(0, USAGE_ROWS);
  const otherTenants = usage.items.length - rows.length;
  // 진하기는 기준 이하 값끼리 나눈다. 초과 칸은 빨강이라, 초과 값까지 넣으면 평소 칸이 전부 가장 옅은 단계로 뭉친다.
  const max = Math.max(0, ...rows.flatMap((t) => t.hourly.filter((n) => !exceedsThreshold(t, n))));
  const over = overCallTenants(usage);
  const total = usage.items.reduce((s, t) => s + t.total_calls, 0);
  const columns = `minmax(4.5rem, 8rem) repeat(${usage.hours.length}, minmax(0, 1fr))`;
  return (
    <ChartCard
      className="@3xl/platform:col-span-2"
      title="고객사 API 호출"
      description={`최근 ${usage.window_hours}시간 · 호출 많은 ${rows.length}곳 · 시간당 건수${judged ? '' : ' · 과다 기준 미정'}`}
      aside={
        <span className={cn('text-sm font-bold tabular-nums', over.length > 0 && 'text-badge-critical-fg')}>
          {!judged ? `${total.toLocaleString('ko-KR')}건` : over.length === 0 ? '기준 초과 없음' : `기준 초과 ${over.length}곳`}
        </span>
      }
    >
      {total === 0 ? (
        <p className="text-muted-foreground text-sm">최근 {usage.window_hours}시간 동안 고객사 호출이 없습니다.</p>
      ) : (
        <div className="grid gap-6 @4xl/chart:grid-cols-[minmax(0,1fr)_18rem]">
          <ChartSection title="고객사별 시간당 호출(진할수록 많음, 빨강 = 기준 초과)">
            <div
              role="img"
              aria-label={rows
                .map((t) => `${t.tenant_name} 최고 ${Math.max(0, ...t.hourly).toLocaleString('ko-KR')}건`)
                .join(', ')}
              className="flex flex-col gap-0.5"
            >
              {rows.map((t) => (
                <div key={t.tenant_id} className="grid items-center gap-0.5" style={{ gridTemplateColumns: columns }}>
                  <span className={cn(TIP_ANCHOR, 'min-w-0 pr-2 text-xs')}>
                    <span className="block truncate">{t.tenant_name}</span>
                    <TipBox align="start">
                      <span className="font-medium">{t.tenant_name}</span>
                      <span className="text-muted-foreground tabular-nums">
                        합계 {t.total_calls.toLocaleString('ko-KR')}건 · 자산 {t.asset_count} · 계정 {t.staff_count}
                      </span>
                    </TipBox>
                  </span>
                  {usage.hours.map((at, i) => {
                    const n = t.hourly[i] ?? 0;
                    const exceeded = exceedsThreshold(t, n);
                    return (
                      <span
                        key={at}
                        className={cn(TIP_ANCHOR, 'hover:ring-foreground/60 h-[22px] rounded-[2px] hover:ring-2')}
                        style={{ background: exceeded ? 'var(--status-critical)' : LEVEL_FILL[usageLevel(n, max)] }}
                      >
                        <TipBox align={tipAlign(i, usage.hours.length)}>
                          <span className="text-muted-foreground">{kstHourLabel(at)}</span>
                          <span className="flex justify-between gap-6 font-medium">
                            <span>{t.tenant_name}</span>
                            <span className="font-mono tabular-nums">{n.toLocaleString('ko-KR')}건</span>
                          </span>
                          {exceeded && t.threshold_per_hour !== null ? (
                            <span className="text-badge-critical-fg font-semibold">
                              기준 시간당 {t.threshold_per_hour.toLocaleString('ko-KR')}건 초과
                            </span>
                          ) : null}
                        </TipBox>
                      </span>
                    );
                  })}
                </div>
              ))}
              <div className="grid gap-0.5 pt-1" style={{ gridTemplateColumns: columns }}>
                <span />
                {usage.hours.map((at, i) => {
                  const p = kstParts(at);
                  // 12시간마다 한 번, 카드가 넓으면 6시간마다. 날이 바뀌는 칸은 날짜와 함께 적는다.
                  const hour = Number(p.hour);
                  const show = hour % 6 === 0;
                  return (
                    <span
                      key={at}
                      className={cn(
                        'text-muted-foreground overflow-visible text-[11px] whitespace-nowrap tabular-nums',
                        // display:none이면 격자 칸이 빠져 뒤 라벨이 밀린다 — 자리는 두고 글자만 감춘다
                        hour % 12 !== 0 && 'invisible @xl/chart:visible',
                      )}
                    >
                      {show ? (p.hour === '00' || i === 0 ? `${p.md} ${p.hour}시` : `${p.hour}시`) : null}
                    </span>
                  );
                })}
              </div>
            </div>
            {otherTenants > 0 ? (
              <p className="text-muted-foreground text-xs">그 밖의 고객사 {otherTenants}곳은 표에서 뺐습니다.</p>
            ) : null}
          </ChartSection>
          <ChartSection title="기준 초과 고객사(최고 건수 순)">
            {!judged ? (
              <p className="text-muted-foreground text-sm">서버에 과다 호출 기준이 없어 판정하지 않았습니다.</p>
            ) : over.length === 0 ? (
              <p className="text-muted-foreground text-sm">기준을 넘은 고객사가 없습니다.</p>
            ) : (
              <ul className="flex flex-col divide-y text-sm">
                {over.slice(0, OVER_ROWS).map((t) => (
                  <li key={t.tenant_id} className="flex flex-col gap-0.5 py-1.5">
                    <Link
                      href={`/tenants/${encodeURIComponent(t.tenant_id)}`}
                      className="truncate font-medium hover:underline"
                    >
                      {t.tenant_name}
                    </Link>
                    <span className="text-muted-foreground text-xs tabular-nums">
                      최고 <span className="text-badge-critical-fg font-semibold">{t.peakRequests.toLocaleString('ko-KR')}건</span>{' '}
                      ({kstHourLabel(t.peakAt)}) · 기준 {t.limit.toLocaleString('ko-KR')}건 · 초과 {t.hoursOver}시간 · 마지막{' '}
                      {kstHourLabel(t.lastOverAt)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {over.length > OVER_ROWS ? (
              <p className="text-muted-foreground text-xs">외 {over.length - OVER_ROWS}곳</p>
            ) : null}
          </ChartSection>
        </div>
      )}
    </ChartCard>
  );
}

export function PlatformStatusSection({
  status,
}: {
  /** 조회 결과. 실패면 null이다. */
  status: PlatformOverviewResponse | null;
}) {
  return (
    <section aria-labelledby="platform-status" className="@container/platform flex flex-col gap-3">
      <h2 id="platform-status" className="text-lg font-semibold">
        플랫폼 상태
      </h2>
      {status === null ? (
        <p role="status" className="text-muted-foreground rounded-xl border p-4 text-sm">
          플랫폼 상태를 불러오지 못했습니다. 새로고침하면 다시 조회합니다.
        </p>
      ) : (
        <div className="grid gap-4 @3xl/platform:grid-cols-2">
          <CollectionCard collection={status.collection} />
          <IncidentsCard incidents={status.incidents} />
          <ApiUsageCard usage={status.api_usage} />
        </div>
      )}
    </section>
  );
}
