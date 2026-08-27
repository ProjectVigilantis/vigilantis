'use client';

// CMN-001 실시간 연결 — 화면설계서 v1.5 §4.8. **WebSocket 수명주기를 이 컴포넌트 하나가 소유합니다.**
// 페이지가 각자 연결을 열지 않습니다(§4.8 목적). 판단은 lib/realtime-events가 하고 여기는 전송·배급만 합니다.

import { useRouter } from 'next/navigation';
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';

import {
  SeenEvents,
  actionFor,
  backoffMs,
  websocketUrl,
  type RealtimeAction,
} from '@/lib/realtime-events';
import type { WsEvent } from '@/types/api';

export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'disabled';

interface RealtimeContextValue {
  connection: ConnectionState;
  /** 수동 [재연결] — 백오프가 길어졌을 때 관제자가 즉시 다시 시도한다(§4.8 4). */
  reconnect: () => void;
  /** `EXECUTION_UPDATED` 구독. ACT-002가 자기 실행만 골라 쓴다. 해지 함수를 돌려준다. */
  subscribeExecution: (listener: (action: Extract<RealtimeAction, { kind: 'execution' }>) => void) => () => void;
  /** `INCIDENT_CREATED`·`INCIDENT_UPDATED` 구독. Toast가 `toast` flag를 보고 판단한다. */
  subscribeIncident: (listener: (action: Extract<RealtimeAction, { kind: 'refresh' }>) => void) => () => void;
}

const RealtimeContext = createContext<RealtimeContextValue>({
  connection: 'disabled',
  reconnect: () => {},
  subscribeExecution: () => () => {},
  subscribeIncident: () => () => {},
});

export function useRealtime(): RealtimeContextValue {
  return useContext(RealtimeContext);
}

