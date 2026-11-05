// 플랫폼 운영자 대시보드(2026-10-29). 운영자가 여기서 알아야 할 것은 둘이다(lib/admin-dashboard.ts 머리말).
//   1. 수동 처리가 필요한 문제 — 플랫폼 자동 기능으로 풀리지 않아 운영자가 고객 계정 일을 직접 처리해야 하는 것
//   2. 고객사별 관제 직원 — 어느 고객사에 어떤 로그인 계정이 있는지
// 고객 하나를 들여다보는 구성(자산 띠 · 위협 추이 · AI 제안)은 관제 화면과 고객 웹의 몫이다.
//
// 대시보드는 숫자와 그래프만 둔다. 지금 어떤 상황인지 한눈에 읽히게 하고, 한 건 한 줄 목록은 숫자 카드와 그래프 아래 링크로
// 넘어간 상세 화면에서 본다(수동 처리 문제 `ISSUES_PATH` · 고객사 목록 · 고객사별 직원 `STAFF_PATH`). 그래프 아래 링크는 숫자 카드와
// 겹쳐 뺐다(2026-11-02) — 목록으로 가는 길은 숫자 카드다.
//   숫자 카드 4개(고객사 · 고객사 직원 · 확인 필요 · 수동 처리 필요) → 그래프 4장(고객사 상태 · 고객사 직원 · 확인 필요 · 수동 처리 문제)
//   확인 필요(2026-11-03) = 연결 실패 · 확인 불가 · 미납 + API 이상 호출(플랫폼 상태를 받는 계정만, 기준 초과 고객사).
// 고객이 수백 곳으로 늘어도 높이가 고정된다: 그래프는 분류별 건수와 상위 5곳만 그린다.
//
// 훅이 없어 `'use client'`를 붙이지 않는다.

import { ChevronRight } from 'lucide-react';
import Link from 'next/link';

import { BarRows, ChartSection, Histogram, ShareBar } from '@/components/admin-dashboard/chart-parts';
import { PlatformStatusSection } from '@/components/admin-dashboard/platform-status';
import { AutoRefresh } from '@/components/auto-refresh';
import { ChartCard } from '@/components/chart-card';
import { StatTile } from '@/components/stat-tile';
import { NeedCheckRows, needCheckCounts, type ApiOverCall } from '@/components/tenants/tenant-charts';
import {
  issueCountsByTenant,
  issueRows,
  manualIssues,
  staffCountDistribution,
  staffSummary,
  type StaffCountDistribution,
  type StaffSummary,
  type TenantIssueCount,
} from '@/lib/admin-dashboard';
import { ISSUES_PATH, STAFF_PATH, TENANTS_PATH } from '@/lib/home-path';
import { summarizeTenants, TENANT_STATUS_LABELS } from '@/lib/tenants';
import { cn } from '@/lib/utils';
import { overCallTenants, type PlatformStatusResponse } from '@/lib/platform-status';
import type { IncidentListItem, TenantListItem, UserListItem } from '@/types/api';

/** 그래프에 이름으로 올리는 고객사 수. 나머지는 상세 화면에서 본다. */
const TOP_N = 5;

function IssuesChart({
  counts,
  connection,
  stuck,
  incidentsFailed,
}: {
  counts: readonly TenantIssueCount[];
  connection: number;
  stuck: number;
  incidentsFailed: boolean;
}) {
  const total = connection + stuck;
  return (
    <ChartCard
      title="수동 처리가 필요한 문제"
      aside={<span className="text-sm font-bold tabular-nums">{total}건</span>}
    >
      {total === 0 ? (
        <p className="text-muted-foreground text-sm">지금 수동으로 처리할 문제가 없습니다.</p>
      ) : (
        <>
          <ChartSection title="분류">
            <ShareBar
              parts={[
                { key: 'c', label: '연결 실패', value: connection, fill: 'bg-status-critical' },
                { key: 's', label: '진행 불가 인시던트', value: stuck, fill: 'bg-status-serious' },
              ]}
            />
          </ChartSection>
          <ChartSection title={`문제가 많은 고객사 ${Math.min(TOP_N, counts.length)}곳`}>
            <BarRows
              labelWidth={112}
              rows={counts.slice(0, TOP_N).map((c) => ({
                key: c.tenantId,
                label: c.tenantName,
                value: c.total,
                title: `${c.tenantName} · ${c.connection ? '연결 실패 1 · ' : ''}진행 불가 ${c.stuck}`,
                href: `/tenants/${encodeURIComponent(c.tenantId)}`,
              }))}
            />
          </ChartSection>
        </>
      )}
      {incidentsFailed ? (
        <p role="status" className="text-muted-foreground text-xs">
          인시던트 목록을 불러오지 못해 진행 불가 인시던트는 빠졌습니다.
        </p>
      ) : null}
    </ChartCard>
  );
}

