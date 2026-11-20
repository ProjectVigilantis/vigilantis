// 개인정보 처리방침 · 이용약관(T-15)이 함께 쓰는 운영자 정보. 두 문서와 바닥글이 같은 값을 보도록 한 곳에 둔다.
// 문서 내용을 바꾸면 시행일도 함께 바꾼다. 처리방침은 바뀐 내용을 시행 7일 전에 알린다고 적어 두었다.

import { ROLE_LABELS } from './user-admin.ts';

export const OPERATOR_NAME = '딸깍 인프라';
export const SERVICE_NAME = 'Vigilantis';

// 이메일은 서비스 도메인 주소다. 받는 사람은 메일 전달 설정에서 정하므로 담당자가 바뀌어도 여기는 그대로 둔다.
export const PRIVACY_OFFICER = {
  name: '김세혁',
  position: '팀장',
  email: 'privacy@vigilantis.dev',
} as const;

/**
 * 고객센터(2026-11-19 PM 결정). 이용 질문(Q&A)과 도입 중 오류를 받는 창구다. 서비스 장애는 여기가 아니라
 * 로그인 후 사이드바의 문의하기로 받는다. 전화는 두지 않는다.
 */
export const SUPPORT_CENTER = {
  email: 'support@vigilantis.dev',
  hours: '09:00-18:00',
} as const;

/** 두 문서가 적는 역할 이름. 사용자 관리 화면의 표시 이름과 같은 말을 쓴다. */
export const ROLE_NAMES = [ROLE_LABELS.ADMIN, ROLE_LABELS.OPERATOR, ROLE_LABELS.VIEWER].join(' · ');

/** 처리방침 시행일. 화면에는 `formatLegalDate`로 적는다. 첫 시행은 플랫폼 상시 운영 시작일(11/16, 2026-11-02 회의)이고,
 * 신고 작성 보조 초안의 국외 이전 추가(#648)로 시행 7일 전 고지를 지켜 옮겼다. */
export const LEGAL_EFFECTIVE_DATE = '2026-11-27';

/** 이용약관 시행일(2026-11-19 PM 결정). 처리방침과 따로 둔다. */
export const TERMS_EFFECTIVE_DATE = '2026-12-09';

export function formatLegalDate(iso: string): string {
  const [year, month, day] = iso.split('-').map(Number);
  return `${year}년 ${month}월 ${day}일`;
}