export function RealtimeProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  /**
   * 초기값은 **소켓을 열기 전에** 정해 둔다 — effect 안에서 동기로 setState하면 React Compiler
   * 규칙에 걸리고, 이 값은 빌드 시 인라인되는 env에서만 파생하므로 서버·클라 결과가 같다.
   * 이후 전이는 전부 소켓 콜백(onopen·onclose)이 만든다.
   */
  const [connection, setConnection] = useState<ConnectionState>(() =>
    websocketUrl(process.env.NEXT_PUBLIC_API_BASE_URL) === null ? 'disabled' : 'connecting',
  );

  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  /**
   * 다음 `onopen`이 **재연결**인지. 백오프 카운터(`attemptRef`)에 겸직시키면 수동 [재연결]이
   * 카운터를 0으로 되돌린 뒤 붙으므로 판정이 항상 `false`가 되어 REST 복구가 빠진다(PR #181 리뷰).
   */
  const resumedRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seenRef = useRef(new SeenEvents());
  const listenersRef = useRef(new Set<(a: Extract<RealtimeAction, { kind: 'execution' }>) => void>());
  const incidentListenersRef = useRef(new Set<(a: Extract<RealtimeAction, { kind: 'refresh' }>) => void>());
  /** 언마운트 후 재연결이 붙지 않게 하는 표식. */
  const aliveRef = useRef(true);
  /** `connect`가 자기 자신을 예약해야 해서 참조로 끊는다(useCallback 자기 참조 금지). */
  const connectRef = useRef<() => void>(() => {});

  const subscribeExecution = useCallback(
    (listener: (a: Extract<RealtimeAction, { kind: 'execution' }>) => void) => {
      listenersRef.current.add(listener);
      return () => {
        listenersRef.current.delete(listener);
      };
    },
    [],
  );

  const subscribeIncident = useCallback(
    (listener: (a: Extract<RealtimeAction, { kind: 'refresh' }>) => void) => {
      incidentListenersRef.current.add(listener);
      return () => {
        incidentListenersRef.current.delete(listener);
      };
    },
    [],
  );

  /** 백오프만큼 기다렸다 다시 붙는다. 30s까지 늘어나면 수동 버튼을 노출한다(§4.8 4). */
  const scheduleRetry = useCallback(() => {
    const wait = backoffMs(attemptRef.current);
    attemptRef.current += 1;
    resumedRef.current = true;
    setConnection(wait >= 30_000 ? 'closed' : 'reconnecting');
    timerRef.current = setTimeout(() => connectRef.current(), wait);
  }, []);

  const connect = useCallback(() => {
    const url = websocketUrl(process.env.NEXT_PUBLIC_API_BASE_URL);
    // mock 단계에는 WS가 없다 — 초기값이 이미 `disabled`이므로 상태를 건드리지 않는다.
    if (url === null || !aliveRef.current) return;

    let socket: WebSocket;
    try {
      // 생성자가 던지는 경로가 있다 — HTTPS 페이지에서 `ws://`(mixed content)면 SecurityError다.
      // mount effect에서 터지면 인디케이터가 아니라 **화면 전체가 에러 바운더리로** 가고,
      // 재시도 타이머 안에서 터지면 예외가 잡히지 않아 재시도 체인이 조용히 끊긴다(PR #181 리뷰).
      socket = new WebSocket(url);
    } catch {
      scheduleRetry();
      return;
    }
    socketRef.current = socket;

    socket.onopen = () => {
      // 재조회는 **재연결일 때만** 한다 — 첫 진입에서 부르면 방금 그린 페이지를 RSC로 한 번 더 받는다.
      const reconnected = resumedRef.current;
      resumedRef.current = false;
      attemptRef.current = 0;
      setConnection('open');
      // 재연결 성공 시 목록 API를 재조회해 상태를 교체한다 — snapshot·replay 없음이 계약이다
      // (§4.8 3 · ws.py "과거 이벤트 재생은 보장하지 않는다"). 끊긴 동안의 변화가 여기서 복구된다.
      if (reconnected) router.refresh();
    };

    socket.onmessage = (message) => {
      let event: WsEvent;
      try {
        event = JSON.parse(String(message.data)) as WsEvent;
      } catch {
        return; // 계약 밖 프레임은 조용히 버린다 — 연결을 끊을 이유는 아니다
      }
      // 같은 event_id 재수신은 중복 반영하지 않는다(수신 측 멱등 키).
      if (!seenRef.current.accept(event.event_id)) return;

      const action = actionFor(event);
      if (action.kind === 'execution') {
        for (const listener of listenersRef.current) listener(action);
        // 최종 상태면 REST로 화면 값을 확정한다(§4.8 전달 원칙).
        if (action.toast) router.refresh();
        return;
      }
      // 인시던트 계열은 값을 들고 오지 않는다 — 해당 화면을 재조회하고, Toast에도 배달한다.
      router.refresh();
      for (const listener of incidentListenersRef.current) listener(action);
    };

    // 연결·구독 오류 이벤트는 계약에 없다 — 상태는 수명주기(onclose·onerror)로만 표시한다(§4.8).
    socket.onerror = () => socket.close();

    socket.onclose = () => {
      // 옛 소켓의 지연 close가 새 소켓 참조를 지우고 재시도를 한 번 더 예약하는 것을 막는다.
      // `reconnect()`가 close 직후 동기로 connect()를 부르므로 이 창이 실제로 열린다(PR #181 리뷰).
      if (socketRef.current !== socket) return;
      socketRef.current = null;
      if (!aliveRef.current) return;
      scheduleRetry();
    };
  }, [router, scheduleRetry]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  const reconnect = useCallback(() => {
    if (timerRef.current !== null) clearTimeout(timerRef.current);
    attemptRef.current = 0;
    // 백오프는 처음부터 다시 세지만 "끊겼다 붙는 중"이라는 사실은 남긴다 — 이 줄이 없으면
    // 수동 재연결로 복구한 세션만 끊긴 동안의 변화를 못 받는다(PR #181 리뷰).
    resumedRef.current = true;
    const previous = socketRef.current;
    socketRef.current = null; // 옛 소켓의 지연 close가 새 연결을 건드리지 못하게 먼저 끊는다
    previous?.close();
    // 시도가 진행 중임을 즉시 보여준다 — 아니면 in-flight 동안에도 `연결 끊김`이 떠 있다.
    setConnection('connecting');
    connect();
  }, [connect]);

  // 앱 진입 시 1회 연결하고 페이지 이동 중 유지한다(§4.8 1) — layout에 두어 라우트가 바뀌어도
  // 이 컴포넌트가 언마운트되지 않는다.
  useEffect(() => {
    aliveRef.current = true;
    connect();
    return () => {
      aliveRef.current = false;
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      socketRef.current?.close();
    };
  }, [connect]);

  return (
    <RealtimeContext.Provider
      value={{ connection, reconnect, subscribeExecution, subscribeIncident }}
    >
      {children}
    </RealtimeContext.Provider>
  );
}
