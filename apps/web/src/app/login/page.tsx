// 로그인(#492 PR 1): 셸 · 실시간 연결 없이 그린다(components/app-shell.tsx). 단계 진행은 LoginFlow가 맡는다.
// 넓은 화면에서는 왼쪽에 서비스 소개 띠를 둔다(DESIGN.md §셸의 남색). 좁은 화면에서는 로그인 카드만 남는다.

import { ArrowLeft, Shield } from 'lucide-react';
import type { Metadata } from 'next';
import Link from 'next/link';

import { LoginFlow } from '@/components/auth/login-flow';
import { LegalLinks } from '@/components/legal/legal-document';
import { SESSION_ENDED_PARAM } from '@/lib/auth-redirect';

export const metadata: Metadata = { title: '로그인 · Vigilantis' };

const POINTS = [
  '24시간 AWS 자산과 보안 위협을 지켜봅니다',
  '조치는 4단계 검증을 통과한 런북으로만 실행합니다',
  '실패하면 직전 상태로 자동 원복합니다',
];

/** `?ended=1` — 쓰던 세션이 끝나 여기로 왔다(lib/auth-redirect.ts). 일반 안내만 띄우고 이유는 말하지 않는다. */
export default async function LoginPage({ searchParams }: PageProps<'/login'>) {
  const ended = (await searchParams)[SESSION_ENDED_PARAM] === '1';
  return (
    <div className="flex flex-1">
      <aside className="dark bg-shell text-foreground relative hidden w-[44%] max-w-xl flex-col justify-between overflow-hidden p-12 lg:flex">
        <div aria-hidden className="landing-grid pointer-events-none absolute inset-0 opacity-60" />
        <Link href="/landing" className="relative flex w-fit items-center gap-2.5">
          <span className="bg-primary flex size-8 items-center justify-center rounded-md">
            <Shield aria-hidden className="size-4.5 text-white" />
          </span>
          <span className="text-lg font-bold tracking-tight">Vigilantis</span>
        </Link>
        <div className="relative flex flex-col gap-6">
          <h2 className="text-3xl leading-tight font-bold tracking-tight">
            잠든 시간에도
            <br />
            클라우드를 지킵니다
          </h2>
          <ul className="flex flex-col gap-3">
            {POINTS.map((p) => (
              <li key={p} className="text-foreground-secondary flex items-center gap-2.5 text-[15px]">
                <span aria-hidden className="bg-status-good size-1.5 shrink-0 rounded-full" />
                {p}
              </li>
            ))}
          </ul>
        </div>
        <p className="text-muted-foreground relative text-xs">© Vigilantis · 딸깍 인프라</p>
      </aside>

      <div className="flex flex-1 flex-col items-center justify-center gap-6 px-4 py-10">
        <Link
          href="/landing"
          className="text-muted-foreground inline-flex items-center gap-1 self-center text-sm hover:underline lg:hidden"
        >
          <ArrowLeft aria-hidden className="size-3.5" />
          서비스 소개
        </Link>
        <LoginFlow sessionEnded={ended} />
        {/* 로그인하면 이메일을 저장하므로 처리방침을 입력 전에 열어 볼 수 있게 둔다(T-15). */}
        <LegalLinks className="text-muted-foreground flex gap-5 text-xs" />
      </div>
    </div>
  );
}
