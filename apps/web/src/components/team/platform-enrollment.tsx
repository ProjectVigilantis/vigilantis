'use client';

// 플랫폼 운영자 초대 링크 등록(#492) — 링크를 받은 사람이 자기 이메일을 넣어 플랫폼 운영자 계정을 요청한다.
// 권한(플랫폼 운영자)은 링크가 정한다 — 고를 칸을 두지 않는다. 고객사 사용자는 이 화면이 아니라 사용자 관리(/users)로 초대한다.
//
// 토큰은 lib/enrollment.ts의 저장소가 URL 조각에서 한 번 읽어 메모리에만 들고 있는다(주소에서는 즉시 지운다).
// 화면 텍스트 · 입력 칸 · 오류 문구에 토큰을 싣지 않는다. 서버 렌더 결과에는 토큰이 없다(조각은 서버에 오지 않는다).
//
// 성공(204)은 계정 생성 · 초대 메일 발송 요청까지다 — 자동 로그인 · 세션 확인을 하지 않고 로그인 화면 링크만 둔다.
// 예약 전 거절(422 입력 · 403 출처) 뒤의 재요청은 사용자가 직접 누를 때만 보낸다. 409 · 429는 예약 뒤 거절이다.
// 404 · 인증 서비스 · 메일 발송 · 500 · 네트워크 · 계약 밖 코드로는 예약 여부를 단정하지 않지만 모두 끝낸다.
// 토큰을 버리고 폼을 다시 열지 않으며, 그 문구(로그인 · 메일함 확인 · 담당자 문의)를 그대로 보인다.

import Link from 'next/link';
import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react';

import { HangulWarnedInput } from '@/components/auth/hangul-warned-input';
import { INPUT_CLASS as INPUT, LABEL_CLASS as LABEL } from '@/components/form-classes';
import { Button } from '@/components/ui/button';
import { ApiError, requestPlatformEnrollment } from '@/lib/api/client';
import {
  ENROLLMENT_DONE,
  ENROLLMENT_LINK_INVALID,
  createEnrollmentLinkStore,
  enrollmentErrorView,
  settleEnrollment,
  takeEnrollmentLink,
  type EnrollmentErrorView,
} from '@/lib/enrollment';
import { LOGIN_PATH } from '@/lib/return-to';

/** 모듈 하나에 하나 — 재마운트가 와도 조각을 다시 읽지 않는다(lib/enrollment.ts). 브라우저에서만 읽는다. */
const linkStore = createEnrollmentLinkStore(() => takeEnrollmentLink(window.location, window.history));

type Phase =
  | { kind: 'form'; error: EnrollmentErrorView | null }
  | { kind: 'submitting' }
  | { kind: 'done' }
  /** 서버가 링크를 거절했거나 결과를 알 수 없다 — 더 요청하지 않는다(토큰을 버렸다). */
  | { kind: 'final'; error: EnrollmentErrorView };

