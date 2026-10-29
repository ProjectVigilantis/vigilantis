'use client';

// 고객사 계정 상단바의 [문의하기](2026-10-29). 고객사 관리자 · 직원 누구나 장애 · 버그를 운영팀에 알린다.
// 보낸 문의는 관리자 웹의 고객 문의함(/inquiries)에 쌓이고 관리자 알림 종에 뜬다. 소속 고객사와 작성자는 서버가 세션에서
// 채우므로 화면은 묻지 않는다. 지금 보고 있는 화면의 경로를 함께 보내 운영자가 어디서 났는지 알게 한다.
// 계약은 초안이다(types/api.ts 끝). 서버에 아직 경로가 없으면(404 · 405) 그렇다고 적는다.

import { LifeBuoy } from 'lucide-react';
import { useState, type FormEvent } from 'react';

import { INPUT_CLASS as INPUT, LABEL_CLASS as LABEL } from '@/components/form-classes';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ApiError, submitInquiry } from '@/lib/api/client';
import { INQUIRY_CATEGORY_HINTS, INQUIRY_CATEGORY_LABELS, notYetServed } from '@/lib/support';
import { cn } from '@/lib/utils';
import type { InquiryCategory } from '@/types/api';

const CATEGORIES: InquiryCategory[] = ['OUTAGE', 'BUG', 'OTHER'];
const TITLE_MAX = 100;

export function InquiryButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="focus-visible:ring-ring inline-flex h-9 items-center gap-1.5 rounded-md px-2.5 text-[13px] font-semibold text-white/85 hover:bg-white/8 hover:text-white focus-visible:ring-2 focus-visible:outline-none"
      >
        <LifeBuoy aria-hidden className="size-4" />
        <span className="hidden sm:inline">문의하기</span>
        <span className="sr-only sm:hidden">문의하기</span>
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-lg">{open ? <InquiryForm onClose={() => setOpen(false)} /> : null}</DialogContent>
      </Dialog>
    </>
  );
}

function InquiryForm({ onClose }: { onClose: () => void }) {
  const [category, setCategory] = useState<InquiryCategory>('OUTAGE');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  // 문의를 연 화면. 대화상자가 열린 순간의 주소를 고정한다.
  const [pagePath] = useState(() => `${window.location.pathname}${window.location.search}`);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const occurred = String(form.get('occurred_at') ?? '');
    setPending(true);
    setError(null);
    try {
      await submitInquiry({
        category,
        title: String(form.get('title') ?? '').trim(),
        body: String(form.get('body') ?? '').trim(),
        // datetime-local 값은 이 브라우저의 현지 시각이다. ISO(UTC)로 바꿔 보낸다.
        occurred_at: occurred === '' ? null : new Date(occurred).toISOString(),
        page_path: pagePath,
      });
      setSent(true);
    } catch (cause) {
      setError(
        cause instanceof ApiError && notYetServed(cause.httpStatus)
          ? '문의 기능이 아직 서버에 열리지 않았습니다.'
          : cause instanceof ApiError
            ? cause.message
            : '문의를 보내지 못했습니다. 잠시 뒤 다시 시도해 주세요.',
      );
    } finally {
      setPending(false);
    }
  };

  if (sent) {
    return (
      <>
        <DialogHeader>
          <DialogTitle>문의를 보냈습니다</DialogTitle>
          <DialogDescription>운영팀이 확인한 뒤 이 계정의 이메일로 연락드립니다.</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" onClick={onClose}>
            닫기
          </Button>
        </DialogFooter>
      </>
    );
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle>운영팀에 문의</DialogTitle>
        <DialogDescription>장애나 버그를 알려 주세요. 보낸 문의는 Vigilantis 운영팀이 확인합니다.</DialogDescription>
      </DialogHeader>

      <fieldset className="flex flex-col gap-2">
        <legend className={cn(LABEL, 'mb-2')}>문의 종류</legend>
        {CATEGORIES.map((c) => (
          <label
            key={c}
            className={cn(
              'flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors',
              category === c ? 'border-primary bg-accent/40' : 'hover:bg-muted/50',
            )}
          >
            <input
              type="radio"
              name="category"
              value={c}
              checked={category === c}
              onChange={() => setCategory(c)}
              className="accent-primary mt-0.5 size-4"
            />
            <span className="flex flex-col gap-0.5">
              <span className="text-sm font-semibold">{INQUIRY_CATEGORY_LABELS[c].label}</span>
              <span className="text-muted-foreground text-xs">{INQUIRY_CATEGORY_HINTS[c]}</span>
            </span>
          </label>
        ))}
      </fieldset>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="inquiry-title" className={LABEL}>
          제목
        </label>
        <input id="inquiry-title" name="title" required maxLength={TITLE_MAX} disabled={pending} className={INPUT} />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="inquiry-body" className={LABEL}>
          내용
        </label>
        <textarea
          id="inquiry-body"
          name="body"
          required
          rows={5}
          disabled={pending}
          placeholder="무엇을 하다가 어떤 일이 생겼는지, 화면에 나온 오류 문구가 있으면 함께 적어 주세요."
          className={cn(INPUT, 'h-auto py-2')}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="inquiry-occurred" className={LABEL}>
          문제가 생긴 시각(선택)
        </label>
        <input id="inquiry-occurred" name="occurred_at" type="datetime-local" disabled={pending} className={INPUT} />
      </div>
      <p className="text-muted-foreground text-xs wrap-anywhere">
        지금 보던 화면도 함께 보냅니다: <span className="font-mono">{pagePath}</span>
      </p>

      {error ? (
        <p role="alert" className="bg-destructive/10 text-destructive rounded-md px-3 py-2 text-sm">
          {error}
        </p>
      ) : null}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose} disabled={pending}>
          취소
        </Button>
        <Button type="submit" disabled={pending}>
          {pending ? '보내는 중…' : '문의 보내기'}
        </Button>
      </DialogFooter>
    </form>
  );
}
