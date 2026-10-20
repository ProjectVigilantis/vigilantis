// 로그인(#492 PR 1) — GNB · 실시간 연결 없이 그린다(components/app-shell.tsx). 단계 진행은 LoginFlow가 맡는다.

import type { Metadata } from 'next';

import { LoginFlow } from '@/components/auth/login-flow';

export const metadata: Metadata = { title: '로그인 · Vigilantis' };

export default function LoginPage() {
  return (
    <div className="flex flex-1 items-center justify-center px-4 py-10">
      <LoginFlow />
    </div>
  );
}
