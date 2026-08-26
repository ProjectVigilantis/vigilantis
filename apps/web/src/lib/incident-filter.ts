// INC-001 목록 필터 — 화면설계서 v1.5 §4.4. 프리셋 전환 시 필터 잔존을 막는 클램프가 여기 있습니다.

// 같은 디렉터리 상대 경로를 쓴다 — `node --test`는 `@/` 별칭을 해석하지 못한다(타입 전용
// import는 스트리핑돼 사라지므로 `@/types/api`는 그대로 둔다).
import { sortByRisk } from './incident-sort.ts';
import { INCIDENT_CATEGORIES, INCIDENT_STATUSES } from '../types/api.ts';
import type { IncidentCategory, IncidentListItem, IncidentStatus } from '@/types/api';

export const ALL = '전체';

/**
 * 프리셋 전환은 **목록을 통째로 갈아끼운다.** `IncidentsView`는 같은 라우트·같은 위치라
 * searchParams만 바뀌는 soft navigation에서 `useState`가 살아남는다 — 이전 목록에서 고른 상태
 * 필터가 새 목록에 없는 값이면 0건이 되는데 **셀렉트는 `전체`라고 말한다**(PR #171 리뷰).
 *
 * 그래서 지금 목록에 없는 값은 `전체`로 접는다. 셀렉트 표시값도 이 결과를 쓰므로 화면과 필터가
 * 어긋나지 않고, 되돌아가면 원래 고른 값이 다시 유효해져 사용자 의도가 유지된다.
 */
export function clampStatus(status: string, options: readonly IncidentStatus[]): string {
  return status === ALL || options.includes(status as IncidentStatus) ? status : ALL;
}

/**
 * 셀렉트 옵션은 **응답에 실제로 있는 값만** 올린다 — 계약 전체 enum을 늘어놓으면 0건 옵션이 섞인다.
 * 다만 순서는 **계약 상수 순서**로 세운다. 응답 순서를 그대로 쓰면 데이터가 바뀔 때마다
 * 셀렉트 순서가 흔들린다(PR #171 리뷰).
 */
export function statusOptionsOf(items: readonly IncidentListItem[]): IncidentStatus[] {
  const present = new Set(items.map((i) => i.status));
  return INCIDENT_STATUSES.filter((s) => present.has(s));
}

/** 유형 셀렉트도 상태와 같은 규칙을 쓴다 — 한 화면에서 규칙이 갈리지 않게(PR #171 리뷰). */
export function categoryOptionsOf(items: readonly IncidentListItem[]): IncidentCategory[] {
  const present = new Set(items.map((i) => i.category));
  return INCIDENT_CATEGORIES.filter((c) => present.has(c));
}

/** 필터를 걸고 위험도순으로 세운다. 정렬은 DSH-001과 공유하는 셀렉터 하나뿐이다(§4.4). */
export function visibleIncidents(
  items: readonly IncidentListItem[],
  category: string,
  status: string,
): IncidentListItem[] {
  const effectiveStatus = clampStatus(status, statusOptionsOf(items));
  const filtered = items.filter(
    (i) =>
      (category === ALL || i.category === (category as IncidentCategory)) &&
      (effectiveStatus === ALL || i.status === (effectiveStatus as IncidentStatus)),
  );
  return sortByRisk(filtered);
}
