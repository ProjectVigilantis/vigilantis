// 로그인 뒤 첫 화면. 플랫폼 관리자는 고객 관리(T-4), 고객사 사용자는 메인 대시보드로 간다.
//
// 지금 계약(`GET /auth/me`)에는 사용자의 고객사 소속이 없다(T-1은 단일 소속으로 시작 · #492). 그래서
// 당분간 ADMIN 역할을 플랫폼 관리자로 본다. 고객사 안에도 ADMIN이 생기면 역할만으로는 가를 수 없으므로,
// `/auth/me`가 소속을 실어 주면 이 판정을 소속 기준으로 바꾼다. 화면을 가르는 것은 편의일 뿐이고
// 고객 관리 API의 접근 차단은 서버 몫이다.

import type { CurrentUserResponse } from '../types/api.ts';

export const ADMIN_HOME = '/tenants';
export const CUSTOMER_HOME = '/';

export function homePathFor(user: Pick<CurrentUserResponse, 'role'>): string {
  return user.role === 'ADMIN' ? ADMIN_HOME : CUSTOMER_HOME;
}
