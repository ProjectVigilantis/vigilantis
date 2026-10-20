'use client';

// 세션 만료 안내(#492 PR 2). 시각 계산은 lib/session-expiry.ts가 하고 여기는 그리기 · 타이머 · 이동만 한다.
// 자동 연장은 없다 — [계속 사용]을 눌러야 `POST /auth/activity`가 나간다.
// ponytail: 띠 모양 · 위치 · 문구 다듬기는 FE 담당 몫이다. 지금은 기존 토큰으로 된 최소 마크업이다.

import { useEffect, useRef, useState } from 'react';

import { useSession } from '@/components/auth/session-provider';
import { Button } from '@/components/ui/button';
import { navigateToLogin } from '@/lib/auth-redirect';
import { expirySchedule } from '@/lib/session-expiry';

export function SessionExpiryNotice() {
  const { session, refresh, extend } = useSession();
  // 결과는 다음 변경 시각(nextChangeAt)까지 변하지 않는다 — 그 시각에만 시계를 갱신한다.
  const [now, setNow] = useState(() => Date.now());
  const [extending, setExtending] = useState(false);
  const leavingRef = useRef(false);

  const info = session.kind === 'user' ? session.user.session : null;
  const schedule = info === null ? null : expirySchedule(info, now);
  const nextChangeAt = schedule?.nextChangeAt ?? null;
  const expired = schedule?.expired ?? false;

  useEffect(() => {
    if (nextChangeAt === null) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.max(0, nextChangeAt - Date.now()));
    return () => clearTimeout(timer);
  }, [nextChangeAt]);

  // 만료 시각에 닿았다. 다른 탭에서 연장했을 수 있어 `/me`로 한 번 확인하고, 여전히 끝났으면 로그인으로 보낸다.
  useEffect(() => {
    if (!expired || leavingRef.current) return;
    leavingRef.current = true;
    refresh()
      .then((user) => {
        if (user !== null && !expirySchedule(user.session, Date.now()).expired) {
          leavingRef.current = false;
          setNow(Date.now());
          return;
        }
        navigateToLogin();
      })
      .catch(() => navigateToLogin());
  }, [expired, refresh]);

  if (schedule === null || (schedule.idleMinutesLeft === null && !schedule.absoluteNotice)) return null;

  async function onExtend() {
    setExtending(true);
    try {
      await extend();
    } catch {
      // 401은 API 계층이 로그인으로 보낸다. 그 밖의 실패는 다시 누를 수 있게 둔다.
    } finally {
      setExtending(false);
    }
  }

  return (
    <div role="status" className="flex flex-col gap-1 border-b bg-muted px-4 py-2 text-sm">
      {schedule.absoluteNotice ? (
        <p>최대 사용 시간(8시간)이 곧 끝납니다. 다시 로그인해야 합니다.</p>
      ) : null}
      {schedule.idleMinutesLeft !== null ? (
        <div className="flex flex-wrap items-center gap-2">
          <p>{schedule.idleMinutesLeft}분 후 자동 로그아웃됩니다.</p>
          <Button type="button" size="sm" variant="outline" disabled={extending} onClick={onExtend}>
            계속 사용
          </Button>
        </div>
      ) : null}
    </div>
  );
}
