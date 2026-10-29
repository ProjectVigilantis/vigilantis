'use client';

// 고객 문의함(플랫폼 계정). 고객사 계정(관리자 · 직원)이 상단바 [문의하기]로 보낸 장애 · 버그 문의를 모아 본다.
// 처리 상태와 종류로 거르고, 한 건을 펼쳐 내용을 읽는다. 메모 · 상태 변경 · 처리 기록은 InquiryWork가 맡는다.
// 답장은 작성자 이메일로 따로 한다(답장 기능은 계약 초안에 없다). 알림을 눌러 오면(`?id=`) 그 건을 펼쳐 둔다.

import Link from 'next/link';
import { useState } from 'react';

import { EmptyState } from '@/components/empty-state';
import { EnumBadge } from '@/components/status-badge';
import { FilterChips } from '@/components/support/filter-chips';
import { InquiryWork } from '@/components/support/inquiry-work';
import { TENANTS_PATH } from '@/lib/home-path';
import { INQUIRY_CATEGORY_LABELS, INQUIRY_STATUS_LABELS } from '@/lib/support';
import { ROLE_LABELS } from '@/lib/user-admin';
import { formatKst } from '@/lib/utils';
import type { InquiryCategory, InquiryItem, InquiryStatus } from '@/types/api';

const STATUS_FILTERS: (InquiryStatus | 'UNRESOLVED' | 'ALL')[] = ['UNRESOLVED', 'OPEN', 'IN_PROGRESS', 'RESOLVED', 'ALL'];
const CATEGORY_FILTERS: (InquiryCategory | 'ALL')[] = ['ALL', 'OUTAGE', 'BUG', 'OTHER'];

export function InquiriesView({ items, focusId }: { items: InquiryItem[]; focusId: string | null }) {
  // 기본은 아직 끝나지 않은 것(접수 + 처리 중)이다. 알림으로 온 건이 이미 해결이면 전체에서 보여 준다.
  const focused = items.find((i) => i.inquiry_id === focusId);
  const [status, setStatus] = useState<InquiryStatus | 'UNRESOLVED' | 'ALL'>(
    focused?.status === 'RESOLVED' ? 'ALL' : 'UNRESOLVED',
  );
  const [category, setCategory] = useState<InquiryCategory | 'ALL'>('ALL');
  const [openId, setOpenId] = useState<string | null>(focusId);

  const byStatus = (i: InquiryItem, s: typeof status) =>
    s === 'ALL' || (s === 'UNRESOLVED' ? i.status !== 'RESOLVED' : i.status === s);
  const byCategory = (i: InquiryItem, c: typeof category) => c === 'ALL' || i.category === c;
  // 펼쳐 둔 건은 필터와 상관없이 남긴다. 해결로 옮기자마자 "미해결"에서 사라지면 방금 쌓인 처리 기록을 볼 수 없다.
  const shown = items.filter((i) => i.inquiry_id === openId || (byStatus(i, status) && byCategory(i, category)));

  if (items.length === 0) {
    return <EmptyState message="받은 문의가 없습니다." description="고객사 계정이 상단바의 [문의하기]로 보내면 여기에 쌓입니다." />;
  }

  return (
    <section className="bg-card ring-foreground/10 flex flex-col gap-4 rounded-xl p-6 ring-1">
      <div className="flex flex-col gap-2">
        <FilterChips
          label="처리 상태"
          options={STATUS_FILTERS.map((s) => ({
            value: s,
            label: s === 'ALL' ? '전체' : s === 'UNRESOLVED' ? '미해결' : INQUIRY_STATUS_LABELS[s].label,
            count: items.filter((i) => byStatus(i, s) && byCategory(i, category)).length,
          }))}
          value={status}
          onChange={setStatus}
        />
        <FilterChips
          label="문의 종류"
          options={CATEGORY_FILTERS.map((c) => ({
            value: c,
            label: c === 'ALL' ? '모든 종류' : INQUIRY_CATEGORY_LABELS[c].label,
            count: items.filter((i) => byStatus(i, status) && byCategory(i, c)).length,
          }))}
          value={category}
          onChange={setCategory}
        />
      </div>
      {shown.length === 0 ? (
        <p className="text-muted-foreground py-6 text-center text-sm">조건에 맞는 문의가 없습니다.</p>
      ) : (
        <ul className="flex flex-col divide-y">
          {shown.map((item) => {
            const open = openId === item.inquiry_id;
            return (
              <li key={item.inquiry_id}>
                <button
                  type="button"
                  aria-expanded={open}
                  onClick={() => setOpenId(open ? null : item.inquiry_id)}
                  className="hover:bg-muted/50 grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 rounded-md px-2 py-3 text-left sm:grid-cols-[auto_minmax(0,3fr)_minmax(0,1fr)_auto_auto]"
                >
                  <EnumBadge entry={INQUIRY_CATEGORY_LABELS[item.category]} className="w-12 justify-center" />
                  <span className="truncate font-semibold">{item.title}</span>
                  <span className="text-muted-foreground hidden truncate text-sm sm:block">{item.tenant_name}</span>
                  <span className="text-muted-foreground hidden text-xs sm:block">{formatKst(item.created_at)}</span>
                  <EnumBadge entry={INQUIRY_STATUS_LABELS[item.status]} />
                </button>
                {open ? (
                  <div className="flex flex-col gap-4 px-2 pt-1 pb-4">
                    <p className="bg-muted/60 rounded-md px-4 py-3 text-sm leading-relaxed whitespace-pre-wrap">{item.body}</p>
                    <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
                      <dt className="text-muted-foreground">고객사</dt>
                      <dd>
                        <Link
                          href={`${TENANTS_PATH}/${encodeURIComponent(item.tenant_id)}`}
                          className="text-primary font-semibold hover:underline"
                        >
                          {item.tenant_name}
                        </Link>
                      </dd>
                      <dt className="text-muted-foreground">보낸 사람</dt>
                      <dd className="wrap-anywhere">
                        {item.author_email} · 권한 {ROLE_LABELS[item.author_role]}
                      </dd>
                      <dt className="text-muted-foreground">문제가 생긴 시각</dt>
                      <dd>{item.occurred_at === null ? '적지 않음' : formatKst(item.occurred_at)}</dd>
                      <dt className="text-muted-foreground">보낸 화면</dt>
                      <dd className="font-mono text-xs wrap-anywhere">{item.page_path ?? '알 수 없음'}</dd>
                      <dt className="text-muted-foreground">받은 시각</dt>
                      <dd>{formatKst(item.created_at)}</dd>
                    </dl>
                    <InquiryWork item={item} />
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
