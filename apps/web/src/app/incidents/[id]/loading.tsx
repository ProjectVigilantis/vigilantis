// INC-002 로딩 — 상세는 형태가 달라 SkeletonBlock을 쓰지 않고 레이아웃을 직접 그린다(§4.9).

import { Skeleton } from '@/components/ui/skeleton';

export default function Loading() {
  return (
    <div aria-busy aria-label="불러오는 중" className="flex max-w-3xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-5 w-32" />
        <Skeleton className="h-6 w-2/3" />
        <Skeleton className="h-4 w-48" />
      </div>
      <Skeleton className="h-20 w-full" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-28 w-full" />
    </div>
  );
}
