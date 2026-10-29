'use client';

// 고객 목록: 고객을 고르고 등록하는 화면이다. 자산 수 · 열린 인시던트 수 · 마지막 연결 확인이 고르는 근거라 목록에 둔다(tenants.py).
// 숫자 카드와 상태 그래프 3종은 2026-10-29에 관리자 대시보드로 옮겼다(admin-dashboard-view.tsx) — 같은 숫자를 두 화면에
// 두지 않는다. 그래서 이 화면은 목록 표 하나다.

import { Plus } from 'lucide-react';
import Link from 'next/link';
import { useMemo, useState } from 'react';

import { EmptyState } from '@/components/empty-state';
import { CreateTenantDialog } from '@/components/tenants/create-tenant-dialog';
import { TenantStatusBadge, VerificationSummary } from '@/components/tenants/tenant-badges';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { INPUT_CLASS } from '@/components/form-classes';
import { matchesTenantQuery, summarizeTenants, TENANT_STATUS_LABELS } from '@/lib/tenants';
import { cn } from '@/lib/utils';
import type { TenantListItem, TenantStatus } from '@/types/api';

type Filter = 'ALL' | TenantStatus;

const FILTERS: readonly Filter[] = ['ALL', 'ACTIVE', 'PENDING_CONNECTION', 'SUSPENDED'];

export function TenantsView({ tenants }: { tenants: TenantListItem[] }) {
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState<Filter>('ALL');
  const [query, setQuery] = useState('');
  const summary = useMemo(() => summarizeTenants(tenants), [tenants]);
  const rows = tenants.filter((t) => (filter === 'ALL' || t.status === filter) && matchesTenantQuery(t, query));

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-bold tracking-tight">고객사 관리</h1>
          <p className="text-muted-foreground text-sm">
            고객사 AWS 계정의 연결 상태와 원격 회수(정지)를 관리합니다. 시각은 모두 KST입니다.
          </p>
        </div>
        <Button type="button" className="h-10 px-4" onClick={() => setCreating(true)}>
          <Plus aria-hidden />
          고객사 추가
        </Button>
      </div>

      {tenants.length === 0 ? (
        <EmptyState
          message="등록된 고객사가 없습니다."
          description="고객사를 추가하면 온보딩 링크로 고객사 AWS 계정에 접근 역할을 만들게 됩니다."
        />
      ) : (
        <>
          <section className="bg-card ring-foreground/10 overflow-hidden rounded-xl ring-1">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b px-6 py-4">
              <div className="flex flex-wrap items-center gap-3">
                <h2 className="text-base font-bold">고객사 목록</h2>
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  aria-label="고객사 검색"
                  placeholder="이름 · 고객사 ID · AWS 계정 검색"
                  className={cn(INPUT_CLASS, 'h-8 w-64')}
                />
                {query.trim() !== '' ? (
                  <span className="text-muted-foreground text-xs" aria-live="polite">
                    {rows.length}곳
                  </span>
                ) : null}
              </div>
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
                        {query.trim() === '' ? '이 상태의 고객사가 없습니다.' : '검색어와 맞는 고객사가 없습니다.'}
                      </TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          </section>
        </>
      )}

      {creating ? <CreateTenantDialog onClose={() => setCreating(false)} /> : null}
    </div>
  );
}
