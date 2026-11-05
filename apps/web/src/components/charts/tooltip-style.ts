// recharts 그래프 말풍선과 마우스 위치 표시의 공용 모양(2026-11-03). 값은 전부 테마 변수라 주간 · 야간을 따른다.
// 막대 · 칸처럼 recharts가 아닌 그래프는 같은 모양의 `TipBox`(components/admin-dashboard/chart-parts.tsx)를 쓴다.
// 브라우저 기본 `title` 상자는 운영체제가 그려 테마를 따르지 않으므로 그래프에는 쓰지 않는다.

export const TOOLTIP_STYLE = {
  backgroundColor: 'var(--popover)',
  border: '1px solid var(--border)',
  borderRadius: '0.5rem',
  fontSize: '0.75rem',
  color: 'var(--popover-foreground)',
} as const;

/** 말풍선 글자는 계열 색이 아니라 본문 색이다. 야간 말풍선 위 진한 파랑 · 빨강 글자는 읽히지 않는다. */
export const TOOLTIP_ITEM_STYLE = { color: 'var(--popover-foreground)' } as const;
export const TOOLTIP_LABEL_STYLE = { color: 'var(--muted-foreground)', marginBottom: '0.25rem' } as const;

const HOVER_INK = 'color-mix(in oklch, var(--muted-foreground) 25%, transparent)';
/** 선 그래프의 세로 표시선. recharts 기본값(#ccc)은 야간에 밝게 튄다. */
export const LINE_CURSOR = { stroke: 'var(--muted-foreground)', strokeDasharray: '3 3' } as const;
/** 막대 그래프의 칸 강조. */
export const BAR_CURSOR = { fill: HOVER_INK } as const;
/** 말풍선이 범례(recharts가 차트 위에 따로 얹는 상자)에 덮이지 않게 한다. */
export const TOOLTIP_WRAPPER_STYLE = { zIndex: 10 } as const;
