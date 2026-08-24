import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Gnb } from "@/components/gnb";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Vigilantis",
  description: "AWS 자산 최적화·보안 위협 대응 관제 대시보드",
};

// 다크 고정(#87)의 마감. CSS가 아니라 meta로 선언해야 CSS 로드 전에 적용돼
// 첫 페인트에서 스크롤바가 흰색으로 번쩍이지 않는다.
export const viewport: Viewport = { colorScheme: "dark" };

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="ko"
      // 다크 고정. 라이트 모드는 MVP 범위 밖이라(화면설계서 v1.5 §0.3) 테마 전환
      // 라이브러리를 두지 않고 클래스 하나로 끝낸다.
      className={`dark ${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <Gnb />
        <main className="flex flex-1 flex-col p-6">{children}</main>
        {/*
          CMN-001 Toast 스택 자리 — 우하단 고정, 최대 3개(4.8).
          WebSocket 연동 단계에서 이 컨테이너에 Toast를 붙인다.
        */}
        <div
          id="toast-stack"
          aria-live="polite"
          className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-80 flex-col gap-2"
        />
      </body>
    </html>
  );
}
