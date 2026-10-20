'use client';

// 스크롤 등장 효과. 안쪽의 `[data-reveal]` 요소 가운데 **처음에 화면 아래에 있는 것만** 숨겼다가 보일 때 드러낸다.
// 서버 HTML은 전부 보이는 상태라 JS가 없거나 늦어도 내용이 가려지지 않는다. 처음부터 화면 안에 있는 요소는
// 건드리지 않아 깜빡이지 않는다. 모션 줄이기 설정이면 아무것도 숨기지 않는다(globals.css도 같은 조건에서 멈춘다).

import { useEffect, useRef } from 'react';

export function RevealRoot({ children, className }: { children: React.ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const root = ref.current;
    if (root === null || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          (entry.target as HTMLElement).dataset.state = 'shown';
          observer.unobserve(entry.target);
        }
      },
      { rootMargin: '0px 0px -10% 0px' },
    );

    for (const el of root.querySelectorAll<HTMLElement>('[data-reveal]')) {
      if (el.getBoundingClientRect().top < window.innerHeight * 0.9) continue;
      el.dataset.state = 'hidden';
      observer.observe(el);
    }
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={ref} className={className}>
      {children}
    </div>
  );
}
