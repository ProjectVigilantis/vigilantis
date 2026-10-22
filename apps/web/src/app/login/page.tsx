// 로그인(#492 PR 1) — GNB · 실시간 연결 없이 그린다(components/app-shell.tsx). 단계 진행은 LoginFlow가 맡는다.

import type { Metadata } from 'next';

import { LoginFlow } from '@/components/auth/login-flow';
import { SESSION_ENDED_PARAM } from '@/lib/auth-redirect';

export const metadata: Metadata = { title: '로그인 · Vigilantis' };

/** `?ended=1` — 쓰던 세션이 끝나 여기로 왔다(lib/auth-redirect.ts). 일반 안내만 띄우고 이유는 말하지 않는다. */
export default async function LoginPage({ searchParams }: PageProps<'/login'>) {
  const ended = (await searchParams)[SESSION_ENDED_PARAM] === '1';
  return (
    <div className="flex flex-1 items-center justify-center px-4 py-10">
      <LoginFlow sessionEnded={ended} />
    </div>
  );
}
