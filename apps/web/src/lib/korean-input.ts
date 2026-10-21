// 한글 입력 경고(#492 PR 3) — 로그인 비밀번호 · 이메일 칸에 한글이 들어갔는지만 본다(한/영 전환 실수 안내).
// 값을 바꾸거나 제출을 막지 않는다. 판정 근거는 **입력값에 실제로 있는 한글**이다 — 조합 이벤트
// (compositionstart)는 한국어가 아닌 IME에서도 일어나 근거로 쓰지 않는다.

/** 한글 자모(U+1100–11FF) · 호환 자모(U+3130–318F) · 자모 확장 A(U+A960–A97F) · 음절(U+AC00–D7AF) · 자모 확장 B(U+D7B0–D7FF) · 반각 자모(U+FFA0–FFDC). */
const HANGUL = /[\u1100-\u11FF\u3130-\u318F\uA960-\uA97F\uAC00-\uD7FF\uFFA0-\uFFDC]/;

export function containsHangul(value: string): boolean {
  return HANGUL.test(value);
}

export const HANGUL_WARNING = '한글이 입력되어 있습니다. 한/영 전환 상태를 확인해 주세요.';
