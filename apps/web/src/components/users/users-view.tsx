'use client';

// 사용자 관리(#492 PR 3) — 목록 · 초대 · 초대 재발송 · 역할 · 활성 상태 · 로그인 세션 종료 · 이력.
// 최소 동작 화면이다. 조치가 끝나면 낙관적으로 고치지 않고 서버 목록을 다시 읽는다.
// 실패 문구는 lib/user-admin.ts가 정한다 — 403은 "관리자만", 초대 · 재발송의 결과 불명 실패는 "메일이 나갔을 수 있다".
// 관리 범위(#492): 중앙 관리자는 모든 고객사 사용자를, 고객 담당자는 배정된 고객의 사용자만, 고객사 ADMIN은 자기
// 고객사 사용자만 본다 — 범위는 서버가 정한다. 행의 조치 버튼은 대상 고객을 다룰 수 있을 때만 그린다(lib/roles.ts
// `canManageUsersOf`). 담당 고객이 없는 고객 담당자에게는 초대 · 조치 없이 배정 없음 안내만 보인다.

import { ChevronDown } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';

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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import {
  ApiError,
  changeUserRole,
  changeUserStatus,
  getUserEvents,
  inviteUser,
  resendInvite,
  revokeUserSessions,
} from '@/lib/api/client';
import {
  EVENT_LABELS,
  ROLE_LABELS,
  STATUS_LABELS,
  eventSummary,
  formatUserTime,
  inviteRequestFor,
  canChangeRow,
  userAdminErrorMessage,
  type UserAdminAction,
} from '@/lib/user-admin';
import {
  canManageUsers,
  canManageUsersOf,
  hasCustomerScope,
  isCentralAdmin,
  isUnassignedManager,
  type PlatformSubject,
} from '@/lib/roles';
import type { CurrentUserResponse, UserEventItem, UserEventsResponse, UserListItem, UserRole } from '@/types/api';

const ROLES: readonly UserRole[] = ['ADMIN', 'OPERATOR', 'VIEWER'];

type Panel =
  | { kind: 'invite' }
  | { kind: 'resend' | 'role' | 'status' | 'revoke' | 'history'; user: UserListItem };

