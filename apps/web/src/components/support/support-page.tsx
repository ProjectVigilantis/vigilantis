// 도입 신청함 · 고객사 문의함의 공통 틀: 머리(제목 · 설명 · 마지막 갱신), 쪽 이동, 서버가 아직 경로를 열지 않았을 때의 안내.
// 훅이 없어 `'use client'`를 붙이지 않는다.

import Link from 'next/link';
import type { ReactNode } from 'react';

import { AutoRefresh } from '@/components/auto-refresh';
import { supportPageHref } from '@/lib/support';

export function SupportPage({
  title,
  description,
  renderedAt,
  children,
}: {
  title: string;
  description?: string;
  renderedAt: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
          {description ? <p className="text-muted-foreground text-sm">{description}</p> : null}
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
      <p className="text-muted-foreground text-sm">이 서버에는 아직 이 API가 없습니다. 서버가 갱신되면 이 자리에 목록이 나옵니다.</p>
    </div>
  );
}

const PAGE_LINK = 'text-primary rounded-md border px-3 py-1.5 text-sm font-semibold hover:bg-muted';
const PAGE_LINK_OFF = 'text-muted-foreground rounded-md border px-3 py-1.5 text-sm opacity-50';

/**
 * 쪽 이동(#565 페이지 응답). 한 쪽뿐이면 건수만 적는다. 마지막 쪽을 넘는 주소로 들어오면 서버가 빈 목록을 주므로
 * 마지막 쪽으로 돌아가는 링크를 둔다.
 */
export function SupportPager({
  path,
  page,
  totalPages,
  total,
}: {
  path: string;
  page: number;
  totalPages: number;
  total: number;
}) {
  if (total > 0 && page > totalPages) {
    return (
      <p role="status" className="text-muted-foreground text-sm">
        {page}쪽에는 건이 없습니다.{' '}
        <Link href={supportPageHref(path, totalPages)} className="text-primary font-semibold hover:underline">
          마지막 쪽({totalPages}쪽)으로
        </Link>
      </p>
    );
  }
  return (
    <nav aria-label="쪽 이동" className="flex flex-wrap items-center justify-between gap-2">
      <p className="text-muted-foreground text-sm tabular-nums">
        모두 {total}건{totalPages > 1 ? ` · ${page} / ${totalPages}쪽` : ''}
      </p>
      {totalPages > 1 ? (
        <div className="flex gap-2">
          {page > 1 ? (
            <Link href={supportPageHref(path, page - 1)} className={PAGE_LINK}>
              이전
            </Link>
          ) : (
            <span aria-disabled className={PAGE_LINK_OFF}>이전</span>
          )}
          {page < totalPages ? (
            <Link href={supportPageHref(path, page + 1)} className={PAGE_LINK}>
              다음
            </Link>
          ) : (
            <span aria-disabled className={PAGE_LINK_OFF}>다음</span>
          )}
        </div>
      ) : null}
    </nav>
  );
}

/** 알림에서 넘어온 건(`?id=`)이 이 쪽에 없다. 신청 · 문의는 최근 것부터라 대개 1쪽에 있다. */
export function FocusMissing() {
  return (
    <p role="status" className="text-muted-foreground text-sm">
      알림의 건이 이 쪽에 없습니다. 다른 쪽에서 찾아 주세요.
    </p>
  );
}
