'use client';

// 관리자 상단바의 알림 종(2026-10-29). 도입 신청 · 고객 문의가 들어오면 여기에 쌓인다(lib/support.ts).
// 플랫폼 계정에게만 그린다(gnb.tsx). 1분마다, 그리고 종을 열 때 다시 부른다. 실시간 이벤트(WebSocket)로 밀어 주는 것은
// 서버 계약이 정해진 뒤의 일이다. 알림을 누르면 그 건을 읽음으로 하고 목록 화면에서 그 건을 펼친다.
//
// 서버에 아직 알림 경로가 없으면(404 · 405) 오류 대신 준비 중이라고 적고 다시 부르지 않는다.

import { Bell, CheckCheck } from 'lucide-react';
import Link from 'next/link';
import { Popover } from 'radix-ui';
import { useEffect, useRef, useState } from 'react';

import { ApiError, getNotifications, markNotificationsRead } from '@/lib/api/client';
import { timeAgo } from '@/lib/customer-dashboard';
import {
  INQUIRIES_PATH,
  SIGNUP_REQUESTS_PATH,
  notYetServed,
  notificationHref,
  notificationText,
} from '@/lib/support';
import { cn } from '@/lib/utils';
import type { NotificationsResponse } from '@/types/api';

const POLL_MS = 60_000;

type State =
  | { kind: 'loading' }
  | { kind: 'ready'; data: NotificationsResponse }
  | { kind: 'unserved' }
  | { kind: 'error' };

export function NotificationBell() {
  const [state, setState] = useState<State>({ kind: 'loading' });
  const [nowMs, setNowMs] = useState(() => Date.now());

  /** 종을 열 때 · 읽음 처리 뒤 · [다시 시도]에서 다시 부른다. 아래 effect가 채운다. */
  const reload = useRef<() => void>(() => {});

  // 준비 중으로 바뀌면 타이머를 걷는다. 화면을 떠난 뒤 도착한 응답은 버린다(collection-indicator.tsx와 같은 모양).
  const unserved = state.kind === 'unserved';
  useEffect(() => {
    if (unserved) return;
    let alive = true;
    const load = () =>
      getNotifications().then(
        (data) => {
          if (!alive) return;
          setState({ kind: 'ready', data });
          setNowMs(Date.now());
        },
        (error: unknown) => {
          if (!alive) return;
          setState(error instanceof ApiError && notYetServed(error.httpStatus) ? { kind: 'unserved' } : { kind: 'error' });
        },
      );
    reload.current = () => void load();
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [unserved]);

  const unread = state.kind === 'ready' ? state.data.unread_count : 0;

  const read = async (ids: string[] | null) => {
    try {
      await markNotificationsRead(ids);
    } finally {
      reload.current();
    }
  };

  return (
    <Popover.Root onOpenChange={(open) => open && !unserved && reload.current()}>
      <Popover.Trigger
        aria-label={unread > 0 ? `알림, 읽지 않은 ${unread}건` : '알림'}
        className="focus-visible:ring-ring relative inline-flex size-9 items-center justify-center rounded-md text-white/80 hover:bg-white/8 hover:text-white focus-visible:ring-2 focus-visible:outline-none"
      >
        <Bell aria-hidden className="size-4.5" />
        {unread > 0 ? (
          <span
            aria-hidden
            className="bg-status-critical absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] leading-none font-bold text-white tabular-nums"
          >
            {unread > 9 ? '9+' : unread}
          </span>
        ) : null}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          className="bg-popover text-popover-foreground ring-foreground/10 z-50 flex w-[min(24rem,calc(100vw-2rem))] flex-col rounded-xl shadow-lg ring-1"
        >
          <div className="flex items-center justify-between border-b px-4 py-3">
            <span className="text-sm font-bold">알림</span>
            <button
              type="button"
              disabled={unread === 0}
              onClick={() => void read(null)}
              className="text-primary inline-flex items-center gap-1 text-xs font-semibold hover:underline disabled:pointer-events-none disabled:opacity-40"
            >
              <CheckCheck aria-hidden className="size-3.5" />
              모두 읽음
            </button>
          </div>

          <div className="max-h-96 overflow-y-auto">
            {state.kind === 'loading' ? <p className="text-muted-foreground px-4 py-6 text-sm">불러오는 중…</p> : null}
            {state.kind === 'unserved' ? (
              <p className="text-muted-foreground px-4 py-6 text-sm">알림 기능이 아직 서버에 열리지 않았습니다.</p>
            ) : null}
            {state.kind === 'error' ? (
              <div className="flex items-center justify-between gap-3 px-4 py-6 text-sm">
                <span className="text-muted-foreground">알림을 불러오지 못했습니다.</span>
                <button type="button" onClick={() => reload.current()} className="text-primary font-semibold hover:underline">
                  다시 시도
                </button>
              </div>
            ) : null}
            {state.kind === 'ready' && state.data.items.length === 0 ? (
              <p className="text-muted-foreground px-4 py-6 text-sm">새 알림이 없습니다.</p>
            ) : null}
            {state.kind === 'ready' && state.data.items.length > 0 ? (
              <ul>
                {state.data.items.map((item) => {
                  const text = notificationText(item);
                  const unreadItem = item.read_at === null;
                  return (
                    <li key={item.notification_id} className="border-b last:border-b-0">
                      <Popover.Close asChild>
                        <Link
                          href={notificationHref(item)}
                          onClick={() => unreadItem && void read([item.notification_id])}
                          className="hover:bg-muted/60 flex gap-3 px-4 py-3"
                        >
                          <span
                            aria-hidden
                            className={cn('mt-1.5 size-2 shrink-0 rounded-full', unreadItem ? 'bg-primary' : 'bg-transparent')}
                          />
                          <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                            <span className={cn('truncate text-sm', unreadItem ? 'font-semibold' : 'text-foreground-secondary')}>
                              {unreadItem ? <span className="sr-only">읽지 않음, </span> : null}
                              {text.title}
                            </span>
                            <span className="text-muted-foreground truncate text-xs">{text.detail}</span>
                          </span>
                          <span className="text-muted-foreground shrink-0 text-xs">{timeAgo(item.created_at, nowMs)}</span>
                        </Link>
                      </Popover.Close>
                    </li>
                  );
                })}
              </ul>
            ) : null}
          </div>

          <div className="flex border-t text-xs font-semibold">
            <Popover.Close asChild>
              <Link href={SIGNUP_REQUESTS_PATH} className="text-primary flex-1 px-4 py-2.5 text-center hover:underline">
                도입 신청함
              </Link>
            </Popover.Close>
            <Popover.Close asChild>
              <Link href={INQUIRIES_PATH} className="text-primary flex-1 border-l px-4 py-2.5 text-center hover:underline">
                고객 문의함
              </Link>
            </Popover.Close>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
