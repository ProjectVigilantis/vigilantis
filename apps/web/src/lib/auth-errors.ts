// 로그인 오류 코드 → 화면 문구 · 다음 동작(#492 PR 1). 서버 message 대신 짧은 한국어 문구를 쓴다.

import type { ErrorCode } from '../types/api.ts';

export interface AuthErrorView {
  message: string;
  /** true면 진행 중인 로그인이 무효다 — 비밀번호 단계로 되돌린다. false면 같은 단계에서 다시 낸다. */
  restart: boolean;
}

const RESTART = '처음부터 다시 로그인해 주세요.';
const LATER = '잠시 후 다시 시도해 주세요.';
/** 서버가 로그인 진행을 끝내고 flow 쿠키를 지운 일시 장애 — 같은 단계 재시도가 아니라 처음부터다. */
const LATER_RESTART = '잠시 후 처음부터 다시 로그인해 주세요.';
const ADMIN = '관리자에게 문의해 주세요.';

const VIEWS: Partial<Record<ErrorCode, AuthErrorView>> = {
  AUTH_FAILED: { message: '이메일 또는 비밀번호가 올바르지 않습니다.', restart: false },
  AUTH_CODE_INVALID: { message: '코드가 일치하지 않습니다. 다시 입력해 주세요.', restart: false },
  AUTH_FLOW_EXPIRED: { message: `로그인 시간이 지났습니다. ${RESTART}`, restart: true },
  AUTH_STEP_CONFLICT: { message: `로그인 단계가 맞지 않습니다. ${RESTART}`, restart: true },
  AUTH_REQUIRED: { message: `로그인이 필요합니다. ${RESTART}`, restart: true },
  AUTH_PASSWORD_POLICY: {
    message: '비밀번호는 12자 이상이며 대문자·소문자·숫자·기호를 모두 포함해야 합니다.',
    restart: false,
  },
  AUTH_RATE_LIMITED: { message: `시도가 너무 많습니다. ${LATER_RESTART}`, restart: true },
  AUTH_PROVIDER_UNAVAILABLE: {
    message: `인증 서비스에 연결할 수 없습니다. ${LATER_RESTART}`,
    restart: true,
  },
  AUTH_EMAIL_DELIVERY_FAILED: { message: `인증 메일을 보내지 못했습니다. ${LATER_RESTART}`, restart: true },
  ACCOUNT_NOT_PROVISIONED: { message: `사용 권한이 없는 계정입니다. ${ADMIN}`, restart: false },
  ACCOUNT_DISABLED: { message: `비활성화된 계정입니다. ${ADMIN}`, restart: false },
  AUTH_ORIGIN_REJECTED: { message: `허용되지 않은 주소에서 보낸 요청입니다. ${ADMIN}`, restart: false },
  REQUEST_VALIDATION_FAILED: { message: '입력값을 다시 확인해 주세요.', restart: false },
};

/**
 * `code`는 오류 봉투의 코드다. `null`은 응답 자체를 받지 못한 실패(네트워크)다.
 * 계약 밖 코드는 일반 문구로 접고, 같은 단계에 남긴다.
 */
export function authErrorView(code: ErrorCode | null): AuthErrorView {
  if (code === null) return { message: `서버에 연결할 수 없습니다. ${LATER}`, restart: false };
  return VIEWS[code] ?? { message: `로그인 중 오류가 발생했습니다. ${LATER}`, restart: false };
}
