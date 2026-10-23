// 플랫폼 운영자 초대 링크 등록(#492) — `/team/enroll#token=…`. 로그인 없이 열리고(src/proxy.ts) 셸 · 세션 조회 ·
// 실시간 연결 없이 그린다(components/app-shell.tsx, 목록은 lib/public-paths.ts). 일반 내비게이션 · 랜딩에서 잇지 않는다.
// 서버는 토큰을 보지 않는다(URL 조각) — 이 페이지는 토큰 없이 렌더되고, 판독 · 요청은 브라우저(PlatformEnrollment)가 한다.

import type { Metadata } from 'next';

import { PlatformEnrollment } from '@/components/team/platform-enrollment';

export const metadata: Metadata = {
  title: '플랫폼 운영자 계정 만들기 · Vigilantis',
  // 일회용 초대 화면 — 검색에 올리지 않고, 이 화면에서 나가는 요청에 주소를 싣지 않는다.
  robots: { index: false, follow: false },
  referrer: 'no-referrer',
};

export default function TeamEnrollPage() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center px-4 py-10">
      <PlatformEnrollment />
    </div>
  );
}
