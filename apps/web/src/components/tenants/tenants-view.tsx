'use client';

// 고객 목록: 고객을 고르는 화면이다. 위에서부터 숫자 카드 → 상태 그래프 → 목록 표(DESIGN.md §레이아웃).
// 자산 수 · 열린 인시던트 수 · 마지막 연결 확인이 고르는 근거라 목록과 그래프에 둔다(tenants.py).

import { Plus } from 'lucide-react';
import Link from 'next/link';
import { useMemo, useState } from 'react';

import { useSession } from '@/components/auth/session-provider';
import { EmptyState } from '@/components/empty-state';
import { StatGrid, StatTile } from '@/components/stat-tile';
import { CreateTenantDialog } from '@/components/tenants/create-tenant-dialog';
import { TenantStatusBadge, VerificationSummary } from '@/components/tenants/tenant-badges';
import { OpenIncidentBars, StatusComposition, VerificationAgeChart } from '@/components/tenants/tenant-charts';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { canCreateTenant, isUnassignedManager } from '@/lib/roles';
import { summarizeTenants, TENANT_STATUS_LABELS, verificationAges } from '@/lib/tenants';
import { cn } from '@/lib/utils';
import type { TenantListItem, TenantStatus } from '@/types/api';

type Filter = 'ALL' | TenantStatus;

const FILTERS: readonly Filter[] = ['ALL', 'ACTIVE', 'PENDING_CONNECTION', 'SUSPENDED'];