export function PlatformEnrollment() {
  const link = useSyncExternalStore(linkStore.subscribe, linkStore.getSnapshot, linkStore.getServerSnapshot);
  const [phase, setPhase] = useState<Phase>({ kind: 'form', error: null });
  /** 비밀이 아닌 값이라 상태에 둔다 — 다시 요청할 때 다시 치지 않게. */
  const [email, setEmail] = useState('');
  const inFlight = useRef(false);

  // 이 화면이 열린 탭에서 다른 초대 링크를 열면 브라우저는 조각만 바꾸고 문서를 다시 읽지 않는다 — 토큰이 주소창에
  // 남고 화면은 이전 링크 상태 그대로다. 새 문서로 다시 읽어 그 링크로 처음부터 시작한다(읽는 즉시 다시 지운다).
  useEffect(() => {
    const onHashChange = () => {
      if (window.location.hash !== '') window.location.reload();
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current || link.kind !== 'ready') return;
    // 이메일은 다듬지 않고 입력 그대로 보낸다(서버가 형식을 판정한다).
    const nextEmail = String(new FormData(event.currentTarget).get('email') ?? '');
    if (nextEmail.trim() === '') return;
    // 응답은 이 초대에만 반영한다 — 그 사이 다른 초대로 바뀌었으면 그 링크 · 화면을 건드리지 않는다(settleEnrollment).
    const { token } = link;
    inFlight.current = true;
    setEmail(nextEmail);
    setPhase({ kind: 'submitting' });
    try {
      await requestPlatformEnrollment({ token, email: nextEmail });
    } catch (cause) {
      const view = enrollmentErrorView(cause instanceof ApiError ? cause.code : null);
      if (settleEnrollment(linkStore, token, view)) {
        setPhase(view.final ? { kind: 'final', error: view } : { kind: 'form', error: view });
      }
      return;
    } finally {
      inFlight.current = false;
    }
    if (settleEnrollment(linkStore, token, null)) setPhase({ kind: 'done' });
  }

  return (
    <section className="flex w-full max-w-md flex-col gap-6 rounded-lg border bg-card p-6">
      <header className="flex flex-col gap-1.5">
        <p className="font-heading text-xs font-semibold tracking-widest text-muted-foreground">VIGILANTIS</p>
        <h1 className="text-xl font-semibold">플랫폼 운영자 계정 만들기</h1>
        <p className="text-sm text-muted-foreground">
          받은 초대 링크로 사용할 이메일을 입력하면, 그 이메일로 계정을 만들고 초대 메일을 보냅니다.
        </p>
      </header>

      <Body phase={phase} link={link.kind} email={email} onSubmit={onSubmit} />
    </section>
  );
}

function Body({
  phase,
  link,
  email,
  onSubmit,
}: {
  phase: Phase;
  link: 'pending' | 'invalid' | 'ready' | 'spent';
  email: string;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  if (phase.kind === 'done') {
    return (
      <>
        <div role="status" className="flex flex-col gap-2 rounded-md bg-muted px-3 py-2 text-sm">
          <p className="font-medium">요청을 처리했습니다.</p>
          {ENROLLMENT_DONE.map((line) => (
            <p key={line}>{line}</p>
          ))}
        </div>
        <LoginLink />
      </>
    );
  }

  // 링크 상태(spent)보다 먼저 본다 — 거절 · 결과 불명 문구가 "링크가 올바르지 않다"로 바뀌지 않게.
  if (phase.kind === 'final') {
    return (
      <>
        <Alert>{phase.error.message}</Alert>
        <LoginLink variant="outline" />
      </>
    );
  }

  if (link === 'pending') {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        초대 링크를 확인하는 중…
      </p>
    );
  }

  if (link !== 'ready') {
    return <Alert>{ENROLLMENT_LINK_INVALID}</Alert>;
  }

  const pending = phase.kind === 'submitting';
  return (
    <form className="flex flex-col gap-4" onSubmit={onSubmit}>
      {phase.kind === 'form' && phase.error ? <Alert>{phase.error.message}</Alert> : null}
      <dl className="flex flex-col gap-1 rounded-md bg-muted px-3 py-2 text-sm">
        <dt className="text-muted-foreground">만들 계정</dt>
        <dd className="font-medium">플랫폼 운영자</dd>
        <dd className="text-xs text-muted-foreground">권한은 초대 링크가 정해 바꿀 수 없습니다.</dd>
      </dl>
      <div className="flex flex-col gap-1.5">
        <label htmlFor="enroll-email" className={LABEL}>
          이메일
        </label>
        <HangulWarnedInput
          id="enroll-email"
          name="email"
          type="email"
          autoComplete="email"
          required
          defaultValue={email}
          disabled={pending}
          className={INPUT}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        이 링크는 한 번만 쓸 수 있습니다. 다른 사람과 공유하지 마세요.
      </p>
      <Button type="submit" size="lg" className="w-full" disabled={pending}>
        {pending ? '요청 중…' : '계정 만들기'}
      </Button>
    </form>
  );
}

function Alert({ children }: { children: React.ReactNode }) {
  return (
    <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
      {children}
    </p>
  );
}

function LoginLink({ variant = 'default' }: { variant?: 'default' | 'outline' }) {
  return (
    <Button asChild size="lg" variant={variant} className="w-full">
      <Link href={LOGIN_PATH}>로그인 화면으로</Link>
    </Button>
  );
}
