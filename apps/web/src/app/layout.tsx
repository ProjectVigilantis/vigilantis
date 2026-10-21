import type { Metadata, Viewport } from "next";

import { AppShell } from "@/components/app-shell";
import { THEME_INIT_SCRIPT } from "@/lib/theme";

import "./globals.css";

export const metadata: Metadata = {
  title: "Vigilantis",
  description: "AWS 자산 최적화·보안 위협 대응 관제 대시보드",
};

// 주간이 기본이고 야간은 상단바에서 고른다(DESIGN.md §야간 모드). CSS가 늦게 오는 경로에서도
// 브라우저가 두 모드를 다 그릴 수 있게 둘 다 선언한다. 실제 모드는 html의 dark 클래스가 정한다.
export const viewport: Viewport = { colorScheme: "light dark" };

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    // dark 클래스는 첫 화면 전에 THEME_INIT_SCRIPT가 붙인다. 서버 HTML과 달라지므로 경고를 끈다.
    <html lang="ko" className="h-full antialiased" suppressHydrationWarning>
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
