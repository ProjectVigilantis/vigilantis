// 미인증 처리의 공통 조각(#492 PR 2) — 세션 쿠키 이름, proxy가 넘기는 경로 헤더, 로그인 주소 조립.
// proxy(`src/proxy.ts`) · 서버 조회(`lib/api/server.ts`) · 브라우저 조회(`lib/api/client.ts`) · 실시간 연결이 같이 쓴다.

import { LOGIN_PATH, safeReturnTo } from './return-to.ts';

/** core-api가 심는 세션 쿠키(HttpOnly · host-only). 값은 웹이 해석하지 않는다 — 유효성은 API가 판정한다. */
export const SESSION_COOKIE = '__Host-vg_session';

/**
 * proxy가 서버 컴포넌트에 넘기는 현재 `pathname + search`. 서버 컴포넌트는 요청 URL을 직접 못 읽어서
 * 401 리다이렉트의 `next`를 이 헤더로 만든다. proxy가 **항상 덮어쓰므로** 클라이언트가 보낸 값은 남지 않는다.
 */
export const REQUEST_PATH_HEADER = 'x-vg-request-path';

/**
 * 쓰던 세션이 끝나 로그인으로 보냈다는 화면 표시(#492 PR 3). 값은 `1` 하나뿐이다 — **끝난 이유는 싣지 않는다.**
 * 서버도 이유를 알려 주지 않으므로(401 `AUTH_REQUIRED` · WebSocket 4401) 로그인 화면은 일반 안내만 띄운다.
 */
export const SESSION_ENDED_PARAM = 'ended';

/**
 * `next`를 붙인 로그인 주소. 돌아갈 곳이 `/`이거나 안전하지 않으면 `next` 없이 `/login`이다.
 * `sessionEnded`면 세션 종료 안내 표시를 함께 붙인다 — 세션 쿠키가 없어 처음 들어오는 경우(proxy)는 붙이지 않는다.
 */
export function loginUrl(returnTo: string | null | undefined, sessionEnded = false): string {
  const next = safeReturnTo(returnTo);
  const query = [
    ...(next === '/' ? [] : [`next=${encodeURIComponent(next)}`]),
    ...(sessionEnded ? [`${SESSION_ENDED_PARAM}=1`] : []),
  ];
  return query.length === 0 ? LOGIN_PATH : `${LOGIN_PATH}?${query.join('&')}`;
}

interface CookieReader {
  get(name: string): { value: string } | undefined;
}

/**
 * 서버에서 API로 보낼 `Cookie` 헤더 값 — **세션 쿠키 하나만** 싣는다. 브라우저가 웹에 보낸 다른 쿠키
 * (로그인 진행 쿠키 · 분석 도구 등)는 API로 넘기지 않는다. 세션 쿠키가 없거나 비어 있으면 null.
 */
export function sessionCookieHeader(cookies: CookieReader): string | null {
  const value = cookies.get(SESSION_COOKIE)?.value;
  return value ? `${SESSION_COOKIE}=${value}` : null;
}

/**
 * 브라우저에서 로그인 화면으로 보낸다(전체 이동 — 세션이 끝난 화면의 상태를 남기지 않는다).
 * 부르는 자리는 전부 쓰던 세션이 끝난 경우다(API 401 · WebSocket 4401 �� 만료 시각 도달) — 세션 종료 안내를 붙인다.
 * 서버 렌더 중(window 없음)이거나 이미 로그인 화면이면 아무것도 하지 않는다 — 로그인 진행 중의 상태를 날리지 않게.
 */
export function navigateToLogin(): void {
  if (typeof window === 'undefined') return;
  const { pathname, search } = window.location;
  if (pathname === LOGIN_PATH) return;
  window.location.assign(loginUrl(`${pathname}${search}`, true));
}
