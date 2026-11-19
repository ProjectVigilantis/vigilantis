'use client';

// 중앙 관리계정의 계정 관리 화면: 관리자 직원계정(플랫폼 계정) 목록이다(`GET /staff`, 중앙 관리계정 전용).
// 고객사 계정은 고객사 상세의 고객사 계정 패널에서 다룬다(components/tenants/tenant-users-panel.tsx).
//
// 개인정보 보호를 위해 처음에는 아무 계정도 보이지 않고, 이메일로 검색한 결과만 보인다.
// 중앙 관리계정 자신은 목록에서 뺀다. 최종 결정자라 스스로를 관리할 일이 없고, 서버도 자기 변경을 거절한다.
//
// 직원 초대(`POST /staff`, #572 1번)는 담당 그룹 없는 관리자 직원계정만 만든다. 중앙 관리계정은 초대한 뒤
// 등급 변경(`PUT /staff/{id}/role`)으로 지정한다. 초대가 끝나면 그 이메일로 검색해 새 계정 행을 바로 보인다.

import { ChevronDown } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState, type FormEvent } from 'react';

import { HangulWarnedInput } from '@/components/auth/hangul-warned-input';
import { useSession } from '@/components/auth/session-provider';
import { INPUT_CLASS, LABEL_CLASS } from '@/components/form-classes';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ActionDialog, HistorySheet } from '@/components/users/users-view';
import {
  changeStaffRole,
  changeStaffStatus,
  getStaffEvents,
  getStaffGroups,
  inviteStaff,
  listGroups,
  replaceStaffGroups,
  resendStaffInvite,
} from '@/lib/api/client';
import { ACCOUNT_KIND_LABELS } from '@/lib/roles';
import { STATUS_LABELS, formatUserTime, staffAdminErrorMessage } from '@/lib/user-admin';
import type { GroupItem, PlatformRole, StaffItem, TenantListItem } from '@/types/api';

const PLATFORM_ROLES: readonly PlatformRole[] = ['TENANT_MANAGER', 'CENTRAL_ADMIN'];

type Panel = { kind: 'invite' } | { kind: 'status' | 'history' | 'groups' | 'role' | 'resend'; user: StaffItem };

export function StaffView({
  staff,
}: {
  staff: StaffItem[];
  /** 기존 /users 호출부와의 호환용. 배정 창의 그룹 선택지는 창에서 직접 조회한다. */
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

  function open(next: Panel) {
    setDone(null);
    setPanel(next);
  }

  function finished(message: string) {
    setPanel(null);
    setDone(message);
    router.refresh();
  }

  function invited(email: string) {
    setDraft(email);
    setQuery(email);
    finished(`${email}을(를) 관리자 직원계정으로 초대했습니다.`);
  }

  const close = () => setPanel(null);
  const emailOf = (userId: string) => staff.find((s) => s.user_id === userId)?.email ?? userId;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold">계정 관리</h1>
        <Button type="button" onClick={() => open({ kind: 'invite' })}>
          직원 초대
        </Button>
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
                <TableHead className="w-[34%]">이메일</TableHead>
                <TableHead className="w-[18%]">계정 종류</TableHead>
                <TableHead className="w-[12%]">상태</TableHead>
                <TableHead>마지막 로그인</TableHead>
                <TableHead className="w-px">
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
                  <TableCell className="text-right">
                    {/* 메뉴는 비모달이다. 항목을 고르면 메뉴가 닫히고 확인 모달이 열린다(users-view와 같다). */}
                    <DropdownMenu modal={false}>
                      <DropdownMenuTrigger asChild>
                        <Button type="button" size="sm" variant="outline" aria-label={`${user.email} 관리`}>
                          관리
                          <ChevronDown aria-hidden />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent>
                        {/* 담당 배정은 관리자 직원계정만 받는다. 중앙 관리계정은 전체 고객사를 본다(서버도 409로 거절). */}
                        {user.platform_role === 'TENANT_MANAGER' ? (
                          <DropdownMenuItem onSelect={() => open({ kind: 'groups', user })}>담당 그룹</DropdownMenuItem>
                        ) : null}
                        <DropdownMenuItem onSelect={() => open({ kind: 'role', user })}>등급 변경</DropdownMenuItem>
                        <DropdownMenuItem
                          destructive={user.status === 'ACTIVE'}
                          onSelect={() => open({ kind: 'status', user })}
                        >
                          {user.status === 'ACTIVE' ? '비활성화' : '활성화'}
                        </DropdownMenuItem>
                        {/* 아직 로그인한 적이 없는 직원만. 재발송 가능 여부의 최종 판정은 서버다(409). */}
                        {user.last_login_at === null ? (
                          <DropdownMenuItem onSelect={() => open({ kind: 'resend', user })}>초대 재발송</DropdownMenuItem>
                        ) : null}
                        <DropdownMenuSeparator />
                        <DropdownMenuItem onSelect={() => open({ kind: 'history', user })}>이력</DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}

      {panel?.kind === 'invite' ? <StaffInviteDialog onClose={close} onDone={invited} /> : null}

      {panel?.kind === 'role' ? (
        <StaffRoleDialog
          user={panel.user}
          onClose={close}
          onDone={() => finished(`${panel.user.email}의 등급을 바꿨습니다.`)}
        />
      ) : null}

      {panel?.kind === 'resend' ? (
        <ActionDialog
          action="resend"
          title="초대 메일 재발송"
          description={`${panel.user.email}(으)로 초대 메일을 다시 보냅니다.`}
          confirmLabel="재발송"
          errorMessage={(code) => staffAdminErrorMessage('resend', code)}
          run={() => resendStaffInvite(panel.user.user_id)}
          onClose={close}
          onDone={() => finished('초대 메일을 다시 보냈습니다.')}
        />
      ) : null}

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
          errorMessage={(code) => staffAdminErrorMessage('status', code)}
          run={() => changeStaffStatus(panel.user.user_id, panel.user.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE')}
          onClose={close}
          onDone={() => finished('계정 상태를 바꿨습니다.')}
        />
      ) : null}

      {panel?.kind === 'groups' ? (
        <GroupAssignDialog
          user={panel.user}
          onClose={close}
          onDone={() => finished(`${panel.user.email}의 담당 그룹을 바꿨습니다.`)}
        />
      ) : null}

      {panel?.kind === 'history' ? (
        <HistorySheet user={panel.user} emailOf={emailOf} onClose={close} load={getStaffEvents} />
      ) : null}
    </div>
  );
}

