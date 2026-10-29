// 플랫폼 운영자 대시보드(2026-10-29). 운영자가 여기서 알아야 할 것은 둘이다(lib/admin-dashboard.ts 머리말).
//   1. 수동 처리가 필요한 문제 — 플랫폼 자동 기능으로 풀리지 않아 운영자가 고객 계정 일을 직접 처리해야 하는 것
//   2. 고객사별 관제 직원 — 어느 고객사에 어떤 로그인 계정이 있는지
// 고객 하나를 들여다보는 구성(자산 띠 · 위협 추이 · AI 제안)은 관제 화면과 고객 웹의 몫이다.
//
// 대시보드는 숫자와 그래프만 둔다. 지금 어떤 상황인지 한눈에 읽히게 하고, 한 건 한 줄 목록은 숫자 카드와 그래프 아래 링크로
// 넘어간 상세 화면에서 본다(수동 처리 문제 `ISSUES_PATH` · 고객사 목록 · 고객사별 직원 `STAFF_PATH`).
//   숫자 카드 4개(고객사 · 고객사 직원 · 연결 확인 필요 · 수동 처리 필요) → 그래프 4장(고객사 상태 · 고객사 직원 · 연결 상태 · 수동 처리 문제)
// 고객이 수백 곳으로 늘어도 높이가 고정된다: 그래프는 분류별 건수와 상위 5곳만 그린다.
//
// 훅이 없어 `'use client'`를 붙이지 않는다.

import { BarRows, ChartSection, Histogram, MoreLink, ShareBar } from '@/components/admin-dashboard/chart-parts';
import { AutoRefresh } from '@/components/auto-refresh';
import { ChartCard } from '@/components/chart-card';
import { StatGrid, StatTile } from '@/components/stat-tile';
import { ConnectionStatusChart } from '@/components/tenants/tenant-charts';
import {
  connectionPoints,
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
import { ROLE_LABELS } from '@/lib/user-admin';
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
      description="관제 중 고객사 · 문제 건수"
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
      <MoreLink href={ISSUES_PATH}>문제 전체 보기</MoreLink>
    </ChartCard>
  );
}

function TenantStatusChart({
  summary,
  withoutStaff,
}: {
  summary: ReturnType<typeof summarizeTenants>;
  /** 관제 중인데 직원이 한 명도 없는 고객사 수. 직원 조회 실패면 null이고, 그때는 관제 중을 나누지 않는다. */
  withoutStaff: number | null;
}) {
  const { ACTIVE, PENDING_CONNECTION, SUSPENDED } = summary.byStatus;
  const noStaff = withoutStaff ?? 0;
  // 관제 중을 직원 유무로 나눈다. 네 칸의 합이 전체 고객사 수다.
  const parts = [
    { key: 'a', label: TENANT_STATUS_LABELS.ACTIVE.label, value: ACTIVE - noStaff, fill: 'bg-status-good' },
    ...(withoutStaff === null ? [] : [{ key: 'n', label: '직원 없음', value: noStaff, fill: 'bg-status-serious' }]),
    { key: 'p', label: TENANT_STATUS_LABELS.PENDING_CONNECTION.label, value: PENDING_CONNECTION, fill: 'bg-status-warning' },
    { key: 's', label: TENANT_STATUS_LABELS.SUSPENDED.label, value: SUSPENDED, fill: 'bg-status-idle' },
  ];
  return (
    <ChartCard
      title="고객사 상태"
      description={withoutStaff === null ? '등록된 고객사 전체 · 상태별 비율(직원 조회 실패)' : '등록된 고객사 전체 · 상태별 비율'}
      aside={<span className="text-sm font-bold tabular-nums">{summary.total}곳</span>}
    >
      {summary.total === 0 ? (
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
      )}
      <MoreLink href={TENANTS_PATH}>고객사 목록</MoreLink>
    </ChartCard>
  );
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
      title="고객사 직원"
      description="고객사 직원 계정 · 사람 수"
      aside={summary === null ? null : <span className="text-sm font-bold tabular-nums">{summary.total}명</span>}
    >
      {summary === null ? (
        <p className="text-muted-foreground text-sm">직원 목록을 불러오지 못했습니다.</p>
      ) : summary.total === 0 ? (
        <p className="text-muted-foreground text-sm">등록된 고객사 직원이 없습니다.</p>
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
          <ChartSection title="권한">
            <BarRows
              labelWidth={96}
              rows={(['ADMIN', 'OPERATOR', 'VIEWER'] as const).map((r) => ({ key: r, label: ROLE_LABELS[r], value: summary.byRole[r] }))}
            />
          </ChartSection>
        </>
      )}
      <MoreLink href={STAFF_PATH}>고객사별 직원 보기</MoreLink>
    </ChartCard>
  );
}

