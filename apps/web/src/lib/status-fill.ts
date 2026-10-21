// 상태 tone → 점 · 막대 · 칸의 칠과 그 옆 글자색. 배지(status-badge.tsx)와 같은 다섯 단계다(DESIGN.md §상태 색).
// 상태가 아닌 분류 tone(blue · purple · neutral)은 중립으로 접는다. 그래프에서 상태처럼 읽히지 않게.

import type { LabelTone } from './enum-labels.ts';

export interface StatusFill {
  fill: string;
  text: string;
}

const FILL: Partial<Record<LabelTone, StatusFill>> = {
  green: { fill: 'bg-status-good', text: 'text-badge-good-fg' },
  yellow: { fill: 'bg-status-warning', text: 'text-badge-warning-fg' },
  orange: { fill: 'bg-status-serious', text: 'text-badge-serious-fg' },
  red: { fill: 'bg-status-critical', text: 'text-badge-critical-fg' },
  gray: { fill: 'bg-status-idle', text: 'text-badge-idle-fg' },
};

const IDLE: StatusFill = { fill: 'bg-status-idle', text: 'text-badge-idle-fg' };

export function statusFill(tone: LabelTone): StatusFill {
  return FILL[tone] ?? IDLE;
}
