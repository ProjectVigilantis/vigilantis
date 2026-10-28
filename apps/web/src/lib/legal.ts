// 개인정보 처리방침 · 이용약관(T-15)이 함께 쓰는 운영자 정보. 두 문서와 바닥글이 같은 값을 보도록 한 곳에 둔다.
// 문서 내용을 바꾸면 시행일도 함께 바꾼다. 처리방침은 바뀐 내용을 시행 7일 전에 알린다고 적어 두었다.

export const OPERATOR_NAME = '딸깍 인프라';
export const SERVICE_NAME = 'Vigilantis';

export const PRIVACY_OFFICER = {
  name: '김세혁',
  position: '팀장',
  email: '__TEAM_EMAIL__',
} as const;

/** 두 문서의 시행일. 화면에는 `formatLegalDate`로 적는다. */
export const LEGAL_EFFECTIVE_DATE = '2026-10-28';

export function formatLegalDate(iso: string): string {
  const [year, month, day] = iso.split('-').map(Number);
  return `${year}년 ${month}월 ${day}일`;
}