export function AdminDashboardView({
  tenants,
  users,
  incidents,
  renderedAt,
}: {
  tenants: TenantListItem[];
  /** 고객사 직원 목록. `null`은 조회 실패 — 고객사 직원 자리가 그 사실을 적는다. */
  users: UserListItem[] | null;
  /** 인시던트 목록(전 고객). `null`은 조회 실패 — 수동 처리에서 진행 불가 인시던트만 빠지고 그 사실을 적는다. */
  incidents: IncidentListItem[] | null;
  /** 서버가 그린 시각(ISO). 경과 시간의 기준이다. */
  renderedAt: string;
}) {
  const nowMs = Date.parse(renderedAt);
  const summary = summarizeTenants(tenants);
  const issues = manualIssues(tenants, incidents);
  const rows = issueRows(issues, tenants);
  const connectionIssues = rows.filter((r) => r.kind === 'CONNECTION').length;
  const stuckIssues = rows.length - connectionIssues;
  const points = connectionPoints(tenants, nowMs);
  const stateCount = (s: string) => points.filter((p) => p.state === s).length;
  const needCheck = stateCount('FAILED') + stateCount('UNREACHABLE') + stateCount('STALE');
  const staff = users === null ? null : staffSummary(tenants, users);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-bold tracking-tight">대시보드</h1>
          <p className="text-muted-foreground text-sm">
            고객사 운영 현황을 숫자와 그래프로 봅니다. 숫자 카드나 그래프 아래 링크를 누르면 목록으로 넘어갑니다.
          </p>
        </div>
        <AutoRefresh renderedAt={renderedAt} />
      </div>

      <StatGrid>
        <StatTile
          label="고객사"
          value={summary.total}
          note={`관제 중 ${summary.byStatus.ACTIVE} · 대기 ${summary.byStatus.PENDING_CONNECTION} · 정지 ${summary.byStatus.SUSPENDED}`}
          href={TENANTS_PATH}
        />
        <StatTile
          label="고객사 직원"
          value={staff === null ? null : staff.total}
          note={staff === null ? '직원 조회 실패' : `미로그인 ${staff.neverLoggedIn} · 비활성 ${staff.disabled}`}
          href={STAFF_PATH}
        />
        <StatTile
          label="연결 확인 필요"
          value={needCheck}
          tone={stateCount('FAILED') > 0 ? 'critical' : needCheck > 0 ? 'warning' : undefined}
          note={`실패 ${stateCount('FAILED')} · 확인 불가 ${stateCount('UNREACHABLE')} · 오래됨 ${stateCount('STALE')}`}
          href={TENANTS_PATH}
        />
        <StatTile
          label="수동 처리 필요"
          value={rows.length}
          tone={connectionIssues > 0 ? 'critical' : rows.length > 0 ? 'warning' : undefined}
          note={`고객사 ${issues.length}곳 · 연결 실패 ${connectionIssues} · 진행 불가 ${stuckIssues}`}
          href={ISSUES_PATH}
        />
      </StatGrid>

      {/* 숫자 카드와 같은 순서다: 고객사 → 고객사 직원 → 연결 상태 → 수동 처리 문제. */}
      <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-4">
        <TenantStatusChart summary={summary} withoutStaff={staff === null ? null : staff.activeTenantsWithoutStaff} />
        <StaffChart summary={staff} dist={users === null ? null : staffCountDistribution(tenants, users)} />
        <ConnectionStatusChart tenants={tenants} nowMs={nowMs} />
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
