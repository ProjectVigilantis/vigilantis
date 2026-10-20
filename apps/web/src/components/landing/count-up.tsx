'use client';

// 화면에 들어오면 0에서 목표 값까지 세어 올라가는 숫자. 서버 HTML과 JS가 없을 때는 처음부터 목표 값이다.
// 모션 줄이기 설정이면 세지 않는다.

import { useEffect, useRef } from 'react';

const DURATION_MS = 1200;

export function CountUp({ value, suffix = '' }: { value: number; suffix?: string }) {
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (el === null || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    let frame = 0;
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry?.isIntersecting) return;
      observer.disconnect();
      const start = performance.now();
      const tick = (t: number) => {
        const p = Math.min(1, (t - start) / DURATION_MS);
        // 끝으로 갈수록 느려진다(easeOutCubic).
        el.textContent = `${Math.round(value * (1 - (1 - p) ** 3))}${suffix}`;
        if (p < 1) frame = requestAnimationFrame(tick);
      };
      el.textContent = `0${suffix}`;
      frame = requestAnimationFrame(tick);
    });
    observer.observe(el);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [value, suffix]);

  // 세는 도중의 값을 화면 낭독기가 읽지 않게, 움직이는 숫자는 숨기고 최종 값을 따로 둔다.
  return (
    <>
      <span ref={ref} aria-hidden className="tabular-nums">
        {value}
        {suffix}
      </span>
      <span className="sr-only">
        {value}
        {suffix}
      </span>
    </>
  );
}
