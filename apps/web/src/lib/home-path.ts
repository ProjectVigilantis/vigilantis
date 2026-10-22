// 로그인 뒤 첫 화면. 플랫폼 관리자는 고객 관리(T-4), 고객사 사용자는 메인 대시보드로 간다.
//
// 지금 계약(`GET /auth/me`)에는 플랫폼 운영자인지 알려 주는 값이 없다. 그래서 당분간 ADMIN 역할을
// 플랫폼 관리자로 본다. 고객사 안에도 ADMIN이 생기면 역할만으로는 가를 수 없으므로, T-1(#492)이
// `/auth/me`에 플랫폼 운영자 표시를 실어 주면 이 판정을 그 표시 기준으로 바꾼다. 사용자의 `tenant_id`로
// 가르지 않는다. NULL은 소속이 없다는 뜻일 뿐 권한이 아니다(#506, PR #504 리뷰). 화면을 가르는 것은
// 편의일 뿐이고 고객 관리 API의 접근 차단은 서버 몫이다.

import type { CurrentUserResponse } from '../types/api.ts';

export const ADMIN_HOME = '/tenants';
export const CUSTOMER_HOME = '/';

export function homePathFor(user: Pick<CurrentUserResponse, 'role'>): string {
  return user.role === 'ADMIN' ? ADMIN_HOME : CUSTOMER_HOME;
}
