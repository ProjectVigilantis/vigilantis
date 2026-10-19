'use client';

// GNB 우측 현재 사용자 · 로그아웃(#492 PR 1). 접근 보호(미로그인 리다이렉트)는 PR 2 소관이라 여기서는
// 상태만 보여 준다 — 로그인돼 있으면 이메일과 [로그아웃], 아니면 [로그인] 링크.
//
// 유휴 연장: 로그인 상태에서 **실제 사용자 입력**(pointerdown · keydown)이 있을 때만 `POST /auth/activity`를
// 최대 1분에 1번 보낸다. 타이머 · 포커스 · 화면 표시 · 데이터 갱신으로는 보내지 않는다 — 자동 요청이
// 유휴 만료를 늘리면 자리를 비운 화면의 세션이 끝나지 않는다.

import { LogOut } from 'lucide-react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { createActivityThrottle } from '@/lib/activity-throttle';
import { ApiError, getMe, logout, recordActivity } from '@/lib/api/client';
import type { CurrentUserResponse } from '@/types/api';

type MeState =
  | { kind: 'loading' }
  /** `/me` 자체를 확인하지 못했다(네트워크 · 5xx). 로그인 여부를 단정하지 않고 아무것도 그리지 않는다. */
  | { kind: 'unknown' }
  | { kind: 'anonymous' }
  | { kind: 'user'; user: CurrentUserResponse };

const isUnauthorized = (error: unknown) => error instanceof ApiError && error.httpStatus === 401;

export function UserMenu() {
  const router = useRouter();
  const [me, setMe] = useState<MeState>({ kind: 'loading' });
  const [logoutState, setLogoutState] = useState<'idle' | 'pending' | 'failed'>('idle');

  useEffect(() => {
    let alive = true;
    getMe()
      .then((user) => alive && setMe(user ? { kind: 'user', user } : { kind: 'anonymous' }))
      .catch(() => alive && setMe({ kind: 'unknown' }));
    return () => {
      alive = false;
    };
  }, []);

  const signedIn = me.kind === 'user';
  useEffect(() => {
    if (!signedIn) return;
    const shouldSend = createActivityThrottle();
    const onInput = () => {
      if (!shouldSend()) return;
      recordActivity().catch((error: unknown) => {
        // 세션이 이미 끝났다 — 표시만 바꾼다(리다이렉트는 PR 2). 그 밖의 실패는 다음 입력이 다시 보낸다.
        if (isUnauthorized(error)) setMe({ kind: 'anonymous' });
      });
    };
    const options = { capture: true, passive: true } as const;
    window.addEventListener('pointerdown', onInput, options);
    window.addEventListener('keydown', onInput, options);
    return () => {
      window.removeEventListener('pointerdown', onInput, options);
      window.removeEventListener('keydown', onInput, options);
    };
  }, [signedIn]);

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
      {/* 좁은 화면(< sm)에서는 이메일 폭을 고정하고 줄인다 — 줄어드는 몫은 내비가 가로 스크롤로 받는다. */}
      <span
        className="text-muted-foreground max-w-20 shrink-0 truncate text-xs sm:max-w-64 sm:min-w-0 sm:shrink"
        title={me.user.email}
      >
        {me.user.email}
      </span>
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
