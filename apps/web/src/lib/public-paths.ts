// 로그인 없이 여는 공개 경로(#492). 두 소비자가 이 목록 하나를 쓴다 —
// proxy(src/proxy.ts: 세션 쿠키가 없어도 로그인으로 보내지 않는다)와 AppShell(components/app-shell.tsx: 셸 ·
// 세션 조회(`/auth/me`) · 유휴 연장 · 실시간 연결 · GNB 자산 폴링 · Toast를 두르지 않는다).
// 경로는 정확히 일치할 때만 공개다(하위 경로는 아니다).

import { ENROLL_PATH, LANDING_PATH, LOGIN_PATH } from './return-to.ts';

const PUBLIC_PATHS: ReadonlySet<string> = new Set([LOGIN_PATH, LANDING_PATH, ENROLL_PATH]);

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.has(pathname);
}
