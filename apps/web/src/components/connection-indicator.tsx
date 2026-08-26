'use client';

// CMN-001 연결 인디케이터 — 화면설계서 v1.5 §4.8. GNB 우측에 붙습니다.
// 옆의 `수집 대상` 인디케이터는 CMN-001이 아니다 — `GET /assets` 봉투 소관이다(§3.1).

import { useRealtime, type ConnectionState } from '@/components/realtime-provider';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/**
 * §4.8의 3상태에 `disabled` 하나를 더 둔다 — mock 단계에는 WS 엔드포인트가 없어(2026-08-14 확정)
 * "연결 끊김"으로 그리면 **고칠 수 있는 장애처럼** 읽힌다. 미연동임을 문구로 구분한다.
 *
 * 연결·구독 오류 이벤트는 계약에 없다(§4.8) — 상태는 소켓 수명주기로만 표시하고,
 * **실행 상태로 오해되지 않게** 문구를 실행 어휘와 겹치지 않게 골랐다.
 */
const PRESENTATION: Record<ConnectionState, { glyph: string; label: string; tone: string }> = {
  open: { glyph: '●', label: '실시간 연결됨', tone: 'text-emerald-600 dark:text-emerald-400' },
  connecting: { glyph: '◐', label: '연결 중…', tone: 'text-amber-600 dark:text-amber-400' },
  reconnecting: { glyph: '◐', label: '재연결 중…', tone: 'text-amber-600 dark:text-amber-400' },
  closed: { glyph: '○', label: '연결 끊김', tone: 'text-muted-foreground' },
  disabled: { glyph: '○', label: '실시간 미연동', tone: 'text-muted-foreground' },
};

export function ConnectionIndicator() {
  const { connection, reconnect } = useRealtime();
  const view = PRESENTATION[connection];

  return (
    <span className="ml-auto flex shrink-0 items-center gap-1.5 text-xs whitespace-nowrap">
      <span aria-hidden className={cn(view.tone, connection === 'reconnecting' && 'animate-pulse')}>
        {view.glyph}
      </span>
      <span className={view.tone}>{view.label}</span>
      {/* 재연결 실패가 지속되면 수동 버튼을 노출한다(§4.8 4). 자동 재시도는 계속 돈다. */}
      {connection === 'closed' ? (
        <Button type="button" size="sm" variant="outline" className="h-6 px-2" onClick={reconnect}>
          재연결
        </Button>
      ) : null}
    </span>
  );
}