export function UsersView({
  users: allUsers,
  tenantFilter = null,
  inviteOnOpen = false,
  embedded = false,
  canInvite = true,
}: {
  users: UserListItem[];
  /** 고객 상세에서 넘어온 고객(`?tenant=`). 그 고객 사용자만 보이고 초대 창의 고객 ID를 채운다. 이름은 못 읽으면 null이다. */
  tenantFilter?: { id: string; name: string | null } | null;
  /** 첫 화면에서 초대 창을 연 채로 시작한다(`?invite=1`). */
  inviteOnOpen?: boolean;
  /** 고객사 상세 패널 안에 그린다. 화면 제목 · 고객사 필터 안내 · 고객사 ID 열을 빼고 표와 조치만 남긴다. */
  embedded?: boolean;
  /** 초대 버튼을 그린다. 고객사 상세는 관제 중(ACTIVE)인 고객사에서만 연다(ADR-0012). */
  canInvite?: boolean;
}) {
  const router = useRouter();
  const { session } = useSession();
  const me = session.kind === 'user' ? session.user : null;
  const myId = me?.user_id ?? null;
  const users = tenantFilter === null ? allUsers : allUsers.filter((u) => u.tenant_id === tenantFilter.id);
  const unassigned = isUnassignedManager(me);
  // 고객사 ID는 플랫폼 측이 고객사를 구분하는 값이다. 고객사 계정은 자기 고객사 계정만 받으므로 열이 필요 없다.
  const showTenantId = !embedded && me?.account_type === 'PLATFORM';
  const [panel, setPanel] = useState<Panel | null>(inviteOnOpen ? { kind: 'invite' } : null);
  const [done, setDone] = useState<string | null>(null);

  function open(next: Panel) {
    setDone(null);
    setPanel(next);
  }

  function finished(message: string) {
    setPanel(null);
    setDone(message);
    router.refresh();
  }

  const close = () => setPanel(null);
  const emailOf = (userId: string) => allUsers.find((u) => u.user_id === userId)?.email ?? userId;

  return (
    <div className="flex flex-col gap-4">
      {embedded ? (
        canInvite && !unassigned ? (
          <div className="flex justify-end">
            <Button type="button" size="sm" disabled={!canManageUsers(me)} onClick={() => open({ kind: 'invite' })}>
              직원 초대
            </Button>
          </div>
        ) : null
      ) : (
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-col gap-1">
          <h1 className="text-lg font-semibold">계정 관리</h1>
          {/* 관리자 직원계정은 서버가 운영 CLI로만 만든다(auth/README.md · routers/staff.py에 생성 경로 없음). 이 화면에서 만드는 것은 고객사 계정뿐이다. */}
        </div>
        {/* 초대 칸(고객사 ID 입력 여부)이 계정 종류로 갈리므로 계정을 확인한 뒤에 연다. 담당 고객이 없으면 그리지 않는다. */}
        {unassigned ? null : (
          <Button type="button" disabled={!canManageUsers(me)} onClick={() => open({ kind: 'invite' })}>
            계정 초대
          </Button>
        )}
      </div>
      )}

      {tenantFilter !== null && !embedded ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border px-3 py-2 text-sm">
          <span>
            고객사 <span className="font-semibold">{tenantFilter.name ?? tenantFilter.id}</span>의 직원만 보는 중
          </span>
          <Link href={`/tenants/${encodeURIComponent(tenantFilter.id)}`} className="text-primary hover:underline">
            고객사 상세로
          </Link>
          <Link href="/users" className="text-primary hover:underline">
            전체 계정 보기
          </Link>
        </div>
      ) : null}

      {done ? (
        <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm">
          {done}
        </p>
      ) : null}

      {users.length === 0 ? (
        unassigned ? (
          <p role="status" className="rounded-lg border p-4 text-sm">
            담당 고객사가 배정되지 않아 관리할 고객사 계정이 없습니다. 중앙 관리계정에서 담당 고객사를 배정받아야 합니다.
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">
            {tenantFilter === null ? '등록된 직원이 없습니다.' : '이 고객사에 등록된 직원이 없습니다.'}
          </p>
        )
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              {/* 조치를 한 버튼(관리 메뉴)에 모아 남은 폭을 속성 칸에 나눈다. 관리 칸은 내용 폭만 쓴다. */}
              <TableHead className="w-[30%]">이메일</TableHead>
              {showTenantId ? <TableHead className="w-[20%]">고객사 ID</TableHead> : null}
              <TableHead className="w-[16%]">권한</TableHead>
              <TableHead className="w-[12%]">상태</TableHead>
              <TableHead>마지막 로그인</TableHead>
              <TableHead className="w-px">
                <span className="sr-only">관리</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {users.map((user) => (
              <TableRow key={user.user_id}>
                <TableCell>
                  {user.email}
                  {user.user_id === myId ? <span className="ml-1 text-xs text-muted-foreground">(나)</span> : null}
                </TableCell>
                {showTenantId ? <TableCell className="font-mono text-xs">{user.tenant_id ?? '-'}</TableCell> : null}
                <TableCell>{ROLE_LABELS[user.role]}</TableCell>
                <TableCell>{STATUS_LABELS[user.status]}</TableCell>
                <TableCell>
                  {user.last_login_at === null ? '로그인 기록 없음' : formatUserTime(user.last_login_at)}
                </TableCell>
                <TableCell className="text-right">
                  {/* 확인된 계정이 이 고객사 사용자를 다룰 수 없으면(배정 스냅샷 밖) 조치를 그리지 않는다. 확인 전에는
                      기존처럼 그리고, 본인 판정이 필요한 조치는 막는다. 실제 범위는 서버(404)다. */}
                  {me !== null && !canManageUsersOf(me, user.tenant_id) ? (
                    <span className="text-xs text-muted-foreground">담당 범위 밖</span>
                  ) : (
                    // 메뉴는 비모달이다. 항목을 고르면 메뉴가 닫히고 확인 모달이 열린다(모달 둘이 포커스를 다투지 않게).
                    <DropdownMenu modal={false}>
                      <DropdownMenuTrigger asChild>
                        <Button type="button" size="sm" variant="outline" aria-label={`${user.email} 관리`}>
                          관리
                          <ChevronDown aria-hidden />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent>
                        <DropdownMenuItem
                          disabled={!canChangeRow(user.user_id, myId)}
                          onSelect={() => open({ kind: 'role', user })}
                        >
                          권한 변경
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          disabled={!canChangeRow(user.user_id, myId)}
                          destructive={user.status === 'ACTIVE'}
                          onSelect={() => open({ kind: 'status', user })}
                        >
                          {user.status === 'ACTIVE' ? '비활성화' : '활성화'}
                        </DropdownMenuItem>
                        <DropdownMenuItem onSelect={() => open({ kind: 'revoke', user })}>세션 종료</DropdownMenuItem>
                        {/* 아직 로그인한 적이 없는 사용자만 — 재발송 가능 여부의 최종 판정은 서버다(409). */}
                        {user.last_login_at === null ? (
                          <DropdownMenuItem onSelect={() => open({ kind: 'resend', user })}>초대 재발송</DropdownMenuItem>
                        ) : null}
                        <DropdownMenuSeparator />
                        <DropdownMenuItem onSelect={() => open({ kind: 'history', user })}>이력</DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {panel?.kind === 'invite' && me !== null ? (
        <InviteDialog inviter={me} defaultTenantId={tenantFilter?.id} onClose={close} onDone={finished} />
      ) : null}

      {panel?.kind === 'resend' ? (
        <ActionDialog
          action="resend"
          title="초대 메일 재발송"
          description={`${panel.user.email}(으)로 초대 메일을 다시 보냅니다.`}
          confirmLabel="재발송"
          run={() => resendInvite(panel.user.user_id)}
          onClose={close}
          onDone={() => finished('초대 메일을 다시 보냈습니다.')}
        />
      ) : null}

      {panel?.kind === 'role' ? (
        <RoleDialog user={panel.user} onClose={close} onDone={finished} />
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
          run={() => changeUserStatus(panel.user.user_id, panel.user.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE')}
          onClose={close}
          onDone={() => finished('계정 상태를 바꿨습니다.')}
        />
      ) : null}

      {panel?.kind === 'revoke' ? (
        <ActionDialog
          action="revoke"
          title="로그인 세션 종료"
          description={
            panel.user.user_id === myId
              ? '내 로그인 세션을 모두 종료합니다. 이 화면도 로그인 화면으로 이동합니다.'
              : `${panel.user.email} 계정의 로그인 세션을 모두 종료합니다. 직원은 다시 로그인해야 합니다.`
          }
          confirmLabel="세션 종료"
          destructive
          run={() => revokeUserSessions(panel.user.user_id)}
          onClose={close}
          onDone={() => finished('로그인 세션을 종료했습니다.')}
        />
      ) : null}

      {panel?.kind === 'history' ? <HistorySheet user={panel.user} emailOf={emailOf} onClose={close} /> : null}
    </div>
  );
}

/**
 * 확인 → 실행 모달. 폼이라 필드가 있으면 브라우저 기본 검사(type=email 등)를 그대로 받는다.
 * 실패하면 모달에 남아 문구를 보이고, 대상이 없거나 상태가 바뀐 경우(404 · 409)는 뒤의 목록을 다시 읽는다.
 */
export function ActionDialog({
  action,
  actor = null,
  title,
  description,
  confirmLabel,
  destructive = false,
  disabled = false,
  children,
  validate,
  run,
  onClose,
  onDone,
}: {
  action: UserAdminAction;
  /** 조치하는 계정. 기존 계정 연결(복구) 안내는 고객 범위가 있는 플랫폼 계정에게만 붙는다(lib/user-admin.ts). */
  actor?: PlatformSubject | null;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  destructive?: boolean;
  disabled?: boolean;
  children?: ReactNode;
  /** 보내기 전 입력 검사. 문구를 돌려주면 요청하지 않고 그 문구를 보인다(결과 불명 문구와 섞이지 않게). */
  validate?: (form: FormData) => string | null;
  run: (form: FormData) => Promise<unknown>;
  onClose: () => void;
  onDone: () => void;
}) {
  const router = useRouter();
  const { refresh: refreshSession } = useSession();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    const form = new FormData(event.currentTarget);
    const invalid = validate?.(form) ?? null;
    if (invalid !== null) {
      setError(invalid);
      return;
    }
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      await run(form);
    } catch (caught) {
      const code = caught instanceof ApiError ? caught.code : null;
      setError(userAdminErrorMessage(action, code, actor));
      if (code === 'USER_NOT_FOUND' || code === 'USER_STATE_CONFLICT') router.refresh();
      // 403은 화면의 등급 · 배정 스냅샷이 낡았다는 뜻일 수 있다 — `/me`와 목록을 다시 읽어 지금 권한에 맞춘다.
      if (code === 'AUTH_FORBIDDEN') {
        refreshSession().catch(() => {});
        router.refresh();
      }
      inFlight.current = false;
      setPending(false);
      return;
    }
    inFlight.current = false;
    setPending(false);
    onDone();
  }

  return (
    <Dialog open onOpenChange={(next) => (next || inFlight.current ? undefined : onClose())}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <form className="flex flex-col gap-4" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{description}</DialogDescription>
          </DialogHeader>
          {children}
          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={pending}>
              취소
            </Button>
            <Button type="submit" variant={destructive ? 'destructive' : 'default'} disabled={pending || disabled}>
              {pending ? '처리 중…' : confirmLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function RoleSelect({ id, value, onChange }: { id: string; value: UserRole; onChange: (role: UserRole) => void }) {
  return (
    <select
      id={id}
      name="role"
      value={value}
      onChange={(event) => onChange(event.target.value as UserRole)}
      className={INPUT_CLASS}
    >
      {ROLES.map((role) => (
        <option key={role} value={role}>
          {ROLE_LABELS[role]}
        </option>
      ))}
    </select>
  );
}

/**
 * 초대 칸은 계정 종류로 갈린다. 플랫폼 운영자는 초대할 고객의 ID(UUID)를 직접 넣는다 — 테넌트 목록 API는 있지만
 * 이 화면에는 선택 목록을 연결하지 않았다. 고객 담당자는 담당 고객의 ID만 보낼 수 있다(lib/user-admin.ts
 * `inviteRequestFor`). 고객사 ADMIN은 자기 소속을 읽기 전용으로만 보고 다른 고객을 고를 수 없다.
 */
function InviteDialog({
  inviter,
  defaultTenantId,
  onClose,
  onDone,
}: {
  inviter: CurrentUserResponse;
  /** 고객사 ID 칸의 첫 값(고객사 상세에서 넘어온 경우). 플랫폼 계정에게만 쓰인다. */
  defaultTenantId?: string;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [role, setRole] = useState<UserRole>('VIEWER');
  const [recover, setRecover] = useState(false);
  // 기존 계정 연결(복구)은 고객 범위가 있는 플랫폼 계정만 — 호환 역할(ADMIN)이 아니라 등급 · 배정으로 정한다.
  const canRecover = hasCustomerScope(inviter);
  const build = (form: FormData) =>
    inviteRequestFor(inviter, {
      email: String(form.get('email') ?? ''),
      role,
      recoverExisting: recover,
      tenantId: String(form.get('tenant_id') ?? ''),
    });

  return (
    <ActionDialog
      action="invite"
      actor={inviter}
      title="계정 초대"
      description={
        recover
          ? '같은 이메일의 기존 인증 계정을 이 앱 계정으로 연결합니다.'
          : '입력한 이메일로 임시 비밀번호가 담긴 초대 메일을 보냅니다.'
      }
      confirmLabel={recover ? '기존 계정 연결' : '초대'}
      validate={(form) => {
        const built = build(form);
        return built.ok ? null : built.error;
      }}
      run={(form) => {
        // validate가 먼저 통과시킨 입력이다. 이메일은 다듬지 않고 입력 그대로 보낸다(서버가 형식을 판정한다).
        const built = build(form);
        return built.ok ? inviteUser(built.request) : Promise.reject(new Error(built.error));
      }}
      onClose={onClose}
      onDone={() => onDone(recover ? '기존 계정을 연결했습니다.' : '직원을 초대했습니다.')}
    >
      {inviter.account_type === 'PLATFORM' ? (
        <div className="flex flex-col gap-1.5">
          <label htmlFor="invite-tenant" className={LABEL_CLASS}>
            고객사 ID
          </label>
          <input
            id="invite-tenant"
            name="tenant_id"
            type="text"
            required
            autoComplete="off"
            spellCheck={false}
            defaultValue={defaultTenantId}
            placeholder="123e4567-e89b-12d3-a456-426614174000"
            aria-describedby="invite-tenant-hint"
            className={`${INPUT_CLASS} font-mono`}
          />
          <p id="invite-tenant-hint" className="text-xs text-muted-foreground">
            {isCentralAdmin(inviter)
              ? '활성(ACTIVE) 상태인 고객사의 ID(UUID)를 기존 운영 절차로 확인해 입력하세요.'
              : '담당 고객사 중 활성(ACTIVE) 상태인 고객사의 ID(UUID)를 고객사 목록에서 확인해 입력하세요.'}
          </p>
        </div>
      ) : (
        // 고객사 ID는 플랫폼 측 구분 값이라 고객사 계정에게 보이지 않는다(2026-11-03). 서버가 초대자의 고객사로 채운다.
        <p className="text-xs text-muted-foreground">내 고객사에만 초대할 수 있습니다.</p>
      )}
      <div className="flex flex-col gap-1.5">
        <label htmlFor="invite-email" className={LABEL_CLASS}>
          이메일
        </label>
        <HangulWarnedInput id="invite-email" name="email" type="email" required autoFocus className={INPUT_CLASS} />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="invite-role" className={LABEL_CLASS}>
          권한
        </label>
        <RoleSelect id="invite-role" value={role} onChange={setRole} />
      </div>
      {/* 기존 계정 연결(복구)은 고객 범위가 있는 플랫폼 계정만 고를 수 있다 — 그 밖의 계정에게는 보이지 않는다(서버도 403으로 막는다). */}
      {canRecover ? (
        <div className="flex flex-col gap-1.5 rounded-md border p-3">
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={recover}
              onChange={(event) => setRecover(event.target.checked)}
              className="mt-0.5"
            />
            <span>같은 이메일의 기존 인증 계정을 연결 (복구)</span>
          </label>
          <p className="text-xs text-muted-foreground">
            인증 서비스(Cognito)에 이 이메일의 계정이 이미 있을 때만 선택하세요. 선택하면 그 기존 계정을 이 앱 계정으로
            연결합니다. 이전 초대가 실패했다는 증거는 아니므로, 계정 주인이 맞는지 먼저 확인하세요.
          </p>
          {recover ? (
            <p role="status" className="text-xs font-medium text-destructive">
              기존 계정의 비밀번호 · 인증 앱 설정을 쓰는 사람이 이 권한으로 로그인하게 됩니다. 계정 주인을 확인했을 때만
              진행하세요.
            </p>
          ) : null}
        </div>
      ) : null}
    </ActionDialog>
  );
}

function RoleDialog({
  user,
  onClose,
  onDone,
}: {
  user: UserListItem;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [role, setRole] = useState<UserRole>(user.role);

  return (
    <ActionDialog
      action="role"
      title="권한 변경"
      description={`${user.email} 계정의 권한을 바꿉니다.`}
      confirmLabel="변경"
      disabled={role === user.role}
      run={() => changeUserRole(user.user_id, role)}
      onClose={onClose}
      onDone={() => onDone('권한을 바꿨습니다.')}
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor="role-select" className={LABEL_CLASS}>
          권한 (현재: {ROLE_LABELS[user.role]})
        </label>
        <RoleSelect id="role-select" value={role} onChange={setRole} />
      </div>
    </ActionDialog>
  );
}

type HistoryState = { kind: 'loading' } | { kind: 'error'; message: string } | { kind: 'ok'; items: UserEventItem[] };

export function HistorySheet({
  user,
  emailOf,
  onClose,
  load = getUserEvents,
}: {
  user: Pick<UserListItem, 'user_id' | 'email'>;
  emailOf: (userId: string) => string;
  onClose: () => void;
  /** 이력 원천. 고객사 계정은 `/users/{id}/events`, 관리자 직원계정은 `/staff/{id}/events`다. */
  load?: (userId: string) => Promise<UserEventsResponse>;
}) {
  const [history, setHistory] = useState<HistoryState>({ kind: 'loading' });

  useEffect(() => {
    let alive = true;
    load(user.user_id)
      .then((res) => alive && setHistory({ kind: 'ok', items: res.items }))
      .catch((caught: unknown) => {
        if (!alive) return;
        const forbidden = caught instanceof ApiError && caught.code === 'AUTH_FORBIDDEN';
        setHistory({
          kind: 'error',
          message: forbidden
            ? userAdminErrorMessage('role', 'AUTH_FORBIDDEN')
            : caught instanceof Error
              ? caught.message
              : '이력을 불러오지 못했습니다.',
        });
      });
    return () => {
      alive = false;
    };
  }, [user.user_id, load]);

  return (
    <Sheet open onOpenChange={(next) => (next ? undefined : onClose())}>
      <SheetContent className="overflow-y-auto">
        <SheetHeader>
          <SheetTitle>관리 이력</SheetTitle>
          <SheetDescription className="wrap-anywhere">{user.email} · 최근 50건</SheetDescription>
        </SheetHeader>
        <div className="px-4 pb-4 text-sm">
          {history.kind === 'loading' ? <p className="text-muted-foreground">불러오는 중…</p> : null}
          {history.kind === 'error' ? (
            <p role="alert" className="text-destructive">
              {history.message}
            </p>
          ) : null}
          {history.kind === 'ok' && history.items.length === 0 ? (
            <p className="text-muted-foreground">기록된 이력이 없습니다.</p>
          ) : null}
          {history.kind === 'ok' && history.items.length > 0 ? (
            <ol className="flex flex-col gap-3">
              {history.items.map((event) => {
                const summary = eventSummary(event);
                return (
                  <li key={event.event_id} className="flex flex-col gap-0.5 border-b pb-2 last:border-0">
                    <span className="font-medium">
                      {EVENT_LABELS[event.action]}
                      {summary ? <span className="ml-1 font-normal text-muted-foreground">{summary}</span> : null}
                    </span>
                    <span className="text-xs text-muted-foreground wrap-anywhere">
                      {formatUserTime(event.occurred_at)} · {emailOf(event.actor_user_id)}
                    </span>
                  </li>
                );
              })}
            </ol>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}
