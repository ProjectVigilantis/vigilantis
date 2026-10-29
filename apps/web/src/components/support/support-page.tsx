// 도입 신청함 · 고객 문의함의 공통 틀: 머리(제목 · 설명 · 마지막 갱신)와 서버가 아직 경로를 열지 않았을 때의 안내.
// 훅이 없어 `'use client'`를 붙이지 않는다.

import type { ReactNode } from 'react';

import { AutoRefresh } from '@/components/auto-refresh';

export function SupportPage({
  title,
  description,
  renderedAt,
  children,
}: {
  title: string;
  description: string;
  renderedAt: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
          <p className="text-muted-foreground text-sm">{description}</p>
        </div>
        <AutoRefresh renderedAt={renderedAt} />
      </div>
      {children}
    </div>
  );
}

/** 목록 경로가 404 · 405다. 오류가 아니라 서버 기능이 아직 없는 것이라 그렇게 적는다(lib/support.ts `notYetServed`). */
export function NotYetServed({ what }: { what: string }) {
  return (
    <div className="bg-card ring-foreground/10 flex flex-col gap-1 rounded-xl p-6 ring-1">
      <p className="font-semibold">{what} 기능이 아직 서버에 열리지 않았습니다.</p>
      <p className="text-muted-foreground text-sm">화면은 계약 초안에 맞춰 준비돼 있고, 서버 API가 붙으면 이 자리에 목록이 나옵니다.</p>
    </div>
  );
}
