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
}

const RealtimeContext = createContext<RealtimeContextValue>({
  connection: 'disabled',
  reconnect: () => {},
  subscribeExecution: () => () => {},
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
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seenRef = useRef(new SeenEvents());
  const listenersRef = useRef(new Set<(a: Extract<RealtimeAction, { kind: 'execution' }>) => void>());
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

  const connect = useCallback(() => {
    const url = websocketUrl(process.env.NEXT_PUBLIC_API_BASE_URL);
    // mock 단계에는 WS가 없다 — 초기값이 이미 `disabled`이므로 상태를 건드리지 않는다.
    if (url === null || !aliveRef.current) return;

    const socket = new WebSocket(url);
    socketRef.current = socket;

    socket.onopen = () => {
      attemptRef.current = 0;
      setConnection('open');
      // 재연결 성공 시 목록 API를 재조회해 상태를 교체한다 — snapshot·replay 없음이 계약이다
      // (§4.8 3 · ws.py "과거 이벤트 재생은 보장하지 않는다"). 끊긴 동안의 변화가 여기서 복구된다.
      router.refresh();
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
      // 인시던트 계열은 값을 들고 오지 않는다 — 해당 화면을 재조회한다.
      router.refresh();
    };

    // 연결·구독 오류 이벤트는 계약에 없다 — 상태는 수명주기(onclose·onerror)로만 표시한다(§4.8).
    socket.onerror = () => socket.close();

    socket.onclose = () => {
      socketRef.current = null;
      if (!aliveRef.current) return;
      const wait = backoffMs(attemptRef.current);
      attemptRef.current += 1;
      // 30s까지 늘어났으면 자동 재시도는 계속하되 수동 버튼을 노출한다(§4.8 4).
      setConnection(wait >= 30_000 ? 'closed' : 'reconnecting');
      timerRef.current = setTimeout(() => connectRef.current(), wait);
    };
  }, [router]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  const reconnect = useCallback(() => {
    if (timerRef.current !== null) clearTimeout(timerRef.current);
    attemptRef.current = 0;
    socketRef.current?.close();
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
    <RealtimeContext.Provider value={{ connection, reconnect, subscribeExecution }}>
      {children}
    </RealtimeContext.Provider>
  );
}
