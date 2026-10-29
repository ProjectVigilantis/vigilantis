'use client';

// 고객사별 관제 직원 표. 고객사 하나 = 한 줄이라 고객이 수백 곳이어도 표 안에서 스크롤된다. 줄에는 역할별 · 비활성 ·
// 미로그인 수와 가장 최근 로그인을 두고, 누가 있는지(이메일)는 줄을 펼치면 보인다. 위 검색칸은 고객사 이름과 이메일을 함께 찾는다.
// 분류 칩은 운영자가 손볼 줄만 남긴다: 관제 중인데 직원이 없는 고객사, 초대 후 로그인하지 않은 직원이 있는 고객사, 비활성 직원이 있는 고객사.

import { ChevronDown } from 'lucide-react';
import Link from 'next/link';
import { Fragment, useState } from 'react';

import { INPUT_CLASS } from '@/components/form-classes';
import { TenantStatusBadge } from '@/components/tenants/tenant-badges';
import { formatAge, type StaffRow } from '@/lib/admin-dashboard';
import { ROLE_LABELS } from '@/lib/user-admin';
import { cn } from '@/lib/utils';
import type { UserListItem } from '@/types/api';

const rowKey = (r: StaffRow) => r.tenantId;

type Category = 'ALL' | 'NO_STAFF' | 'NEVER_LOGGED_IN' | 'DISABLED';

const CATEGORIES: readonly { value: Category; label: string; match: (r: StaffRow) => boolean }[] = [
  { value: 'ALL', label: '전체', match: () => true },
  { value: 'NO_STAFF', label: '직원 없음', match: (r) => r.tenantStatus === 'ACTIVE' && r.users.length === 0 },
  { value: 'NEVER_LOGGED_IN', label: '미로그인 있음', match: (r) => r.neverLoggedIn > 0 },
  { value: 'DISABLED', label: '비활성 있음', match: (r) => r.disabled > 0 },
];

function loginText(user: UserListItem, nowMs: number): string {
  if (user.status === 'DISABLED') return '비활성';
  return user.last_login_at === null ? '로그인 기록 없음' : `${formatAge(user.last_login_at, nowMs)} 로그인`;
}

function Count({ n, tone }: { n: number; tone?: 'warn' }) {
  return (
    <td className={cn('px-3 py-2 text-right tabular-nums', n === 0 ? 'text-muted-foreground' : tone === 'warn' && 'text-badge-warning-fg font-semibold')}>
      {n}
    </td>
  );
}

export function StaffTable({ rows, nowMs }: { rows: readonly StaffRow[]; nowMs: number }) {
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState<Category>('ALL');
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const q = query.trim().toLowerCase();
  const inCategory = CATEGORIES.find((c) => c.value === category)!.match;
  const shown = rows.filter(
    (r) => inCategory(r) && (q === '' || r.name.toLowerCase().includes(q) || r.users.some((u) => u.email.toLowerCase().includes(q))),
  );
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div role="group" aria-label="고객사 분류" className="flex flex-wrap gap-1.5">
          {CATEGORIES.map(({ value, label, match }) => {
            const on = category === value;
            return (
              <button
                key={value}
                type="button"
                aria-pressed={on}
                onClick={() => setCategory(value)}
                className={cn(
                  'focus-visible:ring-ring h-8 rounded-full border px-3 text-[13px] transition-colors focus-visible:ring-2 focus-visible:outline-none',
                  on ? 'border-primary bg-accent text-accent-foreground font-semibold' : 'text-foreground-secondary hover:bg-muted',
                )}
              >
                {label} <span className="tabular-nums">{rows.filter(match).length}</span>
              </button>
            );
          })}
        </div>
        <label className="flex w-full max-w-xs items-center gap-2 text-sm">
          <span className="text-muted-foreground shrink-0">검색</span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="고객사 이름 · 이메일"
            className={INPUT_CLASS}
          />
        </label>
      </div>
      <p className="text-muted-foreground -mt-1 text-xs" aria-live="polite">
        {shown.length === rows.length ? `${rows.length}곳` : `${shown.length} / ${rows.length}곳`}
      </p>
      <div className="max-h-96 overflow-auto rounded-lg border">
        <table className="w-full min-w-[640px] text-sm">
          <thead className="bg-secondary text-foreground-secondary sticky top-0 z-10 text-xs">
            <tr>
              <th scope="col" className="px-3 py-2 text-left font-semibold">고객사</th>
              <th scope="col" className="px-3 py-2 text-right font-semibold">{ROLE_LABELS.ADMIN}</th>
              <th scope="col" className="px-3 py-2 text-right font-semibold">{ROLE_LABELS.OPERATOR}</th>
              <th scope="col" className="px-3 py-2 text-right font-semibold">{ROLE_LABELS.VIEWER}</th>
              <th scope="col" className="px-3 py-2 text-right font-semibold">비활성</th>
              <th scope="col" className="px-3 py-2 text-right font-semibold">미로그인</th>
              <th scope="col" className="px-3 py-2 text-right font-semibold">최근 로그인</th>
              <th scope="col" className="w-10"><span className="sr-only">펼치기</span></th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {shown.map((r) => {
              const key = rowKey(r);
              const expanded = open.has(key);
              const href = `/users?tenant=${encodeURIComponent(r.tenantId)}`;
              return (
                <Fragment key={key}>
                  <tr className="hover:bg-muted">
                    <td className="px-3 py-2">
                      <span className="flex flex-wrap items-center gap-2">
                        <Link href={href} className="font-semibold hover:underline">{r.name}</Link>
                        {r.tenantStatus !== null ? <TenantStatusBadge status={r.tenantStatus} /> : null}
                      </span>
                    </td>
                    <Count n={r.byRole.ADMIN} />
                    <Count n={r.byRole.OPERATOR} />
                    <Count n={r.byRole.VIEWER} />
                    <Count n={r.disabled} />
                    <Count n={r.neverLoggedIn} tone="warn" />
                    <td className="text-muted-foreground px-3 py-2 text-right text-xs whitespace-nowrap">
                      {r.lastLoginAt === null ? '—' : formatAge(r.lastLoginAt, nowMs)}
                    </td>
                    <td className="pr-2">
                      {r.users.length > 0 ? (
                        <button
                          type="button"
                          aria-expanded={expanded}
                          aria-label={`${r.name} 고객사 직원 ${expanded ? '접기' : '펼치기'}`}
                          onClick={() => toggle(key)}
                          className="hover:bg-accent focus-visible:ring-ring/50 rounded-md p-1 focus-visible:ring-2 focus-visible:outline-none"
                        >
                          <ChevronDown aria-hidden className={cn('size-4 transition-transform', expanded && 'rotate-180')} />
                        </button>
                      ) : null}
                    </td>
                  </tr>
                  {expanded ? (
                    <tr className="bg-muted/40">
                      <td colSpan={8} className="px-3 py-2">
                        <ul className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
                          {r.users.map((u) => (
                            <li key={u.user_id} className="flex items-baseline justify-between gap-3 text-[13px]">
                              <span className="truncate" title={u.email}>{u.email}</span>
                              <span className="text-muted-foreground shrink-0 text-xs">
                                {ROLE_LABELS[u.role]} · {loginText(u, nowMs)}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
            {shown.length === 0 ? (
              <tr>
                <td colSpan={8} className="text-muted-foreground px-3 py-6 text-center">조건과 맞는 고객사가 없습니다.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}
