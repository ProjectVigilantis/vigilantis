'use client';

// 로그인 단계 화면(#492 PR 1). 서버가 `next_step`으로 다음 단계를 정하고, 여기서는 그 값으로 패널만 고른다.
// 진행 · 세션은 서버의 HttpOnly 쿠키가 들고 있다 — 브라우저는 토큰을 보지 않는다.
//
// 비밀값 취급: 비밀번호는 React 상태에 두지 않고 제출 순간 FormData에서만 읽는다. 코드 · TOTP 등록 키는
// 해당 패널의 메모리 상태로만 있고 패널을 떠나면 사라진다. 어떤 값도 저장소(localStorage 등)나 콘솔에 남기지 않는다.

import QRCode from 'qrcode';
import Image from 'next/image';
import { useRouter } from 'next/navigation';
import { useState, type FormEvent } from 'react';

import { Button } from '@/components/ui/button';
import {
  ApiError,
  getMe,
  login,
  startTotpSetup,
  submitEmailVerification,
  submitNewPassword,
  submitTotp,
  verifyTotpSetup,
} from '@/lib/api/client';
import { authErrorView } from '@/lib/auth-errors';
import { CUSTOMER_HOME, homePathFor } from '@/lib/home-path';
import { safeReturnTo } from '@/lib/return-to';
import { NEW_PASSWORD_MIN_LENGTH, type LoginStep, type LoginStepResponse } from '@/types/api';

type Step = 'PASSWORD' | Exclude<LoginStep, 'COMPLETED'>;

interface TotpSetup {
  secret: string;
  qrDataUrl: string;
}

const INPUT =
  'h-10 w-full rounded-md border border-input bg-transparent px-3 text-sm outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50';
const LABEL = 'text-sm font-medium';
const QR_SIZE = 192;

const HEADINGS: Record<Step, { title: string; description: string }> = {
  PASSWORD: { title: '로그인', description: 'Vigilantis 계정으로 로그인하세요.' },
  NEW_PASSWORD_REQUIRED: {
    title: '새 비밀번호 설정',
    description: '처음 로그인했습니다. 임시 비밀번호를 새 비밀번호로 바꿔 주세요.',
  },
  TOTP_SETUP_REQUIRED: {
    title: '2단계 인증 등록',
    description: '인증 앱으로 QR 코드를 스캔한 뒤, 앱에 표시된 6자리 코드를 입력하세요.',
  },
  TOTP_REQUIRED: { title: '2단계 인증', description: '인증 앱에 표시된 6자리 코드를 입력하세요.' },
  EMAIL_VERIFICATION_REQUIRED: {
    title: '이메일 확인',
    description: '이메일로 받은 6자리 코드를 입력하세요.',
  },
};

