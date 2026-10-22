'use client';

// 5초 넘게 스스로 움직이는 요소에는 멈춤 장치가 있어야 한다(WCAG 2.2.2). 랜딩의 반복 움직임(맥박 · 이벤트 줄 ·
// 떠 있는 카드)을 한 번에 멈춘다. html의 data-motion으로 걸고, 화면을 떠나면 지워 다른 화면에 남기지 않는다.
// 버튼 이름이 상태에 따라 바뀌므로 aria-pressed는 달지 않는다. 둘 다 달면 "다시 재생, 눌림"처럼 뜻이 엇갈린다.

import { Pause, Play } from 'lucide-react';
import { useEffect, useState } from 'react';

export function MotionToggle() {
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    const root = document.documentElement;
    if (paused) root.dataset.motion = 'paused';
    else delete root.dataset.motion;
    return () => {
      delete root.dataset.motion;
    };
  }, [paused]);

  return (
    <button
      type="button"
      onClick={() => setPaused((p) => !p)}
      className="text-foreground-secondary hover:text-foreground inline-flex h-9 items-center gap-1.5 rounded-md px-2 text-xs focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
    >
      {paused ? <Play aria-hidden className="size-3.5" /> : <Pause aria-hidden className="size-3.5" />}
      {paused ? '움직임 다시 재생' : '움직임 멈춤'}
    </button>
  );
}