/**
 * 직원 초대 창(#572 1번). 이메일만 받는다. 서버가 담당 그룹 없는 관리자 직원계정으로 만들고 Cognito가 임시 비밀번호
 * 메일을 보낸다. 이메일은 다듬지 않고 입력 그대로 보낸다(서버가 형식을 판정한다).
 */
function StaffInviteDialog({ onClose, onDone }: { onClose: () => void; onDone: (email: string) => void }) {
  const sent = useRef('');

  return (
    <ActionDialog
      action="invite"
      title="직원 초대"
      description="입력한 이메일로 임시 비밀번호가 담긴 초대 메일을 보냅니다. 계정은 담당 그룹이 없는 관리자 직원계정으로 만들어집니다."
      confirmLabel="초대"
      errorMessage={(code) => staffAdminErrorMessage('invite', code)}
      run={async (form) => {
        const created = await inviteStaff({ email: String(form.get('email') ?? '') });
        sent.current = created.email;
      }}
      onClose={onClose}
      onDone={() => onDone(sent.current)}
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor="staff-invite-email" className={LABEL_CLASS}>
          이메일
        </label>
        <HangulWarnedInput
          id="staff-invite-email"
          name="email"
          type="email"
          required
          autoFocus
          className={INPUT_CLASS}
        />
      </div>
    </ActionDialog>
  );
}

/** 등급 변경 창. 서버는 등급이 바뀌면 저장된 담당 그룹을 비운다. 그 결과를 저장 전에 경고로 보인다. */
function StaffRoleDialog({ user, onClose, onDone }: { user: StaffItem; onClose: () => void; onDone: () => void }) {
  const [role, setRole] = useState<PlatformRole>(user.platform_role);

  return (
    <ActionDialog
      action="role"
      title="등급 변경"
      description={`${user.email} 계정의 등급을 바꿉니다.`}
      confirmLabel="변경"
      disabled={role === user.platform_role}
      errorMessage={(code) => staffAdminErrorMessage('role', code)}
      run={() => changeStaffRole(user.user_id, role)}
      onClose={onClose}
      onDone={onDone}
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor="staff-role-select" className={LABEL_CLASS}>
          등급 (현재: {ACCOUNT_KIND_LABELS[user.platform_role]})
        </label>
        <select
          id="staff-role-select"
          value={role}
          onChange={(event) => setRole(event.target.value as PlatformRole)}
          className={INPUT_CLASS}
        >
          {PLATFORM_ROLES.map((value) => (
            <option key={value} value={value}>
              {ACCOUNT_KIND_LABELS[value]}
            </option>
          ))}
        </select>
      </div>
      {role === user.platform_role ? null : (
        <p className="text-xs text-destructive">
          {role === 'CENTRAL_ADMIN'
            ? '모든 고객사와 관리자 직원계정을 관리할 수 있게 됩니다. 저장된 담당 그룹은 비워집니다.'
            : '담당 그룹이 없는 관리자 직원계정이 됩니다. 담당 그룹을 배정하기 전에는 어떤 고객사도 볼 수 없습니다.'}
        </p>
      )}
    </ActionDialog>
  );
}

