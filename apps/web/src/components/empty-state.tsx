// CMN-002 빈 상태 — 문구는 호출하는 화면이 화면설계서 v1.4 §4.9 표에서 그대로 넘깁니다(여기서 만들지 않는다).

import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

/**
 * 빈 상태는 오류가 아니다(4.9: INC-001의 0건은 정상 관제 신호).
 * 오류로 읽히는 색·아이콘을 쓰지 않는다.
 */
export function EmptyState({
  message,
  description,
  action,
  className,
}: {
  /** 4.9 표의 문구를 그대로. */
  message: string;
  description?: string;
  /** 필터 해제 같은 후속 조작(4.9 AST-001). */
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed px-6 py-12 text-center',
        className,
      )}
    >
      <p className="text-sm text-muted-foreground">{message}</p>
      {description ? <p className="text-xs text-muted-foreground">{description}</p> : null}
      {action}
    </div>
  );
}
