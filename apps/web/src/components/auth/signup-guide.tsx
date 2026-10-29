'use client';

// 회원가입 안내(랜딩 로그인 창, components/landing/login-overlay.tsx). 2026-10-29 PM 요청으로 유사 서비스를 조사해 정했다.
//
// 가입은 회사 단위다. 조사한 보안 · 관제 서비스는 둘로 나뉜다. 셀프 가입(Datadog · 와탭: 이메일 · 회사명으로 가입하면
// 체험 조직이 바로 생긴다)과 신청 · 데모 요청(Wiz · 제니퍼: 영업 문의 뒤 개통, Orca: AWS Marketplace 체험 뒤 교차 계정 역할 연결).
// Vigilantis는 고객 AWS 계정에 역할로 들어가 조치까지 하므로 뒤쪽을 따른다. 회사(테넌트)는 운영자가 만들고 구독도 운영자가
// 관리하며(ADR-0012), 고객사 사용자는 연결이 확인된 뒤(ACTIVE)에만 초대한다. 그래서 화면은 둘이다.
//   - 처음 쓰는 회사: 도입 신청서. 운영팀 확인 → AWS 계정 연결 → 관리자 계정 초대 순서를 미리 보여 준다.
//   - 이미 쓰는 회사의 직원: 직접 가입하지 않는다. 관리자가 초대하면 Cognito가 보낸 메일의 임시 비밀번호로 로그인해
//     새 비밀번호 · 2단계 인증 등록을 마친다(LoginFlow의 첫 로그인 단계). 조사한 서비스도 직원은 관리자 초대가 기본이다
//     (Datadog: 이메일 초대 · 역할 지정 · 초대 대기 상태 · 재발송).
//
// 신청은 `POST /signup-requests`로 보낸다(계약 초안, types/api.ts 끝). 받으면 관리자 웹 알림 종에 뜨고 도입 신청함에 쌓인다.
// 서버에 아직 경로가 없으면(404 · 405) 오류 대신 준비 중이라고 적는다.

import { ArrowLeft, Building2, CircleCheck, MailCheck } from 'lucide-react';
import { useState, type FormEvent } from 'react';

import { INPUT_CLASS as INPUT, LABEL_CLASS as LABEL } from '@/components/form-classes';
import { Button } from '@/components/ui/button';
import { ApiError, submitSignupRequest } from '@/lib/api/client';
import { notYetServed } from '@/lib/support';
import { cn } from '@/lib/utils';

type View = 'CHOOSE' | 'APPLY' | 'INVITED';

/** 초대받은 직원이 로그인으로 넘어갈 때 로그인 단계 위에 띄울 안내. */
export const INVITED_LOGIN_NOTICE =
  '초대 메일에 적힌 이메일과 임시 비밀번호로 로그인하세요. 이어서 새 비밀번호와 2단계 인증을 등록합니다.';

const APPLY_STEPS = [
  { title: '도입 신청', body: '회사와 담당자 정보를 보냅니다.' },
  { title: '운영팀 확인', body: '운영팀이 신청을 확인하고 회사를 등록합니다.' },
  { title: 'AWS 계정 연결', body: '안내받은 CloudFormation 템플릿으로 Vigilantis가 맡을 역할을 만듭니다.' },
  { title: '관리자 초대', body: '연결이 확인되면 회사 관리자 계정이 초대 메일로 발급됩니다. 직원은 관리자가 초대합니다.' },
];

const INVITED_STEPS = [
  { title: '초대 메일 확인', body: '회사 관리자가 초대하면 임시 비밀번호가 담긴 메일이 옵니다.' },
  { title: '임시 비밀번호로 로그인', body: '메일의 이메일과 임시 비밀번호로 로그인합니다.' },
  { title: '가입 마무리', body: '새 비밀번호를 정하고 인증 앱으로 2단계 인증을 등록합니다.' },
];

