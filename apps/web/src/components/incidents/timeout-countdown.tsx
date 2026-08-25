'use client';

// B-Medium 타임아웃 카운트다운 — 화면설계서 v1.5 §4.5. 안내용이며 발동 판정은 서버가 합니다.

import { useSyncExternalStore } from 'react';

/** 서버 렌더는 시각에 의존할 수 없다 — 전환 직후 값(60초)을 스냅샷으로 쓰고 하이드레이션 후 보정한다. */
const FULL_WINDOW_SECONDS = 60;

function remainingSeconds(deadline: number): number {
  return Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
}

/** 1초마다 구독자에게 알린다. 스토어 값은 아래 getSnapshot이 그때그때 계산한다. */
function subscribeToTick(onChange: () => void): () => void {
  const timer = setInterval(onChange, 1000);
  return () => clearInterval(timer);
}

/**
 * 남은 시간을 1초 간격으로 그린다. **0에 닿아도 화면은 아무것도 실행하지 않는다**(§4.5) —
 * 자동 격리는 서버가 판정하고, 화면은 `TIMEOUT_ISOLATION_1M` 이벤트를 받아 전환한다.
 * 몇 초 오차는 감수한다(설계서 명시).
 *
 * `useState` + `useEffect`로 짜면 서버와 클라이언트가 서로 다른 `Date.now()`로 초기값을 계산해
 * **하이드레이션 불일치**가 나고, React가 그 트리를 버리면서 타이머가 아예 붙지 않는다.
 * 시각 의존 값은 서버 스냅샷을 고정할 수 있는 `useSyncExternalStore`로 읽는다.
 */
export function TimeoutCountdown({ deadline }: { deadline: number }) {
  const left = useSyncExternalStore(
    subscribeToTick,
    () => remainingSeconds(deadline),
    () => FULL_WINDOW_SECONDS,
  );

  if (left === 0) {
    return <span className="text-danger font-medium tabular-nums">자동 격리 대기 중</span>;
  }
  return <span className="text-danger font-medium tabular-nums">{left}초 후 자동 격리</span>;
}
