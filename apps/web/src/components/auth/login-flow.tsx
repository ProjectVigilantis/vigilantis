'use client';

// 로그인 단계 화면(#492 PR 1). 서버가 `next_step`으로 다음 단계를 정하고, 여기서는 그 값으로 패널만 고른다.
// 진행 · 세션은 서버의 HttpOnly 쿠키가 들고 있다 — 브라우저는 토큰을 보지 않는다.
//
// 비밀번호 재설정(#492 PR 3)은 서버 단계가 아니라 이 화면의 단계(FORGOT_*)다 — 코드 요청 → 코드 + 새 비밀번호 확인 →
// 비밀번호 단계로 돌아와 새 비밀번호로 로그인한다(재설정 API는 세션을 만들지 않는다).
//
// 비밀값 취급: 비밀번호는 React 상태에 두지 않고 제출 순간 FormData에서만 읽는다(새 비밀번호 칸은 조건 충족 여부
// boolean만 상태에 둔다). 코드 · TOTP 등록 키는 해당 패널의 메모리 상태로만 있고 패널을 떠나면 사라진다.
// 어떤 값도 저장소(localStorage 등)나 콘솔에 남기지 않는다. 이메일 · 비밀번호는 다듬지 않고(trim 없음) 입력 그대로 보낸다.

import QRCode from 'qrcode';
import Image from 'next/image';
import { useRouter } from 'next/navigation';
import { useRef, useState, type FormEvent } from 'react';

import { HangulWarnedInput } from '@/components/auth/hangul-warned-input';
import { NewPasswordFields } from '@/components/auth/new-password-fields';
import { INPUT_CLASS as INPUT, LABEL_CLASS as LABEL } from '@/components/form-classes';
import { Button } from '@/components/ui/button';
import {
  ApiError,
  confirmPasswordReset,
  getMe,
  login,
  requestPasswordReset,
  startTotpSetup,
  submitEmailVerification,
  submitNewPassword,
  submitTotp,
  verifyTotpSetup,
} from '@/lib/api/client';
import { authErrorView, resetConfirmErrorMessage, resetRequestErrorMessage } from '@/lib/auth-errors';
import { CUSTOMER_HOME, homePathFor } from '@/lib/home-path';
import { newPasswordError } from '@/lib/password-policy';
import { safeReturnTo } from '@/lib/return-to';
import type { LoginStep, LoginStepResponse } from '@/types/api';

type Step = 'PASSWORD' | 'FORGOT_REQUEST' | 'FORGOT_CONFIRM' | Exclude<LoginStep, 'COMPLETED'>;

interface TotpSetup {
  secret: string;
  qrDataUrl: string;
}

const QR_SIZE = 192;

/** 쓰던 세션이 끝나 로그인으로 왔다(`?ended=1`). 이유(만료 · 로그아웃 · 다른 곳 로그인 · 회수)는 알 수 없어 말하지 않는다. */
const SESSION_ENDED_NOTICE = '로그인 세션이 종료되었습니다. 다시 로그인해 주세요.';
/** 사용자 존재 여부 · 발송 대상을 드러내지 않는다 — 서버도 항상 같은 204다. */
const RESET_CODE_REQUESTED =
  '등록된 계정이면 재설정 코드가 발송됩니다. 받은 코드와 새 비밀번호를 입력해 주세요.';
const RESET_DONE = '비밀번호를 바꿨습니다. 이 계정의 기존 로그인은 모두 종료되었습니다. 새 비밀번호로 로그인해 주세요.';

const codeOf = (cause: unknown) => (cause instanceof ApiError ? cause.code : null);

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
  FORGOT_REQUEST: {
    title: '비밀번호 재설정',
    description: '계정 이메일을 입력하면 재설정 코드를 보내 드립니다.',
  },
  FORGOT_CONFIRM: {
    title: '새 비밀번호 설정',
    description: '받은 6자리 코드와 새 비밀번호를 입력하세요.',
  },
};

