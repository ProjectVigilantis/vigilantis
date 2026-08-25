'use client';

// CMN-002 전역 오류 셸 — root layout을 타지 않으므로 html·body·테마·스타일을 자체 선언합니다(이슈 #112).

import { Geist, Geist_Mono } from 'next/font/google';

import { ErrorState } from '@/components/error-state';
import { Button } from '@/components/ui/button';

// root layout의 import가 이 셸에는 걸리지 않는다. 안 넣으면 Tailwind 클래스가 통째로 죽는다.
import './globals.css';

// 폰트도 같은 이유로 다시 선언한다. globals.css의 `--font-sans`는 아래 변수를 **참조만** 하고
// 정의는 layout.tsx의 next/font 클래스가 하므로, 이 셸에서는 미정의가 되어 브라우저 기본
// 폰트(serif)로 떨어진다. Next 문서도 global-error가 폰트를 자체 선언하도록 명시한다.
const geistSans = Geist({ variable: '--font-geist-sans', subsets: ['latin'] });
const geistMono = Geist_Mono({ variable: '--font-geist-mono', subsets: ['latin'] });

export default function GlobalError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  /** Next 16은 `reset`이 아니라 `retry`를 넘긴다 — 경계 children을 다시 렌더한다. */
  retry: () => void;
}) {
  return (
    <html
      lang="ko"
      className={`dark ${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      {/*
        layout.tsx의 `viewport` export는 이 셸에 적용되지 않는다(#112) — 같은 선언을 직접 넣는다.
        #89에서 정한 대로 CSS가 아니라 meta로 선언해, CSS 로드가 실패·지연돼도 문서 color scheme이 산다.
      */}
      <meta name="color-scheme" content="dark" />
      <body className="flex min-h-full items-center justify-center p-6">
        <div className="flex w-full max-w-md flex-col items-center gap-4">
          {/* 전역 크래시는 오류 봉투가 아니다 — ErrorState가 INTERNAL_ERROR(전체 오류 화면)로 접는다(§4.9). */}
          <ErrorState error={error} variant="page" className="w-full" />
          {/* Next가 주는 복구 수단. 이게 없으면 브라우저 새로고침 외에 빠져나갈 길이 없다. */}
          <Button type="button" variant="outline" size="sm" onClick={() => retry()}>
            다시 시도
          </Button>
        </div>
      </body>
    </html>
  );
}
