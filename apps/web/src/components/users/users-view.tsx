'use client';

// 사용자 관리(#492 PR 3) — 목록 · 초대 · 초대 재발송 · 역할 · 활성 상태 · 로그인 세션 종료 · 이력.
// 최소 동작 화면이다(시각 정리는 FE 담당이 이어서 한다). 조치가 끝나면 낙관적으로 고치지 않고 서버 목록을 다시 읽는다.
// 실패 문구는 lib/user-admin.ts가 정한다 — 403은 "관리자만", 초대 · 재발송의 결과 불명 실패는 "메일이 나갔을 수 있다".

import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';

import { HangulWarnedInput } from '@/components/auth/hangul-warned-input';
import { useSession } from '@/components/auth/session-provider';
import { INPUT_CLASS, LABEL_CLASS } from '@/components/form-classes';
import { Button } from '@/components/ui/button';
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
  userAdminErrorMessage,
  type UserAdminAction,
} from '@/lib/user-admin';
import type { UserEventItem, UserListItem, UserRole } from '@/types/api';

const ROLES: readonly UserRole[] = ['ADMIN', 'OPERATOR', 'VIEWER'];

type Panel =
  | { kind: 'invite' }
  | { kind: 'resend' | 'role' | 'status' | 'revoke' | 'history'; user: UserListItem };

