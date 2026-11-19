import type { Metadata, Viewport } from "next";

import { AppShell } from "@/components/app-shell";
import { THEME_INIT_SCRIPT } from "@/lib/theme";

import "./globals.css";

export const metadata: Metadata = {
  title: "Vigilantis",
  description: "AWS 자산 최적화·보안 위협 대응 관제 대시보드",
};

// 야간이 기본이고 주간은 사이드바 설정에서 고른다(DESIGN.md §야간 모드). CSS가 늦게 오는 경로에서도
// 브라우저가 두 모드를 다 그릴 수 있게 둘 다 선언한다. 실제 모드는 html의 dark 클래스가 정한다.
export const viewport: Viewport = { colorScheme: "light dark" };

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // 기본이 야간이라 dark 클래스를 달아 보낸다. 주간을 고른 브라우저는 THEME_INIT_SCRIPT가 떼므로 경고를 끈다.
    <html lang="ko" className="dark h-full antialiased" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-full flex flex-col">
        {/* 셸 · 실시간 연결 · Toast는 AppShell이 두른다. `/login`만 뺀다(app-shell.tsx). */}
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
