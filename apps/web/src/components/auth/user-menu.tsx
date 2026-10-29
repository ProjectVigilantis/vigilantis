'use client';

// GNB 우측 현재 사용자 · 로그아웃(#492 PR 1). 세션 값과 유휴 연장은 SessionProvider가 들고 있다(PR 2) —
// 로그인돼 있으면 이메일(누르면 계정 정보)과 [로그아웃], 세션이 없으면 [로그인] 링크(미인증 이동은 API 계층이 이미 건다).

import { LogOut } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { Fragment, useState } from 'react';

import { useSession } from '@/components/auth/session-provider';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { accountFields } from '@/lib/account-summary';
import { ApiError, logout } from '@/lib/api/client';
import { cn } from '@/lib/utils';

const isUnauthorized = (error: unknown) => error instanceof ApiError && error.httpStatus === 401;

export function UserMenu() {
  const router = useRouter();
  const { session: me } = useSession();
  const [logoutState, setLogoutState] = useState<'idle' | 'pending' | 'failed'>('idle');

  async function onLogout() {
    setLogoutState('pending');
    try {
      await logout();
    } catch (error) {
      // 401 = 세션이 이미 없다 — 로그아웃된 것과 같다. 그 밖의 실패는 쿠키가 남았을 수 있어 머문다.
      if (!isUnauthorized(error)) {
        setLogoutState('failed');
        return;
      }
    }
    router.replace('/login');
  }

  if (me.kind === 'anonymous') {
    return (
      <Button asChild size="sm" variant="outline">
        <Link href="/login">로그인</Link>
      </Button>
    );
  }
  if (me.kind !== 'user') return null;

  return (
    <div className="flex min-w-0 items-center gap-2">
      {/* 이메일을 누르면 현재 계정 정보(`/me` 값만)를 연다. 좁은 화면(< sm)에서는 이메일 폭을 고정하고 줄인다 —
          줄어드는 몫은 내비가 가로 스크롤로 받는다. */}
      <Dialog>
        <DialogTrigger asChild>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="text-muted-foreground max-w-20 px-1.5 text-xs font-normal sm:max-w-64 sm:min-w-0 sm:shrink"
            title={me.user.email}
            aria-label={`계정 정보: ${me.user.email}`}
          >
            <span className="min-w-0 truncate">{me.user.email}</span>
          </Button>
        </DialogTrigger>
        <DialogContent aria-describedby={undefined}>
          <DialogHeader>
            <DialogTitle>계정 정보</DialogTitle>
          </DialogHeader>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
            {accountFields(me.user).map(({ label, value, mono }) => (
              <Fragment key={label}>
                <dt className="text-muted-foreground">{label}</dt>
                <dd className={cn('font-medium wrap-anywhere', mono && 'font-mono text-xs')}>{value}</dd>
              </Fragment>
            ))}
          </dl>
        </DialogContent>
      </Dialog>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="px-2 sm:px-2.5"
        disabled={logoutState === 'pending'}
        onClick={onLogout}
        title={logoutState === 'failed' ? '로그아웃하지 못했습니다. 다시 눌러 주세요.' : undefined}
      >
        {/* 좁은 화면에서는 아이콘만 보이고, 이름은 보조기기에 남는다. */}
        <LogOut aria-hidden className="sm:hidden" />
        <span className="sr-only sm:not-sr-only">
          {logoutState === 'failed' ? '다시 로그아웃' : '로그아웃'}
        </span>
      </Button>
      {logoutState === 'failed' ? (
        <span role="alert" className="sr-only">
          로그아웃하지 못했습니다. 다시 시도해 주세요.
        </span>
      ) : null}
    </div>
  );
}