export function UsersView({ users }: { users: UserListItem[] }) {
  const router = useRouter();
  const { session } = useSession();
  const myId = session.kind === 'user' ? session.user.user_id : null;
  const [panel, setPanel] = useState<Panel | null>(null);
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
  const emailOf = (userId: string) => users.find((u) => u.user_id === userId)?.email ?? userId;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold">사용자 관리</h1>
        <Button type="button" onClick={() => open({ kind: 'invite' })}>
          사용자 초대
        </Button>
      </div>

      {done ? (
        <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm">
          {done}
        </p>
      ) : null}

      {users.length === 0 ? (
        <p className="text-sm text-muted-foreground">등록된 사용자가 없습니다.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>이메일</TableHead>
              <TableHead>역할</TableHead>
              <TableHead>상태</TableHead>
              <TableHead>마지막 로그인</TableHead>
              <TableHead>
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
                <TableCell>{ROLE_LABELS[user.role]}</TableCell>
                <TableCell>{STATUS_LABELS[user.status]}</TableCell>
                <TableCell>
                  {user.last_login_at === null ? '로그인 기록 없음' : formatUserTime(user.last_login_at)}
                </TableCell>
                <TableCell>
                  <div className="flex gap-1">
                    <Button type="button" size="sm" variant="outline" onClick={() => open({ kind: 'role', user })}>
                      역할 변경
                    </Button>
                    <Button type="button" size="sm" variant="outline" onClick={() => open({ kind: 'status', user })}>
                      {user.status === 'ACTIVE' ? '비활성화' : '활성화'}
                    </Button>
                    <Button type="button" size="sm" variant="outline" onClick={() => open({ kind: 'revoke', user })}>
                      세션 종료
                    </Button>
                    {/* 아직 로그인한 적이 없는 사용자만 — 재발송 가능 여부의 최종 판정은 서버다(409). */}
                    {user.last_login_at === null ? (
                      <Button type="button" size="sm" variant="outline" onClick={() => open({ kind: 'resend', user })}>
                        초대 재발송
                      </Button>
                    ) : null}
                    <Button type="button" size="sm" variant="ghost" onClick={() => open({ kind: 'history', user })}>
                      이력
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {panel?.kind === 'invite' ? <InviteDialog onClose={close} onDone={finished} /> : null}

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
        <RoleDialog user={panel.user} self={panel.user.user_id === myId} onClose={close} onDone={finished} />
      ) : null}

      {panel?.kind === 'status' ? (
        <ActionDialog
          action="status"
          title={panel.user.status === 'ACTIVE' ? '사용자 비활성화' : '사용자 활성화'}
          description={
            panel.user.status === 'ACTIVE'
              ? `${panel.user.email} 사용자는 로그인할 수 없게 되고, 사용 중인 로그인 세션도 더 이상 쓸 수 없습니다.`
              : `${panel.user.email} 사용자가 다시 로그인할 수 있게 됩니다.`
          }
          confirmLabel={panel.user.status === 'ACTIVE' ? '비활성화' : '활성화'}
          destructive={panel.user.status === 'ACTIVE'}
          run={() => changeUserStatus(panel.user.user_id, panel.user.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE')}
          onClose={close}
          onDone={() => finished('사용자 상태를 바꿨습니다.')}
        />
      ) : null}

      {panel?.kind === 'revoke' ? (
        <ActionDialog
          action="revoke"
          title="로그인 세션 종료"
          description={
            panel.user.user_id === myId
              ? '내 로그인 세션을 모두 종료합니다. 이 화면도 로그인 화면으로 이동합니다.'
              : `${panel.user.email} 사용자의 로그인 세션을 모두 종료합니다. 사용자는 다시 로그인해야 합니다.`
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
function ActionDialog({
  action,
  title,
  description,
  confirmLabel,
  destructive = false,
  disabled = false,
  children,
  run,
  onClose,
  onDone,
}: {
  action: UserAdminAction;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  destructive?: boolean;
  disabled?: boolean;
  children?: ReactNode;
  run: (form: FormData) => Promise<unknown>;
  onClose: () => void;
  onDone: () => void;
}) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setError(null);
    try {
      await run(new FormData(event.currentTarget));
    } catch (caught) {
      const code = caught instanceof ApiError ? caught.code : null;
      setError(userAdminErrorMessage(action, code));
      if (code === 'USER_NOT_FOUND' || code === 'USER_STATE_CONFLICT') router.refresh();
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

function InviteDialog({ onClose, onDone }: { onClose: () => void; onDone: (message: string) => void }) {
  const [role, setRole] = useState<UserRole>('VIEWER');
  const [recover, setRecover] = useState(false);

  return (
    <ActionDialog
      action="invite"
      title="사용자 초대"
      description={
        recover
          ? '같은 이메일의 기존 인증 계정을 이 앱 사용자로 연결합니다.'
          : '입력한 이메일로 임시 비밀번호가 담긴 초대 메일을 보냅니다.'
      }
      confirmLabel={recover ? '기존 계정 연결' : '초대'}
      run={(form) =>
        // 이메일은 다듬지 않고 입력 그대로 보낸다(서버가 형식을 판정한다).
        inviteUser({ email: String(form.get('email') ?? ''), role, recover_existing: recover })
      }
      onClose={onClose}
      onDone={() => onDone(recover ? '기존 계정을 연결했습니다.' : '사용자를 초대했습니다.')}
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor="invite-email" className={LABEL_CLASS}>
          이메일
        </label>
        <HangulWarnedInput id="invite-email" name="email" type="email" required autoFocus className={INPUT_CLASS} />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="invite-role" className={LABEL_CLASS}>
          역할
        </label>
        <RoleSelect id="invite-role" value={role} onChange={setRole} />
      </div>
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
          인증 서비스(Cognito)에 이 이메일의 계정이 이미 있을 때만 선택하세요. 선택하면 그 기존 계정을 이 앱 사용자로
          연결합니다. 이전 초대가 실패했다는 증거는 아니므로, 계정 주인이 맞는지 먼저 확인하세요.
        </p>
        {recover ? (
          <p role="status" className="text-xs font-medium text-destructive">
            기존 계정의 비밀번호 · 인증 앱 설정을 쓰는 사람이 이 역할로 로그인하게 됩니다. 계정 주인을 확인했을 때만
            진행하세요.
          </p>
        ) : null}
      </div>
    </ActionDialog>
  );
}

function RoleDialog({
  user,
  self,
  onClose,
  onDone,
}: {
  user: UserListItem;
  self: boolean;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [role, setRole] = useState<UserRole>(user.role);

  return (
    <ActionDialog
      action="role"
      title="역할 변경"
      description={
        self
          ? '내 역할을 바꿉니다. 관리자가 아니게 되면 이 화면을 더 쓸 수 없습니다.'
          : `${user.email} 사용자의 역할을 바꿉니다.`
      }
      confirmLabel="변경"
      disabled={role === user.role}
      run={() => changeUserRole(user.user_id, role)}
      onClose={onClose}
      onDone={() => onDone('역할을 바꿨습니다.')}
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor="role-select" className={LABEL_CLASS}>
          역할 (현재: {ROLE_LABELS[user.role]})
        </label>
        <RoleSelect id="role-select" value={role} onChange={setRole} />
      </div>
    </ActionDialog>
  );
}

type HistoryState = { kind: 'loading' } | { kind: 'error'; message: string } | { kind: 'ok'; items: UserEventItem[] };

function HistorySheet({
  user,
  emailOf,
  onClose,
}: {
  user: UserListItem;
  emailOf: (userId: string) => string;
  onClose: () => void;
}) {
  const [history, setHistory] = useState<HistoryState>({ kind: 'loading' });

  useEffect(() => {
    let alive = true;
    getUserEvents(user.user_id)
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
  }, [user.user_id]);

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
