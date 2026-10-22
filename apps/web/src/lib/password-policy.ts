// 새 비밀번호 정책(#492 PR 3) — 최초 비밀번호 변경과 비밀번호 재설정 확인이 함께 쓰는 순수 함수.
//
// 화면 편의다 — 최종 판정은 서버(공통 정책 함수 → User Pool)이고, 위반은 422 `AUTH_PASSWORD_POLICY`로 온다.
// 입력값을 다듬지 않는다: trim · 유니코드 정규화 · 대소문자 변환 없이 받은 문자열 그대로 판정하고 그대로 보낸다.

import { NEW_PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH } from '../types/api.ts';

/**
 * 허용 기호 — Cognito 비밀번호 정책이 기호로 인정하는 ASCII 문장부호 32자(공백 제외).
 * 출처: https://docs.aws.amazon.com/cognito/latest/developerguide/managing-users-passwords.html
 */
export const PASSWORD_SYMBOLS = '^$*.[]{}()?"!@#%&/\\,><\':;|_~`=+-';

const SYMBOL_SET = new Set(PASSWORD_SYMBOLS);
const isUpper = (c: string) => c >= 'A' && c <= 'Z';
const isLower = (c: string) => c >= 'a' && c <= 'z';
const isDigit = (c: string) => c >= '0' && c <= '9';
const isAllowed = (c: string) => isUpper(c) || isLower(c) || isDigit(c) || SYMBOL_SET.has(c);

/** 실시간으로 보여 주는 조건 6개. 값은 boolean뿐이다 — 비밀번호 자체는 담지 않는다. */
export interface PasswordChecks {
  /** 12자 이상 256자 이하(코드 포인트 기준 — 서버 길이 검사와 같은 단위). */
  length: boolean;
  upper: boolean;
  lower: boolean;
  digit: boolean;
  symbol: boolean;
  /** 비어 있지 않고, 영문 · 숫자 · 허용 기호만 썼다. */
  allowedOnly: boolean;
}

export interface PasswordEvaluation {
  checks: PasswordChecks;
  /** 허용되지 않은 문자가 하나라도 있다(공백 · 한글 · 전각 등). 어떤 문자인지는 담지 않는다. */
  hasForbidden: boolean;
  /** 확인 칸에 허용되지 않은 문자가 있다. 조건 6개는 확인 칸에 다시 걸지 않는다 — 일치 여부가 덮는다. */
  confirmHasForbidden: boolean;
  /** 확인 칸이 비어 있지 않고 새 비밀번호와 정확히 같다. */
  matches: boolean;
}

const hasForbiddenChar = (value: string) => Array.from(value).some((c) => !isAllowed(c));

export function evaluatePassword(password: string, confirm: string): PasswordEvaluation {
  const chars = Array.from(password);
  const hasForbidden = hasForbiddenChar(password);
  return {
    checks: {
      length: chars.length >= NEW_PASSWORD_MIN_LENGTH && chars.length <= PASSWORD_MAX_LENGTH,
      upper: chars.some(isUpper),
      lower: chars.some(isLower),
      digit: chars.some(isDigit),
      symbol: chars.some((c) => SYMBOL_SET.has(c)),
      allowedOnly: chars.length > 0 && !hasForbidden,
    },
    hasForbidden,
    confirmHasForbidden: hasForbiddenChar(confirm),
    matches: confirm !== '' && password === confirm,
  };
}

export function policyMet(checks: PasswordChecks): boolean {
  return Object.values(checks).every(Boolean);
}

export const POLICY_UNMET = '비밀번호 조건을 모두 충족해 주세요.';
export const CONFIRM_MISMATCH = '두 비밀번호가 서로 다릅니다.';

/** 제출 직전 판정 — 통과하면 null. 자동 입력처럼 입력 이벤트를 놓친 경우에도 이 값으로 API 호출을 막는다. */
export function newPasswordError(password: string, confirm: string): string | null {
  const result = evaluatePassword(password, confirm);
  if (!policyMet(result.checks)) return POLICY_UNMET;
  if (!result.matches) return CONFIRM_MISMATCH;
  return null;
}
