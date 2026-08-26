'use client';

// CMN-001 Toast — 화면설계서 v1.5 §4.8. 우하단 고정·8초·최대 3개·동일 incident는 최신 1건 대체.

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { useRealtime } from '@/components/realtime-provider';
import { EXECUTION_STATUS_LABELS } from '@/lib/enum-labels';
import type { ExecutionStatus } from '@/types/api';

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
  const { subscribeExecution } = useRealtime();
  const [toasts, setToasts] = useState<Toast[]>([]);

  useEffect(() => {
    return subscribeExecution((action) => {
      // 최종 상태에서만 띄운다 — 중간 전이마다 띄우면 한 실행이 여러 회 알림이 된다(§4.8).
      if (!action.toast) return;
      const entry = EXECUTION_STATUS_LABELS[action.status as ExecutionStatus];
      setToasts((prev) => {
        // 동일 incident_id의 Toast는 최신 1건으로 대체하고, 최대 3개를 넘으면 오래된 것부터 뺀다.
        const kept = prev.filter((t) => t.incidentId !== action.incidentId);
        const next: Toast = {
          id: action.executionId,
          incidentId: action.incidentId,
          title: `조치 ${entry?.label ?? action.status}`,
          body: `실행 ${action.executionId}`,
        };
        return [...kept, next].slice(-MAX_STACK);
      });
    });
  }, [subscribeExecution]);

  // 각 Toast를 8초 뒤에 뺀다. id 기준이라 대체된 Toast는 자기 타이머만 정리된다.
  useEffect(() => {
    if (toasts.length === 0) return;
    const timers = toasts.map((t) =>
      setTimeout(() => setToasts((prev) => prev.filter((x) => x.id !== t.id)), HOLD_MS),
    );
    return () => timers.forEach(clearTimeout);
  }, [toasts]);

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
