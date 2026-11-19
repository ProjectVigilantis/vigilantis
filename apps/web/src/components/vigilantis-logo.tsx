// Vigilantis 로고(방패 속 부엉이 + 구름, 2026-11-19 확정). 그림은 public/vigilantis-logo.png의 알파 마스크이고
// 색은 글자색(currentColor)을 따른다 — 쓰는 자리에서 text-white 등으로 정한다.
// 탭 아이콘은 src/app/icon.svg · favicon.ico · apple-icon.png가 같은 그림으로 따로 있다.

import { cn } from '@/lib/utils';

const MASK = 'url(/vigilantis-logo.png) center / contain no-repeat';

export function VigilantisLogo({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={cn('inline-block shrink-0 bg-current', className)}
      style={{ mask: MASK, WebkitMask: MASK }}
    />
  );
}
