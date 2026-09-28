// GNB 우측 상태 칩 — `수집`과 `실시간`이 **같은 부품으로** 그려지도록 모양을 여기 하나에 둔다.
// 두 인디케이터가 각자 클래스를 들고 있으면 한쪽만 손대는 순간 같은 성격의 값이 다른 무게로 읽힌다
// (실제로 한쪽만 테두리로 감싸여 있었다).
//
// 색은 **점 하나만** 쓴다. 값 글자까지 물들이면 GNB에 색 덩어리가 둘씩 생겨 본문보다 시끄러워지므로,
// 주의가 필요한 상태(`warn`)에서만 글자에 색을 준다 — 평상시에는 점만 켜 두고 조용히 있는다.

import { cn } from '@/lib/utils';

export type ChipTone = 'ok' | 'warn' | 'idle';

const TONE: Record<ChipTone, { dot: string; value: string }> = {
  ok: { dot: 'bg-emerald-500 dark:bg-emerald-400', value: 'text-foreground' },
  warn: {
    dot: 'bg-amber-500 dark:bg-amber-400',
    value: 'text-amber-600 dark:text-amber-400',
  },
  // 속을 비운 점 — "꺼짐"과 "정상"을 색이 아니라 **모양**으로도 가른다(색각 보조).
  idle: { dot: 'bg-transparent ring-1 ring-muted-foreground', value: 'text-muted-foreground' },
};

export function StatusChip({
  tone,
  label,
  value,
  title,
  pulse = false,
  children,
}: {
  tone: ChipTone;
  /** 속성 — 무엇에 대한 상태인가. 상태값이 바뀌어도 그대로다. */
  label: string;
  /** 상태값 — 사전(§3.2)의 표시명을 그대로 쓴다. 여기서 문구를 새로 만들지 않는다. */
  value: string;
  /** 화면에 싣지 않는 보조 정보(계정 ID·소켓 주소 등). */
  title?: string;
  /** 진행 중 표시. 재연결처럼 "지금 시도하고 있다"를 점의 깜박임으로만 알린다. */
  pulse?: boolean;
  /** 칩 안에 붙는 동작 버튼(예: 재연결). */
  children?: React.ReactNode;
}) {
  const view = TONE[tone];
  return (
    <span
      className="bg-muted/50 flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 whitespace-nowrap"
      title={title}
    >
      <span aria-hidden className={cn('size-1.5 rounded-full', view.dot, pulse && 'animate-pulse')} />
      <span className="text-muted-foreground text-[11px]">{label}</span>
      <span className={cn('text-xs font-medium', view.value)}>{value}</span>
      {children}
    </span>
  );
}