/** 고객사 상태 비율 막대 + 상태별 수. 제목은 감싸는 카드(TenantOverviewChart)가 단다. */
function TenantStatusChart({ summary }: { summary: ReturnType<typeof summarizeTenants> }) {
  const { ACTIVE, PENDING_CONNECTION, SUSPENDED, UNPAID } = summary.byStatus;
  // 상태별 네 칸의 합이 전체 고객사 수다. 직원 유무는 고객사 계정 그래프의 몫이라 여기서 나누지 않는다(2026-11-02).
  const parts = [
    { key: 'a', label: TENANT_STATUS_LABELS.ACTIVE.label, value: ACTIVE, fill: 'bg-status-good' },
    { key: 'p', label: TENANT_STATUS_LABELS.PENDING_CONNECTION.label, value: PENDING_CONNECTION, fill: 'bg-status-warning' },
    { key: 's', label: TENANT_STATUS_LABELS.SUSPENDED.label, value: SUSPENDED, fill: 'bg-status-idle' },
    { key: 'u', label: TENANT_STATUS_LABELS.UNPAID.label, value: UNPAID, fill: 'bg-status-serious' },
  ];
  return summary.total === 0 ? (
    <p className="text-muted-foreground text-sm">등록된 고객사가 없습니다.</p>
  ) : (
    <>
      <ShareBar parts={parts} />
      <dl className="grid grid-cols-[repeat(auto-fit,minmax(56px,1fr))] gap-2 border-t pt-4 text-center">
        {parts.map((p) => (
          <div key={p.key} className="flex flex-col gap-0.5">
            <dt className="text-muted-foreground text-xs whitespace-nowrap">{p.label}</dt>
            <dd className="text-lg font-bold tabular-nums">{p.value}</dd>
          </div>
        ))}
      </dl>
    </>
  );
}

/** 고객사 상태(왼쪽)와 확인 필요(오른쪽)를 한 카드에 둔다(2026-11-02). 위 숫자 카드의 "고객사 · 확인 필요" 짝과 같은 자리다. */
function TenantOverviewChart({
  summary,
  tenants,
  nowMs,
  needCheck,
  apiOverCall,
}: {
  summary: ReturnType<typeof summarizeTenants>;
  tenants: readonly TenantListItem[];
  nowMs: number;
  needCheck: number;
  apiOverCall?: ApiOverCall;
}) {
  return (
    <ChartCard title="고객사 · 확인 필요" className="@3xl/dash:col-span-2">
      <div className="grid gap-6 @xl/chart:grid-cols-2 @xl/chart:gap-0 @xl/chart:divide-x">
        <div className="flex flex-col gap-3 @xl/chart:pr-6">
          <p className="flex items-baseline justify-between text-sm font-semibold">
            고객사 상태 <span className="tabular-nums">{summary.total}곳</span>
          </p>
          <TenantStatusChart summary={summary} />
        </div>
        <div className="flex flex-col gap-3 @xl/chart:pl-6">
          <p className="flex items-baseline justify-between text-sm font-semibold">
            확인 필요 <span className="tabular-nums">{needCheck}곳</span>
          </p>
          <NeedCheckRows tenants={tenants} nowMs={nowMs} apiOverCall={apiOverCall} />
        </div>
      </div>
    </ChartCard>
  );
}

/**
 * 숫자 카드 두 장을 한 상자에 붙인 것. 상자를 가운데에서 나눠 왼쪽 · 오른쪽이 각자 링크다.
 * 마우스를 올리면 상자 테두리가 아니라 그쪽 제목이 강조된다 — 두 링크가 한 상자를 나눠 쓰므로 어느 쪽으로 가는지가 보여야 한다.
 */
