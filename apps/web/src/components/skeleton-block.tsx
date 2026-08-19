// CMN-002 로딩 — 레이아웃 형태의 스켈레톤입니다(화면설계서 v1.4 §4.9: 전체 화면 스피너를 쓰지 않는다).

import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

/**
 * 목록·카드처럼 같은 높이가 반복되는 자리의 스켈레톤.
 * 토폴로지·상세처럼 형태가 다른 화면은 이걸 쓰지 말고 ui/skeleton으로 그 화면 레이아웃을 직접 그린다.
 */
export function SkeletonBlock({
  rows = 3,
  rowClassName,
  className,
}: {
  rows?: number;
  rowClassName?: string;
  className?: string;
}) {
  return (
    <div aria-busy aria-label="불러오는 중" className={cn('flex flex-col gap-2', className)}>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className={cn('h-10 w-full', rowClassName)} />
      ))}
    </div>
  );
}
