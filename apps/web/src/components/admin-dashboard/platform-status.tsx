// 관리자 대시보드 맨 위의 플랫폼 상태(2026-11-02 초안, 중앙 관리 · 조치 권한이 있는 플랫폼 계정). Vigilantis 자체가 돌고 있는지 본다.
// 계약은 FE 제안이며 서버에 아직 없다(lib/platform-status.ts 머리말). 404면 "API 없음"으로 적고 아래 고객사 운영은 그대로 뜬다.
//
// 구성: 구성 요소 8개 상태 → 수집 주기(최근 주기 소요 시간 막대, 실패 고객사가 있던 주기는 빨강) → AI 분석(시간별 호출 · 성공/실패)
// → 고객사 API 호출(2026-11-03, 고객사 x 시간 히트맵 + 기준 초과 고객사와 그 시각).
// 그래프는 platform-charts.tsx(클라이언트, recharts)가 그린다 — 폭을 따르고 막대에 올리면 값이 뜬다.
//
// 훅이 없어 `'use client'`를 붙이지 않는다.

import Link from 'next/link';

import { ChartSection, ShareBar, TIP_ANCHOR, TipBox, tipAlign } from '@/components/admin-dashboard/chart-parts';
import { AiCallsChart, ScanDurationChart } from '@/components/admin-dashboard/platform-charts';
import { ChartCard } from '@/components/chart-card';
import { formatAge } from '@/lib/admin-dashboard';
import { ApiError } from '@/lib/api/client';
import {
  COMPONENT_LABELS,
  STATE_LABELS,
  orderedComponents,
  overCallTenants,
  overallState,
  scanSummary,
  usageLevel,
  type PlatformStatusResponse,
} from '@/lib/platform-status';
import { cn } from '@/lib/utils';

function ComponentsCard({ status, nowMs }: { status: PlatformStatusResponse; nowMs: number }) {
  const components = orderedComponents(status.components);
  const bad = components.filter((c) => c.state === 'DOWN' || c.state === 'DEGRADED').length;
  return (
    <ChartCard
      title="구성 요소"
      aside={<span className="text-sm font-bold tabular-nums">{bad === 0 ? '모두 정상' : `이상 ${bad}곳`}</span>}
    >
      <ul className="flex flex-col divide-y text-sm">
        {components.map((c) => {
          const meta = COMPONENT_LABELS[c.key] ?? { label: c.key, note: '' };
          const state = STATE_LABELS[c.state];
          return (
            <li key={c.key} className={cn('flex items-center gap-2 py-1.5', c.detail && TIP_ANCHOR)}>
              {c.detail ? (
                // 줄에서 잘린 설명 전체를 보인다
                <TipBox align="start">
                  <span className="font-medium">{meta.label}</span>
                  <span className="text-muted-foreground max-w-64 whitespace-normal">{c.detail}</span>
                </TipBox>
              ) : null}
              <span aria-hidden className={cn('size-2 shrink-0 rounded-full', state.dot)} />
              <span className="min-w-0 flex-1 truncate">
                <span className="font-medium">{meta.label}</span>
                <span className="text-muted-foreground ml-1.5 text-xs">{c.detail ?? meta.note}</span>
              </span>
              <span className={cn('shrink-0 text-xs', c.state === 'OK' ? 'text-muted-foreground' : 'font-semibold')}>
                {state.label}
                {c.state !== 'OK' && c.last_success_at !== null ? ` · 마지막 정상 ${formatAge(c.last_success_at, nowMs)}` : null}
              </span>
            </li>
          );
        })}
      </ul>
    </ChartCard>
  );
}

function ScanCard({ status, nowMs }: { status: PlatformStatusResponse; nowMs: number }) {
  const { scan } = status;
  const s = scanSummary(scan, nowMs);
  const minutes = Math.round(scan.interval_seconds / 60);
  return (
    <ChartCard
      title="수집 주기"
      description={`${minutes}분 주기 · 최근 ${scan.cycles.length}회`}
      aside={
        <span className={cn('text-sm font-bold tabular-nums', s.late && 'text-badge-critical-fg')}>
          {s.lastAgeMinutes === null ? '기록 없음' : `${s.lastAgeMinutes}분 전`}
        </span>
      }
    >
      {scan.cycles.length === 0 ? (
        <p className="text-muted-foreground text-sm">수집 기록이 없습니다.</p>
      ) : (
        <>
          <ChartSection title="주기별 소요 시간(빨강 = 실패한 고객사가 있던 주기)">
            <ScanDurationChart cycles={scan.cycles} />
          </ChartSection>
          <dl className="grid grid-cols-3 gap-2 border-t pt-3 text-center">
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground text-xs">평균 소요</dt>
              <dd className="text-lg font-bold tabular-nums">{s.avgDurationSeconds ?? '-'}초</dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground text-xs">마지막 실패</dt>
              <dd className={cn('text-lg font-bold tabular-nums', s.lastFailed > 0 && 'text-badge-critical-fg')}>{s.lastFailed}곳</dd>
            </div>
            <div className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground text-xs">실패 있던 주기</dt>
              <dd className="text-lg font-bold tabular-nums">{s.cyclesWithFailure}회</dd>
            </div>
          </dl>
          {s.late ? (
            <p role="status" className="text-badge-critical-fg text-xs">
              주기 두 번이 지나도록 새 수집이 없습니다. 수집 스케줄러를 확인하세요.
            </p>
          ) : null}
        </>
      )}
    </ChartCard>
  );
}

