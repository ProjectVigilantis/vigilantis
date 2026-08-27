'use client';

// CMN-001 Toast — 화면설계서 v1.5 §4.8. 우하단 고정·8초·최대 3개·동일 incident는 최신 1건 대체.

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import { useRealtime } from '@/components/realtime-provider';
import { EXECUTION_STATUS_LABELS } from '@/lib/enum-labels';

/** 위치는 우하단으로 확정(○ FE 판단) — 상단은 GNB·페이지 제목과 겹쳐 관제 화면을 가린다(§4.8). */
const HOLD_MS = 8_000;
const MAX_STACK = 3;

interface Toast {
  id: string;
  incidentId: string;
  title: string;
  body: string;
}

export function ToastStack() {
  const { subscribeExecution, subscribeIncident } = useRealtime();
  const [toasts, setToasts] = useState<Toast[]>([]);
  /** Toast별 제거 타이머. 스택이 바뀔 때마다 전부 재생성하면 기존 Toast 수명이 8초씩 늘어난다. */
  const timersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  function push(next: Toast) {
    setToasts((prev) => {
      // 동일 incident_id의 Toast는 최신 1건으로 대체하고, 3개를 넘으면 오래된 것부터 뺀다(§4.8).
      const kept = prev.filter((t) => t.incidentId !== next.incidentId);
      return [...kept, next].slice(-MAX_STACK);
    });
  }

  // 실행 상태 — 최종 상태에서만 띄운다(중간 전이마다 띄우면 한 실행이 여러 회 알림이 된다).
  useEffect(
    () =>
      subscribeExecution((action) => {
        if (!action.toast) return;
        push({
          id: action.executionId,
          incidentId: action.incidentId,
          title: `조치 ${EXECUTION_STATUS_LABELS[action.status]?.label ?? action.status}`,
          body: `실행 ${action.executionId}`,
        });
      }),
    [subscribeExecution],
  );

  // 인시던트 — `INCIDENT_CREATED`만 띄운다. `INCIDENT_UPDATED`는 재조회만 하고 알리지 않는다(§4.8).
  useEffect(
    () =>
      subscribeIncident((action) => {
        if (!action.toast) return;
        push({
          id: `incident:${action.incidentId}`,
          incidentId: action.incidentId,
          title: '보안 위협 감지',
          body: action.incidentId,
        });
      }),
    [subscribeIncident],
  );

  // 새로 들어온 Toast에만 타이머를 건다 — 이미 떠 있는 것의 수명은 건드리지 않는다.
  useEffect(() => {
    const timers = timersRef.current;
    for (const toast of toasts) {
      if (timers.has(toast.id)) continue;
      timers.set(
        toast.id,
        setTimeout(() => {
          timers.delete(toast.id);
          setToasts((prev) => prev.filter((x) => x.id !== toast.id));
        }, HOLD_MS),
      );
    }
    // 스택에서 빠진(대체·초과 제거) Toast의 타이머는 정리한다.
    for (const [id, handle] of timers) {
      if (!toasts.some((t) => t.id === id)) {
        clearTimeout(handle);
        timers.delete(id);
      }
    }
  }, [toasts]);

  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const handle of timers.values()) clearTimeout(handle);
      timers.clear();
    };
  }, []);

  return (
    <div
      aria-live="polite"
      className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-80 flex-col gap-2"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          role="status"
          className="border-border bg-card pointer-events-auto rounded-md border p-3 text-sm shadow-lg"
        >
          <p className="font-medium">{toast.title}</p>
          <p className="text-muted-foreground mt-0.5 text-xs break-all">{toast.body}</p>
          <Link
            href={`/incidents/${encodeURIComponent(toast.incidentId)}`}
            className="mt-1.5 inline-block text-xs underline underline-offset-4"
          >
            상세 보기 →
          </Link>
        </div>
      ))}
    </div>
  );
}
