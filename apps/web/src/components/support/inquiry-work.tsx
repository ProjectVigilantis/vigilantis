'use client';

// 고객 문의 한 건을 처리하는 자리(고객 문의함에서 펼친 문의 아래). 운영자 메모와 상태 변경, 그리고 처리 기록.
// 처리 기록은 고치거나 지우지 않고 쌓기만 한다(계약 초안 `GET /inquiries/{id}/events`, 사용자 관리 이력과 같은 방식).
// 상태를 바꾸면서 메모를 적으면 같은 기록 한 줄에 남고, [메모만 남기기]는 상태를 그대로 둔다.
// 서버에 아직 기록 경로가 없으면(404 · 405) 그렇다고 적는다.

import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { INPUT_CLASS as INPUT, LABEL_CLASS as LABEL } from '@/components/form-classes';
import { Button } from '@/components/ui/button';
import { ApiError, addInquiryNote, changeInquiryStatus, getInquiryEvents } from '@/lib/api/client';
import { INQUIRY_NEXT, inquiryEventTitle, notYetServed } from '@/lib/support';
import { cn, formatKst } from '@/lib/utils';
import type { InquiryEventItem, InquiryItem, InquiryStatus } from '@/types/api';

const ACTION_LABEL: Record<InquiryStatus, string> = {
  OPEN: '접수로',
  IN_PROGRESS: '처리 시작',
  RESOLVED: '해결로 표시',
};

type History = { kind: 'loading' } | { kind: 'ready'; items: InquiryEventItem[] } | { kind: 'unserved' } | { kind: 'error' };

export function InquiryWork({ item }: { item: InquiryItem }) {
  const router = useRouter();
  const [note, setNote] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<History>({ kind: 'loading' });
  /** 처리하고 나면 기록을 다시 부른다. */
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let alive = true;
    getInquiryEvents(item.inquiry_id).then(
      (res) => alive && setHistory({ kind: 'ready', items: res.items }),
      (cause: unknown) =>
        alive &&
        setHistory(cause instanceof ApiError && notYetServed(cause.httpStatus) ? { kind: 'unserved' } : { kind: 'error' }),
    );
    return () => {
      alive = false;
    };
  }, [item.inquiry_id, version]);

  const run = async (work: () => Promise<unknown>) => {
    setPending(true);
    setError(null);
    try {
      await work();
      setNote('');
      setVersion((v) => v + 1);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '처리하지 못했습니다.');
    } finally {
      setPending(false);
    }
  };

  const memo = note.trim();

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <label htmlFor={`inquiry-note-${item.inquiry_id}`} className={LABEL}>
          처리 메모(선택)
        </label>
        <textarea
          id={`inquiry-note-${item.inquiry_id}`}
          rows={2}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          disabled={pending}
          placeholder="확인한 내용이나 고객에게 안내한 내용을 남깁니다. 상태를 바꾸면 함께 기록됩니다."
          className={cn(INPUT, 'h-auto py-2')}
        />
      </div>
      <div className="flex flex-wrap gap-2">
        {INQUIRY_NEXT[item.status].map((next) => (
          <Button
            key={next}
            type="button"
            size="sm"
            variant={next === 'RESOLVED' ? 'default' : 'outline'}
            disabled={pending}
            onClick={() => void run(() => changeInquiryStatus(item.inquiry_id, next, memo === '' ? null : memo))}
          >
            {ACTION_LABEL[next]}
          </Button>
        ))}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={pending || memo === ''}
          onClick={() => void run(() => addInquiryNote(item.inquiry_id, memo))}
        >
          메모만 남기기
        </Button>
      </div>
      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      <div className="flex flex-col gap-2 border-t pt-4">
        <span className="text-sm font-semibold">처리 기록</span>
        {history.kind === 'loading' ? <p className="text-muted-foreground text-sm">불러오는 중…</p> : null}
        {history.kind === 'unserved' ? (
          <p className="text-muted-foreground text-sm">처리 기록 기능이 아직 서버에 열리지 않았습니다.</p>
        ) : null}
        {history.kind === 'error' ? <p className="text-muted-foreground text-sm">처리 기록을 불러오지 못했습니다.</p> : null}
        {history.kind === 'ready' ? (
          <ol className="flex flex-col gap-3">
            {history.items.map((event) => (
              <li key={event.event_id} className="flex gap-3 text-sm">
                <span aria-hidden className="bg-primary mt-1.5 size-1.5 shrink-0 rounded-full" />
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span>
                    <span className="font-semibold">{inquiryEventTitle(event)}</span>
                    <span className="text-muted-foreground">
                      {' '}
                      · {event.actor_email} · {formatKst(event.occurred_at)}
                    </span>
                  </span>
                  {event.note ? <p className="text-foreground-secondary whitespace-pre-wrap">{event.note}</p> : null}
                </div>
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </div>
  );
}