function Steps({ steps }: { steps: { title: string; body: string }[] }) {
  return (
    <ol className="flex flex-col gap-3">
      {steps.map((s, i) => (
        <li key={s.title} className="flex gap-3">
          <span className="bg-accent text-accent-foreground flex size-6 shrink-0 items-center justify-center rounded-md text-xs font-bold tabular-nums">
            {i + 1}
          </span>
          <div className="flex flex-col gap-0.5">
            <span className="text-sm font-semibold">{s.title}</span>
            <span className="text-muted-foreground text-[13px] leading-relaxed">{s.body}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

export function SignupGuide({ onBack, onInvitedLogin }: { onBack: () => void; onInvitedLogin: () => void }) {
  const [view, setView] = useState<View>('CHOOSE');

  const back = view === 'CHOOSE' ? onBack : () => setView('CHOOSE');
  const heading =
    view === 'APPLY'
      ? { title: '도입 신청', description: '신청을 받으면 운영팀이 확인한 뒤 담당자 이메일로 연락드립니다.' }
      : view === 'INVITED'
        ? { title: '초대받은 직원', description: '직원 계정은 회사 관리자의 초대로 만들어집니다.' }
        : {
            title: '회원가입',
            description: 'Vigilantis는 회사 단위로 가입합니다. 처음 쓰는 회사는 도입 신청을, 직원은 관리자 초대를 받아 가입합니다.',
          };

  return (
    <section className="bg-card flex w-full max-w-md flex-col gap-6 rounded-lg border p-6">
      <header className="flex flex-col gap-1.5">
        <p className="font-heading text-muted-foreground text-xs font-semibold tracking-widest">VIGILANTIS</p>
        <h1 className="text-xl font-semibold">{heading.title}</h1>
        <p className="text-muted-foreground text-sm">{heading.description}</p>
      </header>

      {view === 'CHOOSE' ? (
        <div className="flex flex-col gap-3">
          <button
            type="button"
            onClick={() => setView('APPLY')}
            className="hover:border-primary focus-visible:ring-ring flex gap-3 rounded-lg border p-4 text-left transition-colors focus-visible:ring-2 focus-visible:outline-none"
          >
            <Building2 aria-hidden className="text-primary mt-0.5 size-5 shrink-0" />
            <span className="flex flex-col gap-1">
              <span className="font-semibold">우리 회사가 처음 씁니다</span>
              <span className="text-muted-foreground text-[13px]">도입 신청 후 운영팀 확인, AWS 계정 연결을 거쳐 관리자 계정을 받습니다.</span>
            </span>
          </button>
          <button
            type="button"
            onClick={() => setView('INVITED')}
            className="hover:border-primary focus-visible:ring-ring flex gap-3 rounded-lg border p-4 text-left transition-colors focus-visible:ring-2 focus-visible:outline-none"
          >
            <MailCheck aria-hidden className="text-primary mt-0.5 size-5 shrink-0" />
            <span className="flex flex-col gap-1">
              <span className="font-semibold">회사에서 초대를 받았습니다</span>
              <span className="text-muted-foreground text-[13px]">초대 메일의 임시 비밀번호로 로그인해 가입을 마칩니다.</span>
            </span>
          </button>
          <p className="text-muted-foreground text-[13px] leading-relaxed">
            회사가 이미 Vigilantis를 쓰고 있다면 직접 가입할 수 없습니다. 회사 관리자에게 초대를 요청하세요.
          </p>
        </div>
      ) : null}

      {view === 'APPLY' ? <ApplyForm /> : null}

      {view === 'INVITED' ? (
        <div className="flex flex-col gap-5">
          <Steps steps={INVITED_STEPS} />
          <p className="bg-muted rounded-md px-3 py-2 text-[13px] leading-relaxed">
            초대 메일이 오지 않았거나 임시 비밀번호의 기한이 지났다면 회사 관리자에게 초대 재발송을 요청하세요.
          </p>
          <Button type="button" size="lg" className="w-full" onClick={onInvitedLogin}>
            임시 비밀번호로 로그인
          </Button>
        </div>
      ) : null}

      <Button type="button" variant="link" size="sm" className="self-center" onClick={back}>
        <ArrowLeft aria-hidden className="size-3.5" />
        {view === 'CHOOSE' ? '로그인으로 돌아가기' : '회원가입 처음으로'}
      </Button>
    </section>
  );
}

const optional = (value: FormDataEntryValue | null) => {
  const text = String(value ?? '').trim();
  return text === '' ? null : text;
};

/** 도입 신청서. 보내면 접수 안내로 바뀐다. */
function ApplyForm() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const email = String(form.get('email') ?? '').trim();
    setPending(true);
    setError(null);
    try {
      await submitSignupRequest({
        company_name: String(form.get('company') ?? '').trim(),
        contact_name: String(form.get('name') ?? '').trim(),
        email,
        phone: optional(form.get('phone')),
        note: optional(form.get('note')),
      });
      setSentTo(email);
    } catch (cause) {
      setError(
        cause instanceof ApiError && notYetServed(cause.httpStatus)
          ? '신청 접수 기능이 아직 서버에 열리지 않았습니다.'
          : cause instanceof ApiError
            ? cause.message
            : '신청을 보내지 못했습니다. 잠시 뒤 다시 시도해 주세요.',
      );
    } finally {
      setPending(false);
    }
  };

  if (sentTo !== null) {
    return (
      <div role="status" className="flex flex-col items-start gap-3">
        <CircleCheck aria-hidden className="text-badge-good-fg size-6" />
        <p className="font-semibold">신청을 받았습니다</p>
        <p className="text-muted-foreground text-sm leading-relaxed wrap-anywhere">
          운영팀이 확인한 뒤 {sentTo}(으)로 다음 단계를 안내드립니다. AWS 계정 연결이 확인되면 같은 주소로 관리자 초대 메일이
          갑니다.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <Steps steps={APPLY_STEPS} />
      <form className="flex flex-col gap-4 border-t pt-5" onSubmit={onSubmit}>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="apply-company" className={LABEL}>
            회사명
          </label>
          <input id="apply-company" name="company" required autoComplete="organization" disabled={pending} className={INPUT} />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="apply-name" className={LABEL}>
              담당자 이름
            </label>
            <input id="apply-name" name="name" required autoComplete="name" disabled={pending} className={INPUT} />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="apply-phone" className={LABEL}>
              연락처(선택)
            </label>
            <input id="apply-phone" name="phone" type="tel" autoComplete="tel" disabled={pending} className={INPUT} />
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="apply-email" className={LABEL}>
            업무 이메일
          </label>
          <input id="apply-email" name="email" type="email" required autoComplete="email" disabled={pending} className={INPUT} />
          <span className="text-muted-foreground text-xs">이 이메일이 회사 관리자 계정이 됩니다.</span>
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="apply-note" className={LABEL}>
            관제할 AWS 환경(선택)
          </label>
          <textarea
            id="apply-note"
            name="note"
            rows={3}
            disabled={pending}
            placeholder="예: 계정 2개, 서울 리전, EC2 30대"
            className={cn(INPUT, 'h-auto py-2')}
          />
        </div>
        {error ? (
          <p role="alert" className="bg-destructive/10 text-destructive rounded-md px-3 py-2 text-sm">
            {error}
          </p>
        ) : null}
        <Button type="submit" size="lg" className="w-full" disabled={pending}>
          {pending ? '보내는 중…' : '신청 보내기'}
        </Button>
      </form>
    </div>
  );
}
