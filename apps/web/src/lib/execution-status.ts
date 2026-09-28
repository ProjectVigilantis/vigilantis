// 실행 status의 최종 여부 — 화면설계서 v1.5 §4.7. ACT-002 패널과 INC-002 실행 잠금이 같은 기준을 씁니다.

import type { ExecutionStatus } from '@/types/api';

/**
 * 최종 상태 5종. 여기에 닿으면 스피너를 끄고 `GET /incidents/{id}`를 재조회한다(§4.7).
 * `ROLLED_BACK`·`ROLLBACK_FAILED`·`UNVERIFIED`는 **원본 Execution에만** 기록되고 롤백 자식은
 * SUCCESS·FAILED만 쓴다. `UNVERIFIED`는 서버가 자동 판정을 멈춘 자리라 최종이다(#249).
 */
export const TERMINAL_STATUSES: readonly ExecutionStatus[] = [
  'SUCCESS',
  'FAILED',
  'ROLLED_BACK',
  'ROLLBACK_FAILED',
  'UNVERIFIED',
];

export function isTerminalStatus(status: ExecutionStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}
