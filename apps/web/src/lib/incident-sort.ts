// 인시던트 위험도 정렬 — 화면설계서 v1.5 §4.4. INC-001 두 프리셋과 DSH-001 AI 조치 카드가
// 이 함수 하나를 공유합니다(대시보드와 목록이 서로 다른 1순위를 보이면 어느 쪽을 믿을지 알 수 없다).

import type { IncidentListItem, RiskLevel } from '@/types/api';

const RISK_RANK: Record<RiskLevel, number> = { HIGH: 0, MEDIUM: 1, LOW: 2 };

/** null은 FinOps다 — 계약이 두 위험도를 null로 강제하므로 자연히 맨 아래로 간다(§4.4). */
function rank(level: RiskLevel | null): number {
  return level === null ? 3 : RISK_RANK[level];
}

/**
 * 정렬 키는 `initial_risk_level` **하나이며 불변 키**다(통합설계 4.4 "초기 위험 판정기의 불변 결과").
 * `reviewed_risk_level`·`response_mode`로 정렬하면 정밀평가·타임아웃 전환마다 목록이 눈앞에서
 * 재배치돼 누르려던 항목이 움직인다. 불변 키라서 재정렬해도 순서가 튀지 않는다.
 *
 * 동점은 `created_at` 오름차순 — 오래 기다린 건이 먼저다(구 APR-001의 대기 시간 개념).
 * 계약의 시각은 UTC ISO 8601이라 문자열 비교가 곧 시간 비교다.
 */
export function byRisk(a: IncidentListItem, b: IncidentListItem): number {
  const diff = rank(a.initial_risk_level) - rank(b.initial_risk_level);
  return diff !== 0 ? diff : a.created_at.localeCompare(b.created_at);
}

export function sortByRisk(items: readonly IncidentListItem[]): IncidentListItem[] {
  return [...items].sort(byRisk);
}
