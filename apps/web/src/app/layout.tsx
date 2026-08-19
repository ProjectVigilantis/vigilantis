import type { Metadata } from "next";
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

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="ko"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
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