type Assigned = { kind: 'loading' } | { kind: 'error' } | { kind: 'ok'; ids: ReadonlySet<string>; groups: GroupItem[] };

/**
 * 담당 그룹 배정 창(#572 2번): 지금 배정을 읽어 체크한 채로 열고, 저장하면 전체를 교체한다(`PUT /staff/{id}/groups`).
 * 그룹 이름 · ID로 거르고, 배정된 그룹을 위에 둔다. 바뀐 것이 없으면 저장을 막는다(서버는 409).
 */
function GroupAssignDialog({
  user,
  onClose,
  onDone,
}: {
  user: StaffItem;
  onClose: () => void;
  onDone: () => void;
}) {
  const [assigned, setAssigned] = useState<Assigned>({ kind: 'loading' });
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [filter, setFilter] = useState('');

  useEffect(() => {
    let alive = true;
    Promise.all([getStaffGroups(user.user_id), listGroups()])
      .then(([res, groups]) => {
        if (!alive) return;
        const ids = new Set(res.group_ids);
        setAssigned({ kind: 'ok', ids, groups: groups.items });
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
  const groups = assigned.kind === 'ok' ? assigned.groups : [];
  const options = groups
    .filter((g) => needle === '' || g.name.toLowerCase().includes(needle) || g.group_id.toLowerCase().includes(needle))
    .sort((a, b) => Number(original?.has(b.group_id) ?? 0) - Number(original?.has(a.group_id) ?? 0) || a.name.localeCompare(b.name, 'ko'));

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
      title="담당 그룹 배정"
      description={user.email}
      confirmLabel="저장"
      disabled={!changed}
      run={() => replaceStaffGroups(user.user_id, [...selected])}
      onClose={onClose}
      onDone={onDone}
    >
      {assigned.kind === 'loading' ? (
        <p className="text-sm text-muted-foreground">그룹 목록과 지금 배정을 불러오는 중…</p>
      ) : assigned.kind === 'error' ? (
        <p role="alert" className="text-sm text-destructive">
          그룹 목록과 지금 배정을 불러오지 못했습니다. 창을 닫고 다시 열어 주세요.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-2">
            <label htmlFor="assign-filter" className="sr-only">
              그룹 거르기
            </label>
            <input
              id="assign-filter"
              type="search"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="그룹 이름 또는 ID"
              autoComplete="off"
              className={INPUT_CLASS}
            />
            <span className="shrink-0 text-xs text-muted-foreground" aria-live="polite">
              {selected.size}개 선택
            </span>
          </div>
          <ul className="max-h-72 overflow-y-auto rounded-md border">
            {groups.length === 0 ? (
              <li className="px-3 py-2 text-sm text-muted-foreground">
                그룹이 아직 없습니다.{' '}
                <Link href="/tenants/groups" className="text-foreground hover:underline">
                  고객사 그룹 만들기
                </Link>
              </li>
            ) : options.length === 0 ? (
              <li className="px-3 py-2 text-sm text-muted-foreground">맞는 그룹이 없습니다.</li>
            ) : (
              options.map((g) => (
                <li key={g.group_id} className="border-b last:border-0">
                  <label className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm hover:bg-muted">
                    <input
                      type="checkbox"
                      checked={selected.has(g.group_id)}
                      onChange={(e) => toggle(g.group_id, e.target.checked)}
                    />
                    <span className="min-w-0 flex-1 truncate">{g.name}</span>
                  </label>
                </li>
              ))
            )}
          </ul>
          {selected.size === 0 && original !== null && original.size > 0 ? (
            <p className="text-xs text-destructive">모두 해제하면 이 계정은 어떤 그룹의 고객사도 볼 수 없습니다.</p>
          ) : null}
        </div>
      )}
    </ActionDialog>
  );
}
