'use client';

// 랜딩 위에서 화면을 다 덮는 로그인 창. 로그인은 따로 된 화면이 아니라 랜딩의 한 상태다(2026-10-29 PM 결정).
// 열림 여부는 주소의 `?login=1`이 정한다: 뒤로 가기로 닫히고, 링크로 바로 열 수 있다. 예전 `/login` 주소와
// 로그인이 필요해 튕겨 온 요청은 `/login`이 이 상태로 보내고(app/login/page.tsx), `next` · `ended`도 그대로 실어 온다.
// 로그인 단계 화면은 LoginFlow가 그대로 맡는다. LoginFlow가 `window.location.search`의 `next`로 돌아갈 곳을 고른다.
// 로그인 아래 [회원가입]은 같은 창 안에서 가입 안내(SignupGuide: 도입 신청 · 초대받은 직원)로 바꾼다.
//
// 랜딩 배경(지구 · 격자)은 창 뒤에 흐리게 비친다. 창은 모달이라 포커스를 가두고 Escape로 닫힌다(Radix Dialog).
// 랜딩 안이 아니라 body에 붙으므로 야간 토큰을 쓰려고 창에 직접 .dark를 단다.

import { Shield, X } from 'lucide-react';
import { Dialog as DialogPrimitive } from 'radix-ui';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useRef, useState } from 'react';

import { LoginFlow } from '@/components/auth/login-flow';
import { INVITED_LOGIN_NOTICE, SignupGuide } from '@/components/auth/signup-guide';
import { LegalLinks } from '@/components/legal/legal-document';
import { SESSION_ENDED_PARAM } from '@/lib/auth-redirect';
import { LOGIN_PARAM } from '@/lib/return-to';

export function LoginOverlay() {
  const params = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const open = params.get(LOGIN_PARAM) === '1';

  // 닫으면 로그인 창에 딸린 값(`login` · `next` · `ended`)을 모두 걷어 낸 랜딩으로 돌아간다. 스크롤 위치는 그대로.
  const close = () => {
    const rest = new URLSearchParams(params);
    for (const key of [LOGIN_PARAM, 'next', SESSION_ENDED_PARAM]) rest.delete(key);
    const query = rest.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={(next) => !next && close()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="bg-shell/70 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0 fixed inset-0 z-50 backdrop-blur-md duration-300" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          // 기본값은 첫 포커스 대상인 [닫기]에 포커스를 둔다. 열자마자 칠 수 있게 첫 입력 칸(이메일)으로 옮긴다.
          onOpenAutoFocus={(event) => {
            const input = (event.currentTarget as HTMLElement).querySelector<HTMLInputElement>('input');
            if (!input) return;
            event.preventDefault();
            input.focus();
          }}
          className="dark text-foreground data-open:animate-in data-open:fade-in-0 data-open:zoom-in-[0.98] data-closed:animate-out data-closed:fade-out-0 fixed inset-0 z-50 flex flex-col overflow-y-auto duration-300 outline-none"
        >
          <DialogPrimitive.Title className="sr-only">Vigilantis 로그인 · 회원가입</DialogPrimitive.Title>
          <div className="mx-auto flex h-16 w-full max-w-6xl shrink-0 items-center justify-between px-4 sm:px-6">
            <span className="flex items-center gap-2.5">
              <span className="bg-primary flex size-8 items-center justify-center rounded-md">
                <Shield aria-hidden className="size-4.5 text-white" />
              </span>
              <span className="text-lg font-bold tracking-tight">Vigilantis</span>
            </span>
            <DialogPrimitive.Close className="text-foreground-secondary hover:text-foreground focus-visible:ring-ring inline-flex h-10 items-center gap-1.5 rounded-md px-3 text-sm font-semibold hover:bg-white/8 focus-visible:ring-2 focus-visible:outline-none">
              <X aria-hidden className="size-4" />
              닫기
            </DialogPrimitive.Close>
          </div>
          <OverlayBody sessionEnded={params.get(SESSION_ENDED_PARAM) === '1'} />
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/**
 * 창 안의 로그인 · 회원가입 전환. 창이 닫히면 Radix가 내용을 내리므로 다시 열 때는 언제나 로그인부터다.
 * 초대받은 직원이 회원가입 안내에서 [임시 비밀번호로 로그인]을 누르면 안내를 단 새 로그인 단계를 띄운다.
 */
function OverlayBody({ sessionEnded }: { sessionEnded: boolean }) {
  const [mode, setMode] = useState<'LOGIN' | 'SIGNUP'>('LOGIN');
  const [invited, setInvited] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  // 화면을 바꾸면 누른 버튼이 사라져 포커스가 길을 잃는다. 새 화면의 첫 입력 칸이나 버튼으로 옮긴다.
  const switchTo = (next: 'LOGIN' | 'SIGNUP') => {
    setMode(next);
    requestAnimationFrame(() => bodyRef.current?.querySelector<HTMLElement>('input, button')?.focus());
  };

  return (
    <div ref={bodyRef} className="flex flex-1 flex-col items-center justify-center gap-6 px-4 py-10">
      {mode === 'LOGIN' ? (
        <LoginFlow
          key={invited ? 'invited' : 'plain'}
          sessionEnded={sessionEnded && !invited}
          initialNotice={invited ? INVITED_LOGIN_NOTICE : null}
          onSignup={() => switchTo('SIGNUP')}
        />
      ) : (
        <SignupGuide
          onBack={() => switchTo('LOGIN')}
          onInvitedLogin={() => {
            setInvited(true);
            switchTo('LOGIN');
          }}
        />
      )}
      {/* 로그인 · 신청 모두 이메일을 받으므로 처리방침을 입력 전에 열어 볼 수 있게 둔다(T-15). */}
      <LegalLinks className="text-muted-foreground flex gap-5 text-xs" />
    </div>
  );
}
