'use client';

// 현재 세션(`GET /auth/me`)을 한 곳에서 들고 있는 Provider(#492 PR 2). GNB 사용자 메뉴 · 조치 버튼의 역할 판정 ·
// 만료 안내가 같은 값을 읽는다. 로그인 화면에는 두르지 않는다(app-shell.tsx).
//
// 유휴 연장: 로그인 상태에서 **실제 사용자 입력**(pointerdown · keydown)이 있을 때만 `POST /auth/activity`를
// 최대 1분에 1번 보낸다. 타이머 · 포커스 · 화면 표시 · 데이터 갱신으로는 보내지 않는다 — 자동 요청이
// 유휴 만료를 늘리면 자리를 비운 화면의 세션이 끝나지 않는다. 연장에 성공하면 `/me`를 다시 읽어
// 새 만료 시각을 받는다(`/me` 자체는 유휴를 늘리지 않는다).

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { createActivityThrottle } from '@/lib/activity-throttle';
import { getMe, recordActivity } from '@/lib/api/client';
import { canOperate, operateBlockedReason } from '@/lib/roles';
import { singleFlight } from '@/lib/single-flight';
import type { CurrentUserResponse } from '@/types/api';

export type SessionState =
  | { kind: 'loading' }
  /** `/me` 자체를 확인하지 못했다(네트워크 · 5xx). 로그인 여부를 단정하지 않는다. */
  | { kind: 'unknown' }
  /** 401 — 세션이 없다. 로그인 이동은 API 계층이 이미 걸었다(lib/api/client.ts). */
  | { kind: 'anonymous' }
  | { kind: 'user'; user: CurrentUserResponse };

interface SessionContextValue {
  session: SessionState;
  /** `/me`를 다시 읽는다. 읽은 사용자(401이면 null)를 돌려주고, 확인하지 못하면 던진다(상태는 그대로). */
  refresh: () => Promise<CurrentUserResponse | null>;
  /** 유휴 연장 → 성공하면 `/me` 재조회. 실패는 호출부에 던진다(401은 API 계층이 로그인으로 보낸다). */
  extend: () => Promise<void>;
}

const SessionContext = createContext<SessionContextValue>({
  session: { kind: 'loading' },
  refresh: async () => null,
  extend: async () => {},
});

export function useSession(): SessionContextValue {
  return useContext(SessionContext);
}

/**
 * 조치 버튼(실행 · 복구 · 종료)을 막는 이유. 계정을 아직 모르면 막는다 — 서버 403이 실제 차단이다.
 * 플랫폼 계정은 호환 역할(ADMIN)이 아니라 등급 · 담당 배정으로 판정한다(lib/roles.ts `canOperate`).
 */
export function useOperateBlockedReason(): string | null {
  const { session } = useSession();
  return operateBlockedReason(session.kind === 'user' ? session.user : null);
}

export function useCanOperate(): boolean {
  const { session } = useSession();
  return session.kind === 'user' && canOperate(session.user);
}

const toState = (user: CurrentUserResponse | null): SessionState =>
  user ? { kind: 'user', user } : { kind: 'anonymous' };

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<SessionState>({ kind: 'loading' });

  useEffect(() => {
    let alive = true;
    getMe()
      .then((user) => alive && setSession(toState(user)))
      .catch(() => alive && setSession({ kind: 'unknown' }));
    return () => {
      alive = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    const user = await getMe();
    setSession(toState(user));
    return user;
  }, []);

  // 동시 호출은 진행 중인 연장 하나를 나눠 받는다 — [계속 사용] 클릭이 입력 연장(pointerdown)과
  // 버튼 연장을 함께 일으켜도 `POST /auth/activity`는 한 번이다(lib/single-flight.ts).
  const extend = useMemo(
    () =>
      singleFlight(async () => {
        await recordActivity();
        await refresh();
      }),
    [refresh],
  );

  const signedIn = session.kind === 'user';
  useEffect(() => {
    if (!signedIn) return;
    const shouldSend = createActivityThrottle();
    const onInput = () => {
      if (!shouldSend()) return;
      // 401은 API 계층이 로그인으로 보낸다. 그 밖의 실패는 다음 입력이 다시 보낸다.
      extend().catch(() => {});
    };
    const options = { capture: true, passive: true } as const;
    window.addEventListener('pointerdown', onInput, options);
    window.addEventListener('keydown', onInput, options);
    return () => {
      window.removeEventListener('pointerdown', onInput, options);
      window.removeEventListener('keydown', onInput, options);
    };
  }, [signedIn, extend]);

  return (
    <SessionContext.Provider value={{ session, refresh, extend }}>{children}</SessionContext.Provider>
  );
}