export function LoginFlow({ sessionEnded = false }: { sessionEnded?: boolean }) {
  const router = useRouter();
  const [step, setStep] = useState<Step>('PASSWORD');
  /** 비밀이 아닌 값이라 상태에 둔다 — 처음부터 다시 할 때 이메일을 다시 치지 않게. */
  const [email, setEmail] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 오류가 아닌 안내(세션 종료 · 재설정 진행). 다음 제출 때 지운다. */
  const [notice, setNotice] = useState<string | null>(sessionEnded ? SESSION_ENDED_NOTICE : null);
  const [totpSetup, setTotpSetup] = useState<TotpSetup | null>(null);
  const loginEmailRef = useRef<HTMLInputElement>(null);

  function fail(cause: unknown) {
    const view = authErrorView(codeOf(cause));
    setError(view.message);
    if (view.restart) restart();
  }

  /** 단계 전환과 함께 안내 · 오류를 지운다. */
  function goTo(next: Step, nextNotice: string | null = null) {
    setError(null);
    setNotice(nextNotice);
    setStep(next);
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
    setNotice(null);
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
      // 없으면 계정 종류 · 역할별 첫 화면이다(lib/home-path.ts). 계정을 못 읽으면 `/`에서 다시 확인한다. 차단은 서버가 한다.
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
    const nextEmail = String(form.get('email') ?? '');
    setEmail(nextEmail);
    void advance(() => login(nextEmail, String(form.get('password') ?? '')));
  }

  function onNewPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const password = String(form.get('new_password') ?? '');
    // 조건을 못 채우면 API를 부르지 않는다 — 자동 입력으로 실시간 표시를 놓쳤어도 여기서 다시 판정한다.
    const problem = newPasswordError(password, String(form.get('confirm_password') ?? ''));
    if (problem !== null) {
      setError(problem);
      return;
    }
    void advance(() => submitNewPassword(password));
  }

  async function onResetRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextEmail = String(new FormData(event.currentTarget).get('email') ?? '');
    setEmail(nextEmail);
    setPending(true);
    setError(null);
    setNotice(null);
    try {
      await requestPasswordReset(nextEmail);
    } catch (cause) {
      setError(resetRequestErrorMessage(codeOf(cause)));
      setPending(false);
      return;
    }
    setPending(false);
    goTo('FORGOT_CONFIRM', RESET_CODE_REQUESTED);
  }

  async function onResetConfirm(code: string, password: string, confirm: string) {
    const problem = newPasswordError(password, confirm);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setPending(true);
    setError(null);
    setNotice(null);
    try {
      await confirmPasswordReset(email, code, password);
    } catch (cause) {
      setError(resetConfirmErrorMessage(codeOf(cause)));
      setPending(false);
      return;
    }
    setPending(false);
    goTo('PASSWORD', RESET_DONE);
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
            : step === 'FORGOT_CONFIRM' && email
              ? `${email} 계정의 재설정 코드와 새 비밀번호를 입력하세요.`
              : heading.description}
        </p>
      </header>

      {notice ? (
        <p role="status" className="rounded-md bg-muted px-3 py-2 text-sm">
          {notice}
        </p>
      ) : null}

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
            <HangulWarnedInput
              ref={loginEmailRef}
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
            <HangulWarnedInput
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
          <Button
            type="button"
            variant="link"
            size="sm"
            className="self-center"
            disabled={pending}
            onClick={() => {
              // 적어 둔 이메일을 재설정 칸으로 넘긴다(비밀이 아닌 값).
              setEmail(loginEmailRef.current?.value ?? email);
              goTo('FORGOT_REQUEST');
            }}
          >
            비밀번호를 잊으셨나요?
          </Button>
        </form>
      ) : null}

      {step === 'NEW_PASSWORD_REQUIRED' ? (
        <form className="flex flex-col gap-4" onSubmit={onNewPassword}>
          <NewPasswordFields idPrefix="login" disabled={pending} autoFocus />
          <Button type="submit" size="lg" className="w-full" disabled={pending}>
            {pending ? '변경 중…' : '비밀번호 변경'}
          </Button>
        </form>
      ) : null}

      {step === 'FORGOT_REQUEST' ? (
        <form className="flex flex-col gap-4" onSubmit={onResetRequest}>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="reset-email" className={LABEL}>
              이메일
            </label>
            <HangulWarnedInput
              id="reset-email"
              name="email"
              type="email"
              autoComplete="username"
              required
              autoFocus
              defaultValue={email}
              disabled={pending}
              className={INPUT}
            />
          </div>
          <Button type="submit" size="lg" className="w-full" disabled={pending}>
            {pending ? '요청 중…' : '재설정 코드 받기'}
          </Button>
        </form>
      ) : null}

      {step === 'FORGOT_CONFIRM' ? (
        <>
          <ResetConfirmForm pending={pending} onSubmit={onResetConfirm} />
          <Button
            type="button"
            variant="link"
            size="sm"
            className="self-center"
            disabled={pending}
            onClick={() => goTo('FORGOT_REQUEST')}
          >
            코드 다시 받기
          </Button>
        </>
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
            setNotice(null);
            restart();
          }}
        >
          {step === 'FORGOT_REQUEST' || step === 'FORGOT_CONFIRM' ? '로그인으로 돌아가기' : '처음부터 다시 로그인'}
        </Button>
      ) : null}
    </section>
  );
}

/** 6자리 숫자 코드 칸. 붙여넣은 값의 공백 · 하이픈은 지우고 숫자 6개만 남긴다. */
function CodeField({
  id,
  label,
  value,
  pending,
  autoFocus = true,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  pending: boolean;
  autoFocus?: boolean;
  onChange: (code: string) => void;
}) {
  return (
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
        autoFocus={autoFocus}
        value={value}
        onChange={(event) => onChange(event.target.value.replace(/\D/g, '').slice(0, 6))}
        disabled={pending}
        className={`${INPUT} font-mono tracking-[0.3em]`}
      />
    </div>
  );
}

/** 재설정 확인 — 코드 + 새 비밀번호를 한 번에 낸다. 판정 · 호출은 부모(`onResetConfirm`)가 한다. */
function ResetConfirmForm({
  pending,
  onSubmit,
}: {
  pending: boolean;
  onSubmit: (code: string, password: string, confirm: string) => void;
}) {
  const [code, setCode] = useState('');

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (code.length !== 6) return;
        const form = new FormData(event.currentTarget);
        onSubmit(code, String(form.get('new_password') ?? ''), String(form.get('confirm_password') ?? ''));
      }}
    >
      <CodeField id="reset-code" label="재설정 코드" value={code} pending={pending} onChange={setCode} />
      <NewPasswordFields idPrefix="reset" disabled={pending} />
      <Button type="submit" size="lg" className="w-full" disabled={pending || code.length !== 6}>
        {pending ? '변경 중…' : '비밀번호 재설정'}
      </Button>
    </form>
  );
}

/** 6자리 숫자 코드 하나만 내는 단계(TOTP · 이메일 확인). */
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
      <CodeField id={id} label={label} value={code} pending={pending} onChange={setCode} />
      <Button type="submit" size="lg" className="w-full" disabled={pending || code.length !== 6}>
        {pending ? '확인 중…' : submitLabel}
      </Button>
    </form>
  );
}
