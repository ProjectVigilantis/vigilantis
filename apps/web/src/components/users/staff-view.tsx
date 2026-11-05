'use client';

// 중앙 관리계정의 계정 관리 화면: 관리자 직원계정(플랫폼 계정) 목록이다(`GET /staff`, 중앙 관리계정 전용).
// 고객사 계정은 고객사 상세의 고객사 계정 패널에서 다룬다(components/tenants/tenant-users-panel.tsx).
//
// 개인정보 보호를 위해 처음에는 아무 계정도 보이지 않고, 이메일로 검색한 결과만 보인다.
// 중앙 관리계정 자신은 목록에서 뺀다. 최종 결정자라 스스로를 관리할 일이 없고, 서버도 자기 변경을 거절한다.

import { useRouter } from 'next/navigation';
import { useEffect, useState, type FormEvent } from 'react';

import { useSession } from '@/components/auth/session-provider';
import { INPUT_CLASS } from '@/components/form-classes';
import { TenantStatusBadge } from '@/components/tenants/tenant-badges';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ActionDialog, HistorySheet } from '@/components/users/users-view';
import { changeStaffStatus, getStaffEvents, getStaffTenants, replaceStaffTenants } from '@/lib/api/client';
import { ACCOUNT_KIND_LABELS } from '@/lib/roles';
import { STATUS_LABELS, formatUserTime } from '@/lib/user-admin';
import type { StaffItem, TenantListItem } from '@/types/api';

type Panel = { kind: 'status' | 'history' | 'tenants'; user: StaffItem };