function PairTile({
  items,
}: {
  items: readonly [PairItem, PairItem];
}) {
  return (
    <div className="bg-card ring-foreground/10 grid grid-cols-2 divide-x rounded-xl ring-1 @xl/dash:col-span-2">
      {items.map((it) => (
        <Link
          key={it.label}
          href={it.href}
          className="group flex min-w-0 flex-col gap-1.5 px-5 py-4 focus-visible:outline-none"
        >
          <span className="text-muted-foreground group-hover:text-primary group-focus-visible:text-primary flex items-center justify-between gap-2 text-xs font-semibold tracking-wide transition-colors">
            {it.label}
            <ChevronRight aria-hidden className="size-4" />
          </span>
          <span className={cn('text-[30px] leading-[38px] font-bold', it.toneClass)}>{it.value}</span>
          <span className="text-muted-foreground text-xs">{it.note}</span>
        </Link>
      ))}
    </div>
  );
}

interface PairItem {
  label: string;
  value: React.ReactNode;
  note: string;
  href: string;
  /** 문제가 있을 때만 값에 상태 글자색을 칠한다(StatTile의 tone과 같은 색). */
  toneClass?: string;
}

function StaffCountHistogram({ dist }: { dist: StaffCountDistribution }) {
  if (dist.domain === null) return null;
  return (
    <Histogram
      axisLabel="고객사 한 곳의 직원 수(명)"
      domain={dist.domain}
      bins={dist.bins.map((b) => {
        const range = b.min === b.max ? `${b.min}` : `${b.min}-${b.max}`;
        return { key: range, label: range, from: b.min - 0.5, to: b.max + 0.5, value: b.tenants, title: `직원 ${range}명 · 고객사 ${b.tenants}곳` };
      })}
    />
  );
}

function StaffChart({ summary, dist }: { summary: StaffSummary | null; dist: StaffCountDistribution | null }) {
  return (
    <ChartCard
      title="고객사 계정"
      aside={summary === null ? null : <span className="text-sm font-bold tabular-nums">{summary.total}명</span>}
    >
      {summary === null ? (
        <p className="text-muted-foreground text-sm">계정 목록을 불러오지 못했습니다.</p>
      ) : summary.total === 0 ? (
        <p className="text-muted-foreground text-sm">등록된 고객사 계정이 없습니다.</p>
      ) : (
        <>
          {dist !== null && dist.n > 0 ? (
            <ChartSection title={`관제 중 고객사 ${dist.n}곳 · 고객사별 직원 수`}>
              <StaffCountHistogram dist={dist} />
            </ChartSection>
          ) : null}
          <ChartSection title="로그인 상태">
            <ShareBar
              parts={[
                { key: 'in', label: '로그인함', value: summary.loggedIn, fill: 'bg-status-good' },
                { key: 'never', label: '미로그인', value: summary.neverLoggedIn, fill: 'bg-status-warning' },
                { key: 'off', label: '비활성', value: summary.disabled, fill: 'bg-status-idle' },
              ]}
            />
          </ChartSection>
        </>
      )}
    </ChartCard>
  );
}

