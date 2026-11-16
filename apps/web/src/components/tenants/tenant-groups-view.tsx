'use client';

// 고객사 그룹(#572 2번, 중앙 관리계정만): 그룹(지사)을 만들고, 고객사마다 어느 그룹에 속하는지 정한다.
// 배정은 고객사 한 곳씩 `PUT /tenants/{id}/group`이다. 고객사 상세의 담당 그룹 칸(tenant-group-field.tsx)도 같은 호출이다.
// 관리자 직원계정에게 그룹을 배정하는 것은 계정 관리(staff-view.tsx)가 한다.
// 그룹 삭제는 두지 않는다(2026-11-16 PM 결정: 지사를 지울 일이 없다). 서버 `DELETE /groups/{id}`는 그대로 있다.

import { ArrowLeft } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { startTransition, useRef, useState, type FormEvent } from 'react';

import { ErrorState } from '@/components/error-state';
import { INPUT_CLASS } from '@/components/form-classes';
import { TenantStatusBadge } from '@/components/tenants/tenant-badges';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ApiError, createGroup, setTenantGroup } from '@/lib/api/client';
import { assignmentRows, GROUP_FILTER_ALL, GROUP_FILTER_NONE, GROUP_NAME_MAX } from '@/lib/tenant-groups';
import { matchesTenantQuery } from '@/lib/tenants';
import { cn } from '@/lib/utils';
import type { GroupItem, TenantListItem } from '@/types/api';

export function TenantGroupsView({ groups, tenants }: { groups: GroupItem[]; tenants: TenantListItem[] | null }) {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Link href="/tenants" className="text-muted-foreground inline-flex w-fit items-center gap-1 text-sm hover:underline">
          <ArrowLeft aria-hidden className="size-3.5" />
          고객사 목록
        </Link>
        <h1 className="text-2xl font-bold tracking-tight">고객사 그룹</h1>
      </div>
      <GroupList groups={groups} />
      {tenants === null ? (
        <section className="bg-card ring-foreground/10 rounded-xl px-6 py-4 ring-1">
          <h2 className="text-base font-bold">고객사 배정</h2>
          <p role="alert" className="text-destructive mt-2 text-sm">
            고객사 목록을 불러오지 못해 배정을 보이지 못했습니다.
          </p>
        </section>
      ) : (
        <Assignments groups={groups} tenants={tenants} />
      )}
    </div>
  );
}

