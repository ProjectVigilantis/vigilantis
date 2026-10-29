// 로그인(#492 PR 1). 로그인은 따로 된 화면이 아니라 랜딩 위를 덮는 창이다(components/landing/login-overlay.tsx,
// 2026-10-29 PM 결정). 이 주소는 남겨 둔다: 접근 관문(src/proxy.ts) · 401 처리 · 로그아웃이 `/login?next=…&ended=1`로
// 보내고(lib/auth-redirect.ts), 즐겨찾기도 있을 수 있다. 받은 `next` · `ended`를 그대로 실어 로그인 창이 열린 랜딩으로 넘긴다.

import { redirect } from 'next/navigation';

import { SESSION_ENDED_PARAM } from '@/lib/auth-redirect';
import { LOGIN_OVERLAY_HREF } from '@/lib/return-to';

export default async function LoginPage({ searchParams }: PageProps<'/login'>) {
  const params = await searchParams;
  const carry = new URLSearchParams();
  for (const key of ['next', SESSION_ENDED_PARAM]) {
    const value = params[key];
    if (typeof value === 'string') carry.set(key, value);
  }
  const query = carry.toString();
  redirect(query ? `${LOGIN_OVERLAY_HREF}&${query}` : LOGIN_OVERLAY_HREF);
}
