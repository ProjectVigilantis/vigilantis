// 서버 컴포넌트 전용 조회(#492 PR 2). `next/headers`를 쓰므로 클라이언트 컴포넌트에서 import하면 빌드가 막는다.
//
// Node fetch에는 브라우저 쿠키가 실리지 않는다 — 들어온 요청의 세션 쿠키 **하나만** 골라 `Cookie` 헤더로 싣는다.
// 다른 쿠키 · 헤더는 API로 넘기지 않는다. API가 401이면 `/login?next=<지금 경로>`로 redirect한다.
//
// redirect는 Next 내부 오류를 던지는 방식이다 — 호출부의 `try/catch`가 삼키지 않게 catch 첫 줄에서
// `unstable_rethrow(error)`를 부르고, 실패를 접는 조회는 아래 `orNull` · `orError`로 감싼다(app/page.tsx 등).

import { cookies, headers } from 'next/headers';
import { redirect, unstable_rethrow } from 'next/navigation';

import { createReadApi } from '@/lib/api/client';
import { fallbacks } from '@/lib/api/fallback';
import { REQUEST_PATH_HEADER, loginUrl, sessionCookieHeader } from '@/lib/auth-redirect';

/** 실패를 null · `{ value, error }`로 접는 조회 래퍼. 401 redirect만은 접지 않는다(lib/api/fallback.ts). */
export const { orNull, orError } = fallbacks(unstable_rethrow);

export async function serverApi(): Promise<ReturnType<typeof createReadApi>> {
  const [cookieStore, headerList] = await Promise.all([cookies(), headers()]);
  const cookie = sessionCookieHeader(cookieStore);
  // proxy가 덮어쓴 값이다(src/proxy.ts). loginUrl이 다시 safeReturnTo로 거른다.
  const returnTo = headerList.get(REQUEST_PATH_HEADER);
  return createReadApi({
    headers: cookie === null ? {} : { Cookie: cookie },
    // 세션 쿠키가 있는데(없으면 proxy가 먼저 보낸다) API가 401이다 — 쓰던 세션이 끝났다.
    onAuthRequired: () => redirect(loginUrl(returnTo, true)),
  });
}
