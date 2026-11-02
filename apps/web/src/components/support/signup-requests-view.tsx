'use client';

// 도입 신청함(플랫폼 계정). 로그인 창 회원가입에서 들어온 신청을 상태별로 걸러 보고, 한 건을 펼쳐 검토 · 승인 · 반려한다.
// 승인은 신청의 상태만 바꾼다. 고객사 추가와 관리자 초대는 고객사 화면에서 이어서 한다(ADR-0012: 고객사는 운영자가 만든다).
// 반려는 사유가 있어야 한다(계약 #552): [반려]를 누르면 사유 칸이 열리고, 저장한 사유는 반려된 신청에 함께 보인다.
// 알림을 눌러 오면(`?id=`) 그 건을 펼쳐 둔다.

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { EmptyState } from '@/components/empty-state';
import { INPUT_CLASS as INPUT, LABEL_CLASS as LABEL } from '@/components/form-classes';
import { EnumBadge } from '@/components/status-badge';
import { FilterChips } from '@/components/support/filter-chips';
import { Button } from '@/components/ui/button';
import { ApiError, changeSignupRequestStatus } from '@/lib/api/client';
import { TENANTS_PATH } from '@/lib/home-path';
import { SIGNUP_NEXT, SIGNUP_STATUS_LABELS } from '@/lib/support';
import { cn, formatKst } from '@/lib/utils';
import type { SignupRequestItem, SignupRequestStatus } from '@/types/api';

const ACTION_LABEL: Record<SignupRequestStatus, string> = {
  NEW: '새 신청으로',
  IN_REVIEW: '검토 시작',
  APPROVED: '승인',
  REJECTED: '반려',
};

const FILTERS: (SignupRequestStatus | 'ALL')[] = ['ALL', 'NEW', 'IN_REVIEW', 'APPROVED', 'REJECTED'];