export function AdminDashboardView({
  tenants,
  users,
  incidents,
  renderedAt,
  platform,
}: {
  tenants: TenantListItem[];
  /** 고객사 직원 목록. `null`은 조회 실패 — 고객사 직원 자리가 그 사실을 적는다. */
  users: UserListItem[] | null;
  /** 인시던트 목록(전 고객). `null`은 조회 실패 — 수동 처리에서 진행 불가 인시던트만 빠지고 그 사실을 적는다. */
  incidents: IncidentListItem[] | null;
  /** 서버가 그린 시각(ISO). 경과 시간의 기준이다. */
  renderedAt: string;
  /** 플랫폼 상태. 중앙 관리 · 조치 권한이 있는 플랫폼 계정에게만 넘어온다(없으면 그 구역을 그리지 않는다). */
  platform?: { status: PlatformStatusResponse | null; error: unknown };
}) {
  const nowMs = Date.parse(renderedAt);
  const summary = summarizeTenants(tenants);
  const issues = manualIssues(tenants, incidents);
  const rows = issueRows(issues, tenants);
  const connectionIssues = rows.filter((r) => r.kind === 'CONNECTION').length;
  const stuckIssues = rows.length - connectionIssues;
  const check = needCheckCounts(tenants, nowMs);
  // API 이상 호출은 플랫폼 상태를 받는 계정에만 있다. 조회 실패면 이름 목록이 null이라 줄을 빼고 그 사실을 적는다.
  const usage = platform?.status?.api_usage;
  const apiOverCall: ApiOverCall | undefined =
    platform === undefined
      ? undefined
      : {
          tenants: usage ? overCallTenants(usage).map((t) => t.tenant_name) : null,
          windowHours: usage?.window_hours ?? 24,
          limit: usage?.threshold_per_hour ?? null,
        };
  const apiOver = apiOverCall?.tenants?.length ?? 0;
  // 한 고객사가 연결 문제와 API 이상 호출에 함께 걸리면 두 번 센다(줄마다 따로 세는 막대와 합이 같게).
  const needCheck = check.FAILED + check.UNREACHABLE + check.UNPAID + apiOver;
  const staff = users === null ? null : staffSummary(tenants, users);

  return (
    // 화면 폭이 아니라 본문이 받은 폭으로 접는다(사이드바 폭 제외) — `@container/dash` (2026-11-03)
    <div className="@container/dash flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-bold tracking-tight">대시보드</h1>
        </div>
        <AutoRefresh renderedAt={renderedAt} />
      </div>

      {platform === undefined ? null : <PlatformStatusSection status={platform.status} error={platform.error} nowMs={nowMs} />}

      {platform === undefined ? null : <h2 className="-mb-3 text-lg font-semibold">고객사 운영</h2>}

      {/* 숫자 카드 순서: 고객사 · 확인 필요(한 상자) → 고객사 계정 → 수동 처리 필요. 아래 그래프도 같은 순서다. */}
      <div className="grid gap-4 @xl/dash:grid-cols-2 @5xl/dash:grid-cols-4">
        <PairTile
          items={[
            {
              label: '고객사',
              value: summary.total,
              note: `관제 중 ${summary.byStatus.ACTIVE} · 대기 ${summary.byStatus.PENDING_CONNECTION} · 정지 ${summary.byStatus.SUSPENDED} · 미납 ${summary.byStatus.UNPAID}`,
              href: TENANTS_PATH,
            },
            {
              label: '확인 필요',
              value: needCheck,
              toneClass: check.FAILED + apiOver > 0 ? 'text-badge-critical-fg' : needCheck > 0 ? 'text-badge-warning-fg' : undefined,
              note: `실패 ${check.FAILED} · 확인 불가 ${check.UNREACHABLE} · 미납 ${check.UNPAID}${
                apiOverCall === undefined ? '' : ` · API 이상 ${apiOver}`
              }`,
              href: TENANTS_PATH,
            },
          ]}
        />
        <StatTile
          label="고객사 계정"
          value={staff === null ? null : staff.total}
          note={staff === null ? '계정 조회 실패' : `미로그인 ${staff.neverLoggedIn} · 비활성 ${staff.disabled}`}
          href={STAFF_PATH}
        />
        <StatTile
          label="수동 처리 필요"
          value={rows.length}
          tone={connectionIssues > 0 ? 'critical' : rows.length > 0 ? 'warning' : undefined}
          note={`고객사 ${issues.length}곳 · 연결 실패 ${connectionIssues} · 진행 불가 ${stuckIssues}`}
          href={ISSUES_PATH}
        />
      </div>

      <div className="grid gap-4 @3xl/dash:grid-cols-2 @6xl/dash:grid-cols-4">
        <TenantOverviewChart summary={summary} tenants={tenants} nowMs={nowMs} needCheck={needCheck} apiOverCall={apiOverCall} />
        <StaffChart summary={staff} dist={users === null ? null : staffCountDistribution(tenants, users)} />
        <IssuesChart
          counts={issueCountsByTenant(issues)}
          connection={connectionIssues}
          stuck={stuckIssues}
          incidentsFailed={incidents === null}
        />
      </div>
    </div>
  );
}