export function StaffView({
  staff,
  tenants,
}: {
  staff: StaffItem[];
  /** 담당 고객사 배정 창의 선택지. `null`은 고객사 목록 조회 실패다. */
  tenants: TenantListItem[] | null;
}) {
  const router = useRouter();
  const { session } = useSession();
  const myId = session.kind === 'user' ? session.user.user_id : null;
  const others = staff.filter((s) => s.user_id !== myId);

  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState('');
  const [panel, setPanel] = useState<Panel | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const needle = query.trim().toLowerCase();
  const results = needle === '' ? [] : others.filter((s) => s.email.toLowerCase().includes(needle));

  function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setDone(null);
    setQuery(draft);
  }

  function finished(message: string) {
    setPanel(null);
    setDone(message);
    router.refresh();
  }

  const close = () => setPanel(null);
  const emailOf = (userId: string) => staff.find((s) => s.user_id === userId)?.email ?? userId;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold">계정 관리</h1>
      </div>

      <form role="search" className="flex max-w-md gap-2" onSubmit={search}>
        <label htmlFor="staff-search" className="sr-only">
          이메일 검색
        </label>
        <input
          id="staff-search"
          type="search"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="이메일로 검색"
          autoComplete="off"
          className={INPUT_CLASS}
        />
        <Button type="submit">검색</Button>
      </form>

      {done ? (
        <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm">
          {done}
        </p>
      ) : null}

      {needle === '' ? (
        null
      ) : results.length === 0 ? (
        <p role="status" className="text-sm text-muted-foreground">
          &lsquo;{query.trim()}&rsquo;에 맞는 관리자 직원계정이 없습니다.
        </p>
      ) : (
        <>
          <p role="status" className="text-sm text-muted-foreground">
            검색 결과 {results.length}명
          </p>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>이메일</TableHead>
                <TableHead>계정 종류</TableHead>
                <TableHead>상태</TableHead>
                <TableHead>마지막 로그인</TableHead>
                <TableHead>
                  <span className="sr-only">관리</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {results.map((user) => (
                <TableRow key={user.user_id}>
                  <TableCell>{user.email}</TableCell>
                  <TableCell>{ACCOUNT_KIND_LABELS[user.platform_role]}</TableCell>
                  <TableCell>{STATUS_LABELS[user.status]}</TableCell>
                  <TableCell>
                    {user.last_login_at === null ? '로그인 기록 없음' : formatUserTime(user.last_login_at)}
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-1">
                      {/* 담당 배정은 관리자 직원계정만 받는다. 중앙 관리계정은 전체 고객사를 본다(서버도 409로 거절). */}
                      {user.platform_role === 'TENANT_MANAGER' ? (
                        <Button type="button" size="sm" variant="outline" onClick={() => setPanel({ kind: 'tenants', user })}>
                          담당 고객사
                        </Button>
                      ) : null}
                      <Button type="button" size="sm" variant="outline" onClick={() => setPanel({ kind: 'status', user })}>
                        {user.status === 'ACTIVE' ? '비활성화' : '활성화'}
                      </Button>
                      <Button type="button" size="sm" variant="ghost" onClick={() => setPanel({ kind: 'history', user })}>
                        이력
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}

      {panel?.kind === 'status' ? (
        <ActionDialog
          action="status"
          title={panel.user.status === 'ACTIVE' ? '계정 비활성화' : '계정 활성화'}
          description={
            panel.user.status === 'ACTIVE'
              ? `${panel.user.email} 계정은 로그인할 수 없게 되고, 사용 중인 로그인 세션도 더 이상 쓸 수 없습니다.`
              : `${panel.user.email} 계정이 다시 로그인할 수 있게 됩니다.`
          }
          confirmLabel={panel.user.status === 'ACTIVE' ? '비활성화' : '활성화'}
          destructive={panel.user.status === 'ACTIVE'}
          run={() => changeStaffStatus(panel.user.user_id, panel.user.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE')}
          onClose={close}
          onDone={() => finished('계정 상태를 바꿨습니다.')}
        />
      ) : null}

      {panel?.kind === 'tenants' ? (
        <TenantAssignDialog
          user={panel.user}
          tenants={tenants}
          onClose={close}
          onDone={() => finished(`${panel.user.email}의 담당 고객사를 바꿨습니다.`)}
        />
      ) : null}

      {panel?.kind === 'history' ? (
        <HistorySheet user={panel.user} emailOf={emailOf} onClose={close} load={getStaffEvents} />
      ) : null}
    </div>
  );
}

type Assigned = { kind: 'loading' } | { kind: 'error' } | { kind: 'ok'; ids: ReadonlySet<string> };

/**
 * 담당 고객사 배정 창: 지금 배정을 읽어 체크한 채로 열고, 저장하면 전체를 교체한다(`PUT /staff/{id}/tenants`).
 * 고객사가 많으므로 이름 · ID로 거르고, 배정된 고객사를 위에 둔다. 바뀐 것이 없으면 저장을 막는다(서버는 409).
 */
function TenantAssignDialog({
  user,
  tenants,
  onClose,
  onDone,
}: {
  user: StaffItem;
  tenants: TenantListItem[] | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [assigned, setAssigned] = useState<Assigned>({ kind: 'loading' });
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [filter, setFilter] = useState('');

  useEffect(() => {
    let alive = true;
    getStaffTenants(user.user_id)
      .then((res) => {
        if (!alive) return;
        const ids = new Set(res.tenant_ids);
        setAssigned({ kind: 'ok', ids });
        setSelected(ids);
      })
      .catch(() => alive && setAssigned({ kind: 'error' }));
    return () => {
      alive = false;
    };
  }, [user.user_id]);

  const original = assigned.kind === 'ok' ? assigned.ids : null;
  const changed =
    original !== null && (original.size !== selected.size || [...selected].some((id) => !original.has(id)));
  const needle = filter.trim().toLowerCase();
  const options = (tenants ?? [])
    .filter((t) => needle === '' || t.name.toLowerCase().includes(needle) || t.tenant_id.toLowerCase().includes(needle))
    .sort((a, b) => Number(original?.has(b.tenant_id) ?? 0) - Number(original?.has(a.tenant_id) ?? 0) || a.name.localeCompare(b.name, 'ko'));

  function toggle(id: string, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  return (
    <ActionDialog
      action="role"
      title="담당 고객사 배정"
      description={user.email}
      confirmLabel="저장"
      disabled={!changed}
      run={() => replaceStaffTenants(user.user_id, [...selected])}
      onClose={onClose}
      onDone={onDone}
    >
      {tenants === null ? (
        <p role="alert" className="text-sm text-destructive">
          고객사 목록을 불러오지 못했습니다. 화면을 새로고침해 주세요.
        </p>
      ) : assigned.kind === 'loading' ? (
        <p className="text-sm text-muted-foreground">지금 배정을 불러오는 중…</p>
      ) : assigned.kind === 'error' ? (
        <p role="alert" className="text-sm text-destructive">
          지금 배정을 불러오지 못했습니다. 창을 닫고 다시 열어 주세요.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-2">
            <label htmlFor="assign-filter" className="sr-only">
              고객사 거르기
            </label>
            <input
              id="assign-filter"
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="고객사 이름 또는 ID"
              autoComplete="off"
              className={INPUT_CLASS}
            />
            <span className="shrink-0 text-xs text-muted-foreground" aria-live="polite">
              {selected.size}곳 선택
            </span>
          </div>
          <ul className="max-h-72 overflow-y-auto rounded-md border">
            {options.length === 0 ? (
              <li className="px-3 py-2 text-sm text-muted-foreground">맞는 고객사가 없습니다.</li>
            ) : (
              options.map((t) => (
                <li key={t.tenant_id} className="border-b last:border-0">
                  <label className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm hover:bg-muted">
                    <input
                      type="checkbox"
                      checked={selected.has(t.tenant_id)}
                      onChange={(e) => toggle(t.tenant_id, e.target.checked)}
                    />
                    <span className="min-w-0 flex-1 truncate">{t.name}</span>
                    <TenantStatusBadge status={t.status} />
                  </label>
                </li>
              ))
            )}
          </ul>
          {selected.size === 0 && original !== null && original.size > 0 ? (
            <p className="text-xs text-destructive">모두 해제하면 이 계정은 어떤 고객사도 볼 수 없습니다.</p>
          ) : null}
        </div>
      )}
    </ActionDialog>
  );
}