export function TenantsView({
  tenants,
  renderedAt,
}: {
  tenants: TenantListItem[];
  /** 서버가 그린 시각(ISO). 경과 시간을 서버와 브라우저가 같은 기준으로 그리게 넘겨받는다. */
  renderedAt: string;
}) {
  const { session } = useSession();
  const me = session.kind === 'user' ? session.user : null;
  // 고객 추가는 중앙 관리자만 — 고객 담당자와 확인 전 계정에는 보이지 않는다. 실제 차단은 서버 403이다.
  const canCreate = canCreateTenant(me);
  // 목록은 서버가 범위를 정한다(중앙 관리자 전체 · 고객 담당자는 배정된 고객만). 담당 고객이 없는 담당자의 빈 목록은
  // "등록된 고객 없음"이 아니라 배정 없음으로 보인다.
  const unassigned = isUnassignedManager(me);
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState<Filter>('ALL');
  const summary = useMemo(() => summarizeTenants(tenants), [tenants]);
  const ages = useMemo(() => verificationAges(tenants, Date.parse(renderedAt)), [tenants, renderedAt]);
  const rows = filter === 'ALL' ? tenants : tenants.filter((t) => t.status === filter);
  const problems = summary.verificationProblems.failed + summary.verificationProblems.unreachable;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-bold tracking-tight">고객 관리</h1>
          <p className="text-muted-foreground text-sm">
            고객 AWS 계정의 연결 상태와 원격 회수(정지)를 관리합니다. 시각은 모두 KST입니다.
            {me?.platform_role === 'TENANT_MANAGER' ? ' 담당 고객만 표시합니다.' : null}
          </p>
        </div>
        {canCreate ? (
          <Button type="button" className="h-10 px-4" onClick={() => setCreating(true)}>
            <Plus aria-hidden />
            고객 추가
          </Button>
        ) : null}
      </div>

      {tenants.length === 0 ? (
        unassigned ? (
          <EmptyState
            message="담당 고객이 배정되지 않았습니다."
            description="배정 전에는 고객 정보 · 조치 · 고객사 사용자 관리를 이용할 수 없습니다. 중앙 관리자에게 담당 고객 배정을 요청해 주세요."
          />
        ) : canCreate ? (
          <EmptyState
            message="등록된 고객이 없습니다."
            description="고객을 추가하면 온보딩 링크로 고객 계정에 접근 역할을 만들게 됩니다."
          />
        ) : (
          <EmptyState message="표시할 고객이 없습니다." />
        )
      ) : (
        <>
          <StatGrid>
            <StatTile
              label="전체 고객"
              value={summary.total}
              note={`관제 중 ${summary.byStatus.ACTIVE} · 대기 ${summary.byStatus.PENDING_CONNECTION} · 정지 ${summary.byStatus.SUSPENDED}`}
            />
            <StatTile label="관제 중 자산" value={summary.activeAssets} note="관제 중 고객 합계" />
            <StatTile label="열린 인시던트" value={summary.openIncidents} note="전체 고객 합계" />
            <StatTile
              label="연결 확인 문제"
              value={problems}
              tone={summary.verificationProblems.failed > 0 ? 'critical' : problems > 0 ? 'warning' : undefined}
              note={`실패 ${summary.verificationProblems.failed} · 확인 불가 ${summary.verificationProblems.unreachable}`}
            />
          </StatGrid>

          <div className="flex flex-wrap gap-4">
            <VerificationAgeChart
              className="flex-[3_1_480px]"
              ages={ages}
              unchecked={tenants.length - ages.length}
            />
            <div className="flex min-w-0 flex-[2_1_320px] flex-col gap-4">
              <StatusComposition summary={summary} />
              <OpenIncidentBars tenants={tenants} />
            </div>
          </div>

          <section className="bg-card ring-foreground/10 overflow-hidden rounded-xl ring-1">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b px-6 py-4">
              <h2 className="text-base font-bold">고객 목록</h2>
              <div role="group" aria-label="상태 필터" className="flex flex-wrap gap-1.5">
                {FILTERS.map((f) => {
                  const count = f === 'ALL' ? summary.total : summary.byStatus[f];
                  const label = f === 'ALL' ? '전체' : TENANT_STATUS_LABELS[f].label;
                  const on = filter === f;
                  return (
                    <button
                      key={f}
                      type="button"
                      aria-pressed={on}
                      onClick={() => setFilter(f)}
                      className={cn(
                        'h-8 rounded-full border px-3 text-[13px] transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                        on
                          ? 'border-primary bg-accent text-accent-foreground font-semibold'
                          : 'text-foreground-secondary hover:bg-muted',
                      )}
                    >
                      {label} {count}
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="bg-secondary hover:bg-secondary">
                    <TableHead className="text-foreground-secondary pl-6 text-xs font-semibold">이름</TableHead>
                    <TableHead className="text-foreground-secondary text-xs font-semibold">상태</TableHead>
                    <TableHead className="text-foreground-secondary text-xs font-semibold">AWS 계정</TableHead>
                    <TableHead className="text-foreground-secondary text-xs font-semibold">마지막 연결 확인</TableHead>
                    <TableHead className="text-foreground-secondary text-right text-xs font-semibold">자산</TableHead>
                    <TableHead className="text-foreground-secondary pr-6 text-right text-xs font-semibold">
                      열린 인시던트
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((t) => (
                    <TableRow key={t.tenant_id} className="h-13">
                      <TableCell className="pl-6 font-bold">
                        <Link href={`/tenants/${encodeURIComponent(t.tenant_id)}`} className="hover:underline">
                          {t.name}
                        </Link>
                      </TableCell>
                      <TableCell><TenantStatusBadge status={t.status} /></TableCell>
                      <TableCell className="text-foreground-secondary font-mono text-[13px]">{t.aws_account_id}</TableCell>
                      <TableCell><VerificationSummary verification={t.last_verification} align="start" /></TableCell>
                      <TableCell className="text-right tabular-nums">{t.asset_count}</TableCell>
                      <TableCell className="pr-6 text-right font-bold tabular-nums">{t.open_incident_count}</TableCell>
                    </TableRow>
                  ))}
                  {rows.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={6} className="text-muted-foreground py-8 text-center">
                        이 상태의 고객이 없습니다.
                      </TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          </section>
        </>
      )}

      {creating && canCreate ? <CreateTenantDialog onClose={() => setCreating(false)} /> : null}
    </div>
  );
}
