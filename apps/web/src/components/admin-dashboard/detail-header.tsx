// 관리자 대시보드에서 넘어온 상세 화면의 머리: 대시보드로 돌아가는 링크 · 제목 · 설명 · 마지막 갱신.
// 훅이 없어 `'use client'`를 붙이지 않는다.

import { ChevronLeft } from 'lucide-react';
import Link from 'next/link';

import { AutoRefresh } from '@/components/auto-refresh';
import { PLATFORM_HOME } from '@/lib/home-path';

export function DetailHeader({ title, description, renderedAt }: { title: string; description: string; renderedAt: string }) {
  return (
    <div className="flex flex-col gap-2">
      <Link href={PLATFORM_HOME} className="text-muted-foreground hover:text-foreground inline-flex w-fit items-center gap-1 text-sm">
        <ChevronLeft aria-hidden className="size-4" />
        대시보드
      </Link>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
          <p className="text-muted-foreground text-sm">{description}</p>
        </div>
        <AutoRefresh renderedAt={renderedAt} />
      </div>
    </div>
  );
}
