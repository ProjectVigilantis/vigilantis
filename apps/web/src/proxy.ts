// 접근 보호(#492 PR 2) — 세션 쿠키가 **없으면** 로그인 화면으로 보낸다. 있는지만 본다: 유효성(만료 · 회수)은
// API가 판정하고, 그 401은 서버 조회(lib/api/server.ts)와 브라우저 조회(lib/api/client.ts)가 받아 로그인으로 보낸다.
//
// 현재 `pathname + search`를 요청 헤더로 서버 컴포넌트에 넘긴다 — 401 redirect의 `next`를 만들 자리가 그것뿐이다.
// 클라이언트가 같은 이름의 헤더를 보내도 여기서 덮어쓴다.

import { NextResponse, type NextRequest } from 'next/server';

import { REQUEST_PATH_HEADER, SESSION_COOKIE, loginUrl } from '@/lib/auth-redirect';
import { LOGIN_PATH } from '@/lib/return-to';

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const current = `${pathname}${search}`;

  if (pathname !== LOGIN_PATH && !request.cookies.get(SESSION_COOKIE)?.value) {
    return NextResponse.redirect(new URL(loginUrl(current), request.url));
  }

  const forwarded = new Headers(request.headers);
  forwarded.set(REQUEST_PATH_HEADER, current);
  return NextResponse.next({ request: { headers: forwarded } });
}

export const config = {
  // Next 내부 · 정적 파일은 건너뛴다(로그인 화면의 JS · CSS · 이미지도 이 경로로 받는다).
  matcher: ['/((?!_next/static|_next/image|favicon\\.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)'],
};