function GroupList({ groups }: { groups: GroupItem[] }) {
  const router = useRouter();
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const inFlight = useRef(false);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed === '' || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setFailure(null);
    try {
      await createGroup({ name: trimmed });
      setName('');
      router.refresh();
    } catch (error) {
      setFailure(error);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="bg-card ring-foreground/10 overflow-hidden rounded-xl ring-1">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b px-6 py-4">
        <h2 className="text-base font-bold">그룹 {groups.length}</h2>
        <form className="flex gap-2" onSubmit={create}>
          <label htmlFor="group-name" className="sr-only">
            새 그룹 이름
          </label>
          <input
            id="group-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={GROUP_NAME_MAX}
            placeholder="새 그룹 이름"
            autoComplete="off"
            className={cn(INPUT_CLASS, 'h-8 w-56')}
          />
          <Button type="submit" size="sm" disabled={busy || name.trim() === ''}>
            {busy ? '만드는 중…' : '그룹 만들기'}
          </Button>
        </form>
      </div>
      {failure !== null ? (
        <div className="border-b px-6 py-3">
          <ErrorState error={failure} variant="inline" />
        </div>
      ) : null}
      {groups.length === 0 ? (
        <p className="text-muted-foreground px-6 py-6 text-center text-sm">그룹이 없습니다.</p>
      ) : (
        <ul className="flex flex-wrap gap-2 px-6 py-4">
          {groups.map((g) => (
            <li key={g.group_id} className="bg-secondary flex items-center gap-2 rounded-full px-3 py-1 text-sm">
              <span className="font-semibold">{g.name}</span>
              <span className="text-muted-foreground tabular-nums">{g.tenant_count}곳</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** 저장 중인 행의 값. 서버 값이 다시 내려오면 지운다. */
type Pending = Record<string, string | null>;

function without<V>(record: Record<string, V>, key: string): Record<string, V> {
  const next = { ...record };
  delete next[key];
  return next;
}

function Assignments({ groups, tenants }: { groups: GroupItem[]; tenants: TenantListItem[] }) {
  const router = useRouter();
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<string>(GROUP_FILTER_ALL);
  const [pending, setPending] = useState<Pending>({});
  const [failed, setFailed] = useState<Record<string, string>>({});

  const groupOf = (t: TenantListItem) => (t.tenant_id in pending ? pending[t.tenant_id] : t.group_id);
  const rows = assignmentRows(tenants, groupOf, filter, (t) => matchesTenantQuery(t, query));
  const unassignedCount = tenants.filter((t) => groupOf(t) === null).length;

  async function assign(tenant: TenantListItem, value: string) {
    const groupId = value === '' ? null : value;
    const id = tenant.tenant_id;
    setPending((p) => ({ ...p, [id]: groupId }));
    setFailed((f) => without(f, id));
    try {
      await setTenantGroup(id, groupId);
    } catch (error) {
      setFailed((f) => ({
        ...f,
        [id]: error instanceof ApiError ? error.message : '그룹을 바꾸지 못했습니다.',
      }));
    }
    // 성공이든 실패든 서버 값으로 돌린다. 고른 그룹이 그 사이 지워졌으면(GROUP_NOT_FOUND) 새 그룹 목록도 함께 온다.
    // 같은 전환 안에서 지우므로 새 값이 내려오기 전에 옛 값이 잠깐 보이지 않는다.
    startTransition(() => {
      setPending((p) => without(p, id));
      router.refresh();
    });
  }

  return (
    <section className="bg-card ring-foreground/10 overflow-hidden rounded-xl ring-1">
      <div className="flex flex-wrap items-center gap-3 border-b px-6 py-4">
        <h2 className="text-base font-bold">고객사 배정</h2>
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="고객사 검색"
          placeholder="이름 · 고객사 ID · AWS 계정 검색"
          className={cn(INPUT_CLASS, 'h-8 w-64')}
        />
        <label htmlFor="group-filter" className="sr-only">
          그룹으로 거르기
        </label>
        <select
          id="group-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className={cn(INPUT_CLASS, 'h-8 w-44')}
        >
          <option value={GROUP_FILTER_ALL}>전체 {tenants.length}곳</option>
          <option value={GROUP_FILTER_NONE}>그룹 없음 {unassignedCount}곳</option>
          {groups.map((g) => (
            <option key={g.group_id} value={g.group_id}>
              {g.name}
            </option>
          ))}
        </select>
        <span className="text-muted-foreground text-xs" aria-live="polite">
          {rows.length}곳
        </span>
      </div>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="bg-secondary hover:bg-secondary">
              <TableHead className="text-foreground-secondary pl-6 text-xs font-semibold">고객사</TableHead>
              <TableHead className="text-foreground-secondary text-xs font-semibold">상태</TableHead>
              <TableHead className="text-foreground-secondary text-xs font-semibold">AWS 계정</TableHead>
              <TableHead className="text-foreground-secondary pr-6 text-xs font-semibold">그룹</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((t) => {
              const value = groupOf(t);
              const saving = t.tenant_id in pending;
              const error = failed[t.tenant_id];
              return (
                <TableRow key={t.tenant_id}>
                  <TableCell className="pl-6 font-bold">
                    <Link href={`/tenants/${encodeURIComponent(t.tenant_id)}`} className="hover:underline">
                      {t.name}
                    </Link>
                  </TableCell>
                  <TableCell><TenantStatusBadge status={t.status} /></TableCell>
                  <TableCell className="text-foreground-secondary font-mono text-[13px]">{t.aws_account_id}</TableCell>
                  <TableCell className="pr-6">
                    <span className="flex flex-col gap-1">
                      <select
                        aria-label={`${t.name} 그룹`}
                        value={value ?? ''}
                        disabled={saving}
                        onChange={(e) => assign(t, e.target.value)}
                        className={cn(INPUT_CLASS, 'h-8 w-48', value === null && 'text-muted-foreground')}
                      >
                        <option value="">그룹 없음</option>
                        {groups.map((g) => (
                          <option key={g.group_id} value={g.group_id}>
                            {g.name}
                          </option>
                        ))}
                      </select>
                      {error !== undefined ? (
                        <span role="alert" className="text-destructive text-xs">
                          {error}
                        </span>
                      ) : null}
                    </span>
                  </TableCell>
                </TableRow>
              );
            })}
            {rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={4} className="text-muted-foreground py-8 text-center">
                  {tenants.length === 0 ? '등록된 고객사가 없습니다.' : '조건과 맞는 고객사가 없습니다.'}
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}