export function LoginFlow() {
  const router = useRouter();
  const [step, setStep] = useState<Step>('PASSWORD');
  /** 비밀이 아닌 값이라 상태에 둔다 — 처음부터 다시 할 때 이메일을 다시 치지 않게. */
  const [email, setEmail] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [totpSetup, setTotpSetup] = useState<TotpSetup | null>(null);

  function fail(cause: unknown) {
    const view = authErrorView(cause instanceof ApiError ? cause.code : null);
    setError(view.message);
    if (view.restart) restart();
  }

  function restart() {
    setTotpSetup(null);
    setStep('PASSWORD');
  }

  /**
   * TOTP 등록 키를 받아 QR로 그린다. 서버가 부를 때마다 새 키를 만들 수 있으므로 effect가 아니라
   * 단계 전환 시점에 한 번만 부른다(dev StrictMode의 effect 2회 실행이 키를 갈아치우지 않게).
   */
  async function loadTotpSetup() {
    setPending(true);
    setError(null);
    try {
      const { secret, otpauth_uri } = await startTotpSetup();
      const qrDataUrl = await QRCode.toDataURL(otpauth_uri, { margin: 1, width: QR_SIZE });
      setTotpSetup({ secret, qrDataUrl });
    } catch (cause) {
      fail(cause);
    } finally {
      setPending(false);
    }
  }

  /** 단계 API를 부르고 `next_step`으로 다음 패널을 고른다. */
  async function advance(call: () => Promise<LoginStepResponse>) {
    setPending(true);
    setError(null);
    let next: LoginStep;
    try {
      next = (await call()).next_step;
    } catch (cause) {
      fail(cause);
      setPending(false);
      return;
    }
    if (next === 'COMPLETED') {
      // 세션 쿠키가 발급됐다. 이동하는 동안 폼을 다시 열지 않는다.
      // 로그인 전에 보던 화면(`?next=`, proxy · 401 처리가 붙인다)이 있으면 그리로 돌아간다. 같은 사이트 경로만(lib/return-to.ts).
      // 없으면 역할별 첫 화면이다(lib/home-path.ts). 역할을 못 읽으면 메인 대시보드로 간다. 고객 관리 API 차단은 서버가 한다.
      const returnTo = new URLSearchParams(window.location.search).get('next');
      router.replace(
        returnTo !== null
          ? safeReturnTo(returnTo)
          : await getMe().then((me) => (me ? homePathFor(me) : CUSTOMER_HOME), () => CUSTOMER_HOME),
      );
      return;
    }
    setTotpSetup(null);
    setStep(next);
    setPending(false);
    if (next === 'TOTP_SETUP_REQUIRED') await loadTotpSetup();
  }

  function onPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const nextEmail = String(form.get('email') ?? '').trim();
    setEmail(nextEmail);
    void advance(() => login(nextEmail, String(form.get('password') ?? '')));
  }

  function onNewPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const password = String(form.get('new_password') ?? '');
    if (password.length < NEW_PASSWORD_MIN_LENGTH) {
      setError(`비밀번호는 ${NEW_PASSWORD_MIN_LENGTH}자 이상이어야 합니다.`);
      return;
    }
    if (password !== String(form.get('confirm_password') ?? '')) {
      setError('두 비밀번호가 서로 다릅니다.');
      return;
    }
    void advance(() => submitNewPassword(password));
  }

  const heading = HEADINGS[step];

  return (
    <section className="flex w-full max-w-sm flex-col gap-6 rounded-lg border bg-card p-6">
      <header className="flex flex-col gap-1.5">
        <p className="font-heading text-xs font-semibold tracking-widest text-muted-foreground">
          VIGILANTIS
        </p>
        <h1 className="text-xl font-semibold">{heading.title}</h1>
        {/* 긴 이메일은 끊을 곳이 없어 360px에서 카드를 밀어낸다 — 아무 자리에서나 줄바꿈한다. */}
        <p className="text-sm text-muted-foreground wrap-anywhere">
          {step === 'EMAIL_VERIFICATION_REQUIRED' && email
            ? `${email}(으)로 보낸 6자리 코드를 입력하세요.`
            : heading.description}
        </p>
      </header>

      {error ? (
        <p role="alert" className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      ) : null}

      {step === 'PASSWORD' ? (
        <form className="flex flex-col gap-4" onSubmit={onPassword}>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-email" className={LABEL}>
              이메일
            </label>
            <input
              id="login-email"
              name="email"
              type="email"
              autoComplete="username"
              required
              defaultValue={email}
              disabled={pending}
              className={INPUT}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-password" className={LABEL}>
              비밀번호
            </label>
            <input
              id="login-password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              disabled={pending}
              className={INPUT}
            />
          </div>
          <Button type="submit" size="lg" className="w-full" disabled={pending}>
            {pending ? '확인 중…' : '로그인'}
          </Button>
        </form>
      ) : null}

      {step === 'NEW_PASSWORD_REQUIRED' ? (
        <form className="flex flex-col gap-4" onSubmit={onNewPassword}>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-new-password" className={LABEL}>
              새 비밀번호
            </label>
            <input
              id="login-new-password"
              name="new_password"
              type="password"
              autoComplete="new-password"
              required
              minLength={NEW_PASSWORD_MIN_LENGTH}
              aria-describedby="login-new-password-hint"
              autoFocus
              disabled={pending}
              className={INPUT}
            />
            <p id="login-new-password-hint" className="text-xs text-muted-foreground">
              {NEW_PASSWORD_MIN_LENGTH}자 이상, 대문자·소문자·숫자·기호를 모두 포함하세요.
            </p>
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="login-confirm-password" className={LABEL}>
              새 비밀번호 확인
            </label>
            <input
              id="login-confirm-password"
              name="confirm_password"
              type="password"
              autoComplete="new-password"
              required
              disabled={pending}
              className={INPUT}
            />
          </div>
          <Button type="submit" size="lg" className="w-full" disabled={pending}>
            {pending ? '변경 중…' : '비밀번호 변경'}
          </Button>
        </form>
      ) : null}

      {step === 'TOTP_SETUP_REQUIRED' ? (
        totpSetup ? (
          <div className="flex flex-col gap-4">
            <div className="flex justify-center">
              <Image
                src={totpSetup.qrDataUrl}
                alt="인증 앱 등록용 QR 코드"
                width={QR_SIZE}
                height={QR_SIZE}
                unoptimized
                className="rounded-md bg-white"
              />
            </div>
            <details className="text-sm">
              <summary className="cursor-pointer text-muted-foreground select-none">
                QR을 스캔할 수 없나요?
              </summary>
              <p className="mt-2 text-xs text-muted-foreground">인증 앱에 아래 키를 직접 입력하세요.</p>
              <code className="mt-1 block rounded-md bg-muted px-3 py-2 font-mono text-sm break-all">
                {totpSetup.secret}
              </code>
            </details>
            <CodeForm
              key="totp-setup"
              id="login-totp-setup-code"
              label="인증 앱의 6자리 코드"
              submitLabel="등록 완료"
              pending={pending}
              onSubmit={(code) => advance(() => verifyTotpSetup(code))}
            />
          </div>
        ) : (
          <Button type="button" size="lg" className="w-full" disabled={pending} onClick={loadTotpSetup}>
            {pending ? 'QR 코드 준비 중…' : 'QR 코드 다시 받기'}
          </Button>
        )
      ) : null}

      {step === 'TOTP_REQUIRED' ? (
        <CodeForm
          key="totp"
          id="login-totp-code"
          label="인증 코드"
          submitLabel="확인"
          pending={pending}
          onSubmit={(code) => advance(() => submitTotp(code))}
        />
      ) : null}

      {step === 'EMAIL_VERIFICATION_REQUIRED' ? (
        <CodeForm
          key="email"
          id="login-email-code"
          label="이메일 인증 코드"
          submitLabel="확인"
          pending={pending}
          onSubmit={(code) => advance(() => submitEmailVerification(code))}
        />
      ) : null}

      {step !== 'PASSWORD' ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="self-center text-muted-foreground"
          disabled={pending}
          onClick={() => {
            setError(null);
            restart();
          }}
        >
          처음부터 다시 로그인
        </Button>
      ) : null}
    </section>
  );
}

/** 6자리 숫자 코드 입력. 붙여넣은 값의 공백 · 하이픈은 지우고 숫자 6개만 남긴다. */
function CodeForm({
  id,
  label,
  submitLabel,
  pending,
  onSubmit,
}: {
  id: string;
  label: string;
  submitLabel: string;
  pending: boolean;
  onSubmit: (code: string) => void;
}) {
  const [code, setCode] = useState('');

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (code.length === 6) onSubmit(code);
      }}
    >
      <div className="flex flex-col gap-1.5">
        <label htmlFor={id} className={LABEL}>
          {label}
        </label>
        <input
          id={id}
          name="code"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]{6}"
          required
          autoFocus
          value={code}
          onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))}
          disabled={pending}
          className={`${INPUT} font-mono tracking-[0.3em]`}
        />
      </div>
      <Button type="submit" size="lg" className="w-full" disabled={pending || code.length !== 6}>
        {pending ? '확인 중…' : submitLabel}
      </Button>
    </form>
  );
}
