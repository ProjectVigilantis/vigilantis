// 지표 띠 — 메인 대시보드(DSH-001)와 자산 관제(AST-001)가 공유하는 숫자 블록 한 줄.
//
// **`'use client'`를 붙이지 않는다.** 훅이 없어 서버 컴포넌트(대시보드)에서도, 클라이언트 컴포넌트
// (자산 관제)에서도 그대로 쓰인다 — `onClick`을 주는 호출부는 이미 자기 클라이언트 경계 안이라
// 여기서 경계를 새로 그으면 대시보드까지 불필요하게 클라이언트 번들로 끌려 들어간다.
//
// 두 화면이 같은 모양을 쓰는 이유: 관제자가 화면을 옮겨도 "왼쪽 위 띠 = 지금 몇 건"이라는 읽는
// 법이 같아야 한다. 타일을 화면마다 따로 만들면 여백·글자 크기가 갈려 같은 것이 달라 보인다.

import { NO_VALUE } from '@/lib/enum-labels';
import { cn } from '@/lib/utils';

/** 빨강은 `--danger` 하나뿐이다(§0.3). 값이 0이면 색을 입히지 않는다 — 0건 위협을 빨강으로 그리지 않는다. */
export const METRIC_TONE = {
  danger: 'text-danger',
  warn: 'text-amber-400',
  ok: 'text-emerald-400',
} as const;

export type MetricTone = keyof typeof METRIC_TONE;

/**
 * 칸 사이를 `gap-px` 테두리로 그리는 띠다 — 열 수는 타일 개수에 맞춰 호출부가 `className`으로 준다.
 * 빈 칸이 남으면 색 덩어리로 보이니 마지막 타일에 `col-span-*`을 먹여 줄을 채운다(대시보드 5종).
 */
export function MetricStrip({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'bg-border ring-foreground/10 grid grid-cols-2 gap-px overflow-hidden rounded-xl ring-1',
        className,
      )}
    >
      {children}
    </div>
  );
}

/**
 * 타일 1칸. `value`가 null이면 `—`다(§3.3) — 0과 구분한다: 0은 "없다"는 단언이고 null은 "모른다"다.
 *
 * `onClick`을 주면 버튼이 된다. 누를 수 있는 타일과 아닌 타일이 한 띠에 섞여도 모양이 같아야
 * 숫자를 나란히 견줄 수 있으므로, 눌리는 쪽은 호버·선택 표시만 더한다(여백·글자 크기는 그대로).
 */
export function MetricTile({
  label,
  value,
  note,
  tone,
  onClick,
  selected = false,
  className,
}: {
  label: string;
  value: number | null;
  note: string;
  tone?: MetricTone;
  /** 주면 버튼이 된다. 누르는 쪽(필터 토글 등)의 의미는 호출부가 정한다. */
  onClick?: () => void;
  selected?: boolean;
  className?: string;
}) {
  const body = (
    <>
      <span className="text-muted-foreground text-xs">{label}</span>
      <span
        className={cn(
          'font-mono text-2xl font-medium tabular-nums',
          tone && value !== null && value > 0 && METRIC_TONE[tone],
        )}
      >
        {value ?? NO_VALUE}
      </span>
      <span className="text-muted-foreground text-xs">{note}</span>
    </>
  );

  const base = 'bg-card flex flex-col gap-1.5 px-5 py-4';

  if (onClick === undefined) {
    return <div className={cn(base, className)}>{body}</div>;
  }

  return (
    <button
      type="button"
      onClick={onClick}
      // 토글이므로 `aria-pressed`다 — 스크린 리더가 "눌림"으로 읽어야 지금 걸린 필터가 전달된다.
      aria-pressed={selected}
      className={cn(
        base,
        'hover:bg-accent/60 focus-visible:ring-ring/50 relative cursor-pointer text-left transition-colors focus-visible:z-10 focus-visible:ring-2 focus-visible:outline-none',
        selected && 'bg-accent',
        className,
      )}
    >
      {/* 선택 표시는 윗변 굵은 선이다 — 배경만 바꾸면 카드 색이 옅어 어느 칸이 걸렸는지 안 보인다. */}
      {selected ? <span className="bg-primary absolute inset-x-0 top-0 h-0.5" /> : null}
      {body}
    </button>
  );
}