function AiCard({ status }: { status: PlatformStatusResponse }) {
  const { ai } = status;
  return (
    <ChartCard
      // 2열일 때는 혼자 남으므로 한 줄을 다 쓴다
      className="@3xl/platform:col-span-2 @6xl/platform:col-span-1"
      title="AI 분석"
      description={`최근 ${ai.window_hours}시간`}
      aside={<span className="text-sm font-bold tabular-nums">{ai.requests.toLocaleString('ko-KR')}건</span>}
    >
      <ChartSection title="시간별 AI 호출(빨강 = 실패가 있던 시간)">
        <AiCallsChart hourly={ai.hourly} />
      </ChartSection>
      <ChartSection title="AI 호출 결과">
        <ShareBar
          parts={[
            { key: 'ok', label: '성공', value: ai.requests - ai.failures, fill: 'bg-status-good' },
            { key: 'fail', label: '실패', value: ai.failures, fill: 'bg-status-critical' },
          ]}
        />
      </ChartSection>
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

function ApiUsageCard({ usage }: { usage: PlatformStatusResponse['api_usage'] }) {
  const limit = usage.threshold_per_hour;
  const rows = usage.tenants.slice(0, USAGE_ROWS);
  // 진하기는 기준 이하 값끼리 나눈다. 초과 칸은 빨강이라, 초과 값까지 넣으면 평소 칸이 전부 가장 옅은 단계로 뭉친다.
  const max = Math.max(0, ...rows.flatMap((t) => t.hourly).filter((n) => limit === null || n <= limit));
  const over = overCallTenants(usage);
  const total = usage.tenants.reduce((s, t) => s + t.hourly.reduce((a, b) => a + b, 0), 0);
  const columns = `minmax(4.5rem, 8rem) repeat(${usage.hours.length}, minmax(0, 1fr))`;
  return (
    <ChartCard
      className="@3xl/platform:col-span-2 @6xl/platform:col-span-3"
      title="고객사 API 호출"
      description={`최근 ${usage.window_hours}시간 · 호출 많은 ${rows.length}곳 · 시간당 건수${
        limit === null ? ' · 과다 기준 미정' : ` · 기준 시간당 ${limit.toLocaleString('ko-KR')}건`
      }`}
      aside={
        <span className={cn('text-sm font-bold tabular-nums', over.length > 0 && 'text-badge-critical-fg')}>
          {limit === null ? `${total.toLocaleString('ko-KR')}건` : over.length === 0 ? '기준 초과 없음' : `기준 초과 ${over.length}곳`}
        </span>
      }
    >
      {rows.length === 0 ? (
        <p className="text-muted-foreground text-sm">호출 기록이 없습니다.</p>
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
                    </TipBox>
                  </span>
                  {usage.hours.map((at, i) => {
                    const n = t.hourly[i] ?? 0;
                    const exceeded = limit !== null && n > limit;
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
                          {exceeded ? (
                            <span className="text-badge-critical-fg font-semibold">
                              기준 시간당 {limit.toLocaleString('ko-KR')}건 초과
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
            {usage.other_tenants > 0 ? (
              <p className="text-muted-foreground text-xs">그 밖의 고객사 {usage.other_tenants}곳은 표에서 뺐습니다.</p>
            ) : null}
          </ChartSection>
          <ChartSection title="기준 초과 고객사(최고 건수 순)">
            {limit === null ? (
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
                      ({kstHourLabel(t.peakAt)}) · 초과 {t.hoursOver}시간 · 마지막 {kstHourLabel(t.lastOverAt)}
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
  error,
  nowMs,
}: {
  /** 조회 결과. 실패면 null이고 `error`가 그 오류다. */
  status: PlatformStatusResponse | null;
  error: unknown;
  nowMs: number;
}) {
  const overall = status === null ? null : overallState(status.components);
  const missingApi = error instanceof ApiError && error.httpStatus === 404;
  return (
    <section aria-labelledby="platform-status" className="@container/platform flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="platform-status" className="text-lg font-semibold">
          플랫폼 상태
        </h2>
        {overall !== null ? (
          <span className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold">
            <span aria-hidden className={cn('size-2 rounded-full', STATE_LABELS[overall].dot)} />
            {overall === 'OK' ? '정상 운영 중' : STATE_LABELS[overall].label}
          </span>
        ) : null}
      </div>
      {status === null ? (
        <p role="status" className="text-muted-foreground rounded-xl border p-4 text-sm">
          {missingApi
            ? '플랫폼 상태 API(GET /api/v1/platform/status)가 서버에 아직 없습니다. 지금은 목업에서만 보입니다.'
            : '플랫폼 상태를 불러오지 못했습니다. 새로고침하면 다시 조회합니다.'}
        </p>
      ) : (
        <div className="grid gap-4 @3xl/platform:grid-cols-2 @6xl/platform:grid-cols-3">
          <ComponentsCard status={status} nowMs={nowMs} />
          <ScanCard status={status} nowMs={nowMs} />
          <AiCard status={status} />
          {status.api_usage ? <ApiUsageCard usage={status.api_usage} /> : null}
        </div>
      )}
    </section>
  );
}
