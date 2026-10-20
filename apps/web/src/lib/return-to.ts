// 로그인 뒤 돌아갈 경로(`/login?next=`) 검증(#492 PR 2). 같은 사이트의 상대 경로만 통과시키고 나머지는 `/`로 접는다.
//
// `next`는 URL에서 오는 값이라 누구나 링크로 심을 수 있다 — 검증 없이 `router.replace(next)`하면 로그인을
// 마친 사용자를 공격자의 사이트로 보내는 열린 리다이렉트가 된다.

export const LOGIN_PATH = '/login';
const FALLBACK = '/';
/** 해석 기준 오리진. 실제 주소와 무관하게 "다른 오리진으로 빠지는가"만 본다. */
const BASE = 'http://return-to.invalid';

/** 제어 문자(C0 · DEL · C1)와 공백류 — 브라우저가 지우거나 바꿔 해석해 검사를 우회하는 통로가 된다. */
const CONTROL_OR_SPACE = /[\u0000-\u0020\u007f-\u009f\s]/;

/**
 * 단일 `/`로 시작하는 같은 오리진 상대 경로인지(입력 · 결과 양쪽에 같은 규칙). 통과하면 정규화한 URL을 돌려준다.
 * `//`(프로토콜 상대) · `/\`와 모든 역슬래시(브라우저가 `/`로 읽는다) · 제어 · 공백 문자 · 해석 결과가 다른 오리진은 거부.
 */
function parseRelativePath(value: string): URL | null {
  if (!value.startsWith('/') || value.startsWith('//')) return null;
  if (value.includes('\\') || CONTROL_OR_SPACE.test(value)) return null;
  try {
    const url = new URL(value, BASE);
    return url.origin === BASE ? url : null;
  } catch {
    return null;
  }
}

const serialize = (url: URL) => `${url.pathname}${url.search}${url.hash}`;

/**
 * 받은 값이 `/`로 시작하는 같은 오리진의 경로면 정규화한 `pathname + search + hash`를, 아니면 `/`를 돌려준다.
 *
 * 거부: 비어 있음 · `/`로 시작하지 않음(스킴 `https:`·`javascript:` 포함) · `//`(프로토콜 상대) ·
 * 역슬래시(`/\evil.com`은 브라우저가 `//evil.com`으로 읽는다) · 제어·공백 문자 · 해석 결과가 다른 오리진 ·
 * 로그인 화면 자신(되돌아가면 로그인을 다시 띄운다). `/%2F%2Fevil.com`처럼 인코딩된 슬래시는 경로 글자일 뿐이라 통과한다.
 *
 * **돌려줄 최종 문자열을 다시 검사한다** — 점 세그먼트 제거가 `//`를 새로 만들 수 있다
 * (`/a/..//evil.example` · `/%2e%2e//evil.example` → `//evil.example`). 결과가 같은 규칙을 통과하고,
 * 다시 정규화해도 그대로여야(브라우저 · 라우터가 한 번 더 해석해도 바뀌지 않아야) 돌려준다.
 */
export function safeReturnTo(raw: string | null | undefined): string {
  if (typeof raw !== 'string' || raw === '') return FALLBACK;
  const url = parseRelativePath(raw);
  if (url === null) return FALLBACK;
  if (url.pathname === LOGIN_PATH || url.pathname.startsWith(`${LOGIN_PATH}/`)) return FALLBACK;

  const result = serialize(url);
  const reparsed = parseRelativePath(result);
  if (reparsed === null || serialize(reparsed) !== result) return FALLBACK;
  return result;
}
