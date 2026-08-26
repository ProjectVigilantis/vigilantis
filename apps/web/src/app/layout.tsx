import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Gnb } from "@/components/gnb";
import { RealtimeProvider } from "@/components/realtime-provider";
import { ToastStack } from "@/components/toast-stack";

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

// 다크 고정(#87)의 마감. 이슈 #89 권장안이자 Next 16 표준 API이고, CSS 로드가
// 실패·지연되는 경로에서도 문서 color scheme이 유효하다. 테마 선언을 dark 클래스
// 옆 한 곳에 모아둔다.
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
        {/* CMN-001이 소켓 수명주기를 소유한다 — layout에 두어야 라우트가 바뀌어도 연결이 유지된다(4.8 1). */}
        <RealtimeProvider>
          <Gnb />
          <main className="flex flex-1 flex-col p-6">{children}</main>
          {/* CMN-001 Toast 스택 — 우하단 고정·8초·최대 3개(4.8). */}
          <ToastStack />
        </RealtimeProvider>
      </body>
    </html>
  );
}