export function SignupRequestsView({ items, focusId }: { items: SignupRequestItem[]; focusId: string | null }) {
  const router = useRouter();
  const [filter, setFilter] = useState<SignupRequestStatus | 'ALL'>('ALL');
  const [openId, setOpenId] = useState<string | null>(focusId);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<{ id: string; message: string } | null>(null);
  /** [반려]를 누른 신청. 사유 칸을 열고, 사유를 적어야 [반려 확정]이 눌린다(계약: 반려는 사유 필수). */
  const [rejecting, setRejecting] = useState<{ id: string; reason: string } | null>(null);

  const shown = filter === 'ALL' ? items : items.filter((i) => i.status === filter);
  const count = (f: SignupRequestStatus | 'ALL') => (f === 'ALL' ? items.length : items.filter((i) => i.status === f).length);

  const change = async (item: SignupRequestItem, status: SignupRequestStatus, reason: string | null = null) => {
    setPendingId(item.request_id);
    setError(null);
    try {
      await changeSignupRequestStatus(item.request_id, status, reason);
      setRejecting(null);
      router.refresh();
    } catch (cause) {
      setError({ id: item.request_id, message: cause instanceof ApiError ? cause.message : '상태를 바꾸지 못했습니다.' });
    } finally {
      setPendingId(null);
    }
  };

  if (items.length === 0) {
    return <EmptyState message="받은 도입 신청이 없습니다." description="랜딩 로그인 창의 회원가입에서 신청하면 여기에 쌓입니다." />;
  }

  return (
    <section className="bg-card ring-foreground/10 flex flex-col gap-4 rounded-xl p-6 ring-1">
      <FilterChips
        label="처리 상태"
        options={FILTERS.map((f) => ({ value: f, label: f === 'ALL' ? '전체' : SIGNUP_STATUS_LABELS[f].label, count: count(f) }))}
        value={filter}
        onChange={setFilter}
      />
      {shown.length === 0 ? (
        <p className="text-muted-foreground py-6 text-center text-sm">이 상태의 신청이 없습니다.</p>
      ) : (
        <ul className="flex flex-col divide-y">
          {shown.map((item) => {
            const open = openId === item.request_id;
            return (
              <li key={item.request_id} id={`request-${item.request_id}`}>
                <button
                  type="button"
                  aria-expanded={open}
                  onClick={() => setOpenId(open ? null : item.request_id)}
                  className="hover:bg-muted/50 grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 rounded-md px-2 py-3 text-left sm:grid-cols-[minmax(0,2fr)_minmax(0,2fr)_auto_auto]"
                >
                  <span className="truncate font-semibold">{item.company_name}</span>
                  <span className="text-muted-foreground hidden truncate text-sm sm:block">
                    {item.contact_name} · {item.email}
                  </span>
                  <span className="text-muted-foreground hidden text-xs sm:block">{formatKst(item.created_at)}</span>
                  <EnumBadge entry={SIGNUP_STATUS_LABELS[item.status]} />
                </button>
                {open ? (
                  <div className="flex flex-col gap-4 px-2 pt-1 pb-4">
                    <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
                      <dt className="text-muted-foreground">담당자</dt>
                      <dd>{item.contact_name}</dd>
                      <dt className="text-muted-foreground">업무 이메일</dt>
                      <dd className="wrap-anywhere">{item.email}</dd>
                      <dt className="text-muted-foreground">연락처</dt>
                      <dd>{item.phone ?? '적지 않음'}</dd>
                      <dt className="text-muted-foreground">관제할 AWS 환경</dt>
                      <dd className="whitespace-pre-wrap">{item.note ?? '적지 않음'}</dd>
                      <dt className="text-muted-foreground">받은 시각</dt>
                      <dd>{formatKst(item.created_at)}</dd>
                      {item.status === 'REJECTED' ? (
                        <>
                          <dt className="text-muted-foreground">반려 사유</dt>
                          <dd className="whitespace-pre-wrap">{item.rejected_reason ?? '기록 없음'}</dd>
                        </>
                      ) : null}
                      {/* 판정 기록(#565): 승인 · 반려한 계정과 시각. 판정 당시의 스냅샷이고, 그 전에 처리된 건은 없다(null). */}
                      {item.decision !== null ? (
                        <>
                          <dt className="text-muted-foreground">처리한 계정</dt>
                          <dd className="wrap-anywhere">{item.decision.actor_email}</dd>
                          <dt className="text-muted-foreground">처리 시각</dt>
                          <dd>{formatKst(item.decision.decided_at)}</dd>
                        </>
                      ) : null}
                    </dl>
                    {item.status === 'APPROVED' ? (
                      <p className="bg-muted rounded-md px-3 py-2 text-sm">
                        승인한 신청입니다. 고객사 화면에서 고객사를 추가하고, AWS 연결이 확인되면 {item.email}을 고객사 중앙계정으로
                        초대하세요.{' '}
                        <Link href={TENANTS_PATH} className="text-primary font-semibold hover:underline">
                          고객사 화면으로
                        </Link>
                      </p>
                    ) : null}
                    {rejecting?.id === item.request_id ? (
                      <div className="flex flex-col gap-2">
                        <label htmlFor={`reject-reason-${item.request_id}`} className={LABEL}>
                          반려 사유
                        </label>
                        <textarea
                          id={`reject-reason-${item.request_id}`}
                          rows={3}
                          autoFocus
                          value={rejecting.reason}
                          onChange={(event) => setRejecting({ id: item.request_id, reason: event.target.value })}
                          disabled={pendingId === item.request_id}
                          placeholder="예: 지원하지 않는 클라우드(AWS 외) 환경입니다."
                          className={cn(INPUT, 'h-auto py-2')}
                        />
                        <div className="flex flex-wrap gap-2">
                          <Button
                            type="button"
                            size="sm"
                            variant="destructive"
                            disabled={pendingId === item.request_id || rejecting.reason.trim() === ''}
                            onClick={() => void change(item, 'REJECTED', rejecting.reason.trim())}
                          >
                            반려 확정
                          </Button>
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            disabled={pendingId === item.request_id}
                            onClick={() => setRejecting(null)}
                          >
                            취소
                          </Button>
                        </div>
                      </div>
                    ) : SIGNUP_NEXT[item.status].length > 0 ? (
                      <div className="flex flex-wrap gap-2">
                        {SIGNUP_NEXT[item.status].map((next) => (
                          <Button
                            key={next}
                            type="button"
                            size="sm"
                            variant={next === 'REJECTED' ? 'outline' : 'default'}
                            disabled={pendingId === item.request_id}
                            onClick={() =>
                              next === 'REJECTED'
                                ? setRejecting({ id: item.request_id, reason: '' })
                                : void change(item, next)
                            }
                          >
                            {ACTION_LABEL[next]}
                          </Button>
                        ))}
                      </div>
                    ) : null}
                    {error?.id === item.request_id ? (
                      <p role="alert" className="text-destructive text-sm">
                        {error.message}
                      </p>
                    ) : null}
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
