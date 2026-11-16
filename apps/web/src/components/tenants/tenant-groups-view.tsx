'use client';

// 고객사 그룹(#572 2번, 중앙 관리계정만): 그룹을 만들고 지운다. 고객사를 그룹에 넣고 빼는 것은 고객사 상세의 담당 그룹 칸
// (tenant-group-field.tsx), 관리자 직원계정에게 그룹을 배정하는 것은 계정 관리(staff-view.tsx)가 한다.
// 그룹별 고객사 이름은 고객사 목록(`GET /tenants`)으로 채운다. 목록을 못 읽으면 서버가 센 고객사 수만 보인다.

import { ArrowLeft } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useRef, useState, type FormEvent } from 'react';

import { ErrorState } from '@/components/error-state';
import { INPUT_CLASS } from '@/components/form-classes';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { createGroup, deleteGroup } from '@/lib/api/client';
import { GROUP_NAME_MAX, tenantsByGroup } from '@/lib/tenant-groups';
import type { GroupItem, TenantListItem } from '@/types/api';

export function TenantGroupsView({ groups, tenants }: { groups: GroupItem[]; tenants: TenantListItem[] | null }) {
  const router = useRouter();
  const [name, setName] = useState('');
  const [busy, setBusy] = useState<'create' | 'delete' | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [removing, setRemoving] = useState<GroupItem | null>(null);
  const inFlight = useRef(false);
  const byGroup = tenants === null ? null : tenantsByGroup(tenants);
  const unassigned = byGroup?.get(null) ?? [];

  async function run(kind: 'create' | 'delete', call: () => Promise<unknown>): Promise<boolean> {
    if (inFlight.current) return false;
    inFlight.current = true;
    setBusy(kind);
    setFailure(null);
    try {
      await call();
      router.refresh();
      return true;
    } catch (error) {
      setFailure(error);
      // 그 사이 다른 관리자가 지운 그룹이면 목록을 다시 읽어 지금 상태를 보인다.
      router.refresh();
      return false;
    } finally {
      inFlight.current = false;
      setBusy(null);
    }
  }

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed === '') return;
    if (await run('create', () => createGroup({ name: trimmed }))) setName('');
  }

  async function remove(group: GroupItem) {
    await run('delete', () => deleteGroup(group.group_id));
    setRemoving(null);
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Link href="/tenants" className="text-muted-foreground inline-flex w-fit items-center gap-1 text-sm hover:underline">
          <ArrowLeft aria-hidden className="size-3.5" />
          고객사 목록
        </Link>
        <h1 className="text-2xl font-bold tracking-tight">고객사 그룹</h1>
      </div>

      <form className="flex max-w-md gap-2" onSubmit={create}>
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
          className={INPUT_CLASS}
        />
        <Button type="submit" disabled={busy !== null || name.trim() === ''}>
          {busy === 'create' ? '만드는 중…' : '그룹 만들기'}
        </Button>
      </form>

      {failure !== null ? <ErrorState error={failure} variant="inline" /> : null}

      <section className="bg-card ring-foreground/10 overflow-hidden rounded-xl ring-1">
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow className="bg-secondary hover:bg-secondary">
                <TableHead className="text-foreground-secondary pl-6 text-xs font-semibold">그룹</TableHead>
                <TableHead className="text-foreground-secondary text-xs font-semibold">고객사</TableHead>
                <TableHead className="pr-6">
                  <span className="sr-only">관리</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {groups.map((g) => (
                <TableRow key={g.group_id}>
                  <TableCell className="pl-6 font-bold">{g.name}</TableCell>
                  <TableCell>
                    <TenantNames count={g.tenant_count} tenants={byGroup?.get(g.group_id) ?? null} />
                  </TableCell>
                  <TableCell className="pr-6 text-right">
                    <Button type="button" size="sm" variant="outline" disabled={busy !== null} onClick={() => setRemoving(g)}>
                      삭제
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {groups.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={3} className="text-muted-foreground py-8 text-center">
                    그룹이 없습니다.
                  </TableCell>
                </TableRow>
              ) : null}
              {unassigned.length > 0 ? (
                <TableRow>
                  <TableCell className="text-muted-foreground pl-6">그룹 없음</TableCell>
                  <TableCell>
                    <TenantNames count={unassigned.length} tenants={unassigned} />
                  </TableCell>
                  <TableCell className="pr-6" />
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </div>
      </section>

      {removing !== null ? (
        <Dialog open onOpenChange={(next) => (next || busy !== null ? undefined : setRemoving(null))}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{removing.name} 삭제</DialogTitle>
              <DialogDescription>
                {removing.tenant_count > 0 ? `고객사 ${removing.tenant_count}곳은 담당 그룹이 없어지고, ` : ''}
                이 그룹을 배정받은 관리자 직원계정의 배정도 함께 지워집니다. 되돌릴 수 없습니다.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setRemoving(null)} disabled={busy !== null}>
                취소
              </Button>
              <Button type="button" variant="destructive" disabled={busy !== null} onClick={() => remove(removing)}>
                {busy === 'delete' ? '삭제하는 중…' : '삭제'}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      ) : null}
    </div>
  );
}

/** 한 그룹에서 이름으로 보일 고객사 수. 나머지는 수로만 적는다(이름순 앞부터). */
const NAMES_SHOWN = 12;

function TenantNames({ count, tenants }: { count: number; tenants: TenantListItem[] | null }) {
  if (count === 0) return <span className="text-muted-foreground">없음</span>;
  if (tenants === null) return <span className="tabular-nums">{count}곳</span>;
  const shown = tenants.slice(0, NAMES_SHOWN);
  return (
    <span className="flex flex-wrap gap-x-2 gap-y-1">
      {shown.map((t) => (
        <Link key={t.tenant_id} href={`/tenants/${encodeURIComponent(t.tenant_id)}`} className="hover:underline">
          {t.name}
        </Link>
      ))}
      {tenants.length > shown.length ? (
        <span className="text-muted-foreground">외 {tenants.length - shown.length}곳</span>
      ) : null}
    </span>
  );
}
