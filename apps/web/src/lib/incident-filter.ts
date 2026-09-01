// INC-001 보안 / INC-004 자산 목록 필터 — 화면설계서 v1.6 §4.4.
// 프리셋 판정과 필터 잔존 클램프가 여기 있습니다.

// 같은 디렉터리 상대 경로를 쓴다 — `node --test`는 `@/` 별칭을 해석하지 못한다(타입 전용
// import는 스트리핑돼 사라지므로 `@/types/api`는 그대로 둔다).
import { sortByRisk } from './incident-sort.ts';
import { INCIDENT_STATUSES, RISK_LEVELS } from '../types/api.ts';
import type { IncidentListItem, IncidentStatus, ResponseMode, RiskLevel } from '@/types/api';

export const ALL = '전체';

/**
 * 프리셋 4종 (§4.4). **`전체` 칸은 없다** — 프리셋 미선택(`ACTIVE`)이 곧 진행 중 전량이다.
 * `PREEMPTIVE`는 보안 화면에만 나온다(FINOPS에는 `response_mode`가 없다).
 */
export const INCIDENT_PRESETS = ['ACTIVE', 'PENDING', 'PREEMPTIVE', 'HISTORY'] as const;
export type IncidentPreset = (typeof INCIDENT_PRESETS)[number];

/**
 * 진행 중 = 종료(`RESOLVED`)가 아닌 5종. `FAILED`는 사람 개입이 남아 여기 잔류하고(§4.4),
 * `AWAITING_CLOSURE`도 같은 이유로 남는다 — 관제자 종료 판단이 아직 끝나지 않았으므로
 * 기본 목록에서 빠지면 조치에 성공한 건이 화면에서 통째로 사라진다 (#240).
 */
export const ACTIVE_STATUSES = [
  'ANALYZING',
  'AWAITING_APPROVAL',
  'ACTION_IN_PROGRESS',
  'AWAITING_CLOSURE',
  'FAILED',
] as const satisfies readonly IncidentStatus[];

/**
 * 선제차단 계열 — **승인 없이 이미 격리가 수행된** 두 `response_mode`다.
 * 묶는 근거는 §3.2.3의 `action.mode = PREEMPTIVE_EXECUTED` 유도 규칙과 같다.
 * `AGENT_WAIT`은 아직 실행 전이라 들어가지 않는다.
 */
export const PREEMPTIVE_MODES = [
  'PRE_MITIGATION_0_5S',
  'TIMEOUT_ISOLATION_1M',
] as const satisfies readonly ResponseMode[];

export function isPreemptive(incident: IncidentListItem): boolean {
  return (
    incident.response_mode !== null &&
    (PREEMPTIVE_MODES as readonly string[]).includes(incident.response_mode)
  );
}

/**
 * **상태 셀렉트에 쓴다.** 프리셋 전환은 **목록을 통째로 갈아끼운다.** 뷰는 같은 라우트·같은 위치라
 * searchParams만 바뀌는 soft navigation에서 `useState`가 살아남는다 — 이전 목록에서 고른 상태
 * 필터가 새 목록에 없는 값이면 0건이 되는데 **셀렉트는 `전체`라고 말한다**(PR #171 리뷰).
 *
 * 그래서 지금 목록에 없는 값은 `전체`로 접는다. 셀렉트 표시값도 이 결과를 쓰므로 화면과 필터가
 * 어긋나지 않고, 되돌아가면 원래 고른 값이 다시 유효해져 사용자 의도가 유지된다.
 */
export function clampOption(value: string, options: readonly string[]): string {
  return value === ALL || options.includes(value) ? value : ALL;
}

/**
 * 셀렉트 옵션은 **프리셋이 이미 거른 뒤의 목록**에 실제로 있는 값만 올린다 — 계약 전체 enum을
 * 늘어놓으면 0건 옵션이 섞이고, 프리셋 전 목록으로 세면 지금 보이지 않는 상태가 옵션에 남는다.
 * 다만 순서는 **계약 상수 순서**로 세운다. 응답 순서를 그대로 쓰면 데이터가 바뀔 때마다
 * 셀렉트 순서가 흔들린다(PR #171 리뷰).
 */
export function statusOptionsOf(items: readonly IncidentListItem[]): IncidentStatus[] {
  const present = new Set(items.map((i) => i.status));
  return INCIDENT_STATUSES.filter(
    // `승인 대기`는 **프리셋이 담당한다** — 셀렉트에도 두면 같은 걸 두 곳에서 걸게 되고,
    // 프리셋으로 건 것과 셀렉트로 건 것이 어긋나면 어느 쪽이 지금 걸린 건지 알 수 없다.
    // 왼쪽(무엇을 볼지)과 오른쪽(어떻게 거를지)을 가른 툴바 규칙과도 맞는다(§4.4).
    (s) => s !== 'AWAITING_APPROVAL' && present.has(s),
  );
}

/**
 * 위험도 셀렉트 — **`initial_risk_level` 기준**이다. 정렬 축과 같은 불변 키라(§4.4 정렬)
 * 걸러도 순서가 튀지 않고, `reviewed_risk_level`로 걸면 AI 정밀평가가 바뀔 때마다
 * 목록에서 항목이 사라진다.
 *
 * FINOPS는 계약이 두 위험도를 `null`로 강제해 옵션이 비고, 호출부가 셀렉트를 감춘다.
 */
export function riskOptionsOf(items: readonly IncidentListItem[]): RiskLevel[] {
  const present = new Set(items.map((i) => i.initial_risk_level));
  return RISK_LEVELS.filter((r) => present.has(r));
}

/**
 * 프리셋이 거르는 몫. **서버가 이미 거른 프리셋에서도 한 번 더 건다** — 멱등이라 결과가 같고,
 * WS로 들어온 건이 목록에 병합될 때(§4.4 병합 규칙) 서버 필터를 통과하지 않은 항목이 섞이는
 * 것을 여기서 막는다.
 */
export function byPreset(
  items: readonly IncidentListItem[],
  preset: IncidentPreset,
): IncidentListItem[] {
  const active = items.filter((i) =>
    (ACTIVE_STATUSES as readonly string[]).includes(i.status),
  );
  switch (preset) {
    case 'HISTORY':
      return items.filter((i) => i.status === 'RESOLVED');
    case 'PENDING':
      // **선제차단된 건은 여기 담지 않는다** — 두 프리셋을 배타로 둔다.
      //
      // 계약상 겹치는 것은 합법이다(`AWAITING_APPROVAL`은 "제안 1개 이상 + 진행 중 실행 없음"만
      // 요구하므로, 격리가 끝난 뒤 추가 조치가 승인을 기다릴 수 있다). 그러나 §4.4가 두 프리셋을
      // 세운 근거는 **"성격이 반대"** — 승인 대기는 *지금 누를 일*, 선제차단은 *이미 끝난 일의
      // 정당성 판단*이다. 한 카드가 양쪽에 뜨면 어느 큐가 그 건을 책임지는지 알 수 없다.
      //
      // 일이 사라지지는 않는다 — 선제차단 큐에서 열면 격리 사실과 남은 제안을 함께 보고,
      // 기본(프리셋 없음) 목록에도 그대로 있다. 카드의 상태 배지도 `승인 대기`로 남는다.
      return active.filter((i) => i.status === 'AWAITING_APPROVAL' && !isPreemptive(i));
    case 'PREEMPTIVE':
      return active.filter(isPreemptive);
    case 'ACTIVE':
      return active;
  }
}

/** 프리셋 → 상태 필터 순으로 걸고 정렬한다. 정렬은 DSH-001과 공유하는 셀렉터 하나뿐이다(§4.4). */
export function visibleIncidents(
  items: readonly IncidentListItem[],
  preset: IncidentPreset,
  status: string,
  risk: string = ALL,
): IncidentListItem[] {
  const inPreset = byPreset(items, preset);
  // 두 셀렉트 모두 접는다 — 한쪽만 접으면 다른 쪽으로 같은 버그가 남는다(PR #180 리뷰).
  const effectiveStatus = clampOption(status, statusOptionsOf(inPreset));
  const effectiveRisk = clampOption(risk, riskOptionsOf(inPreset));
  const filtered = inPreset.filter(
    (i) =>
      (effectiveStatus === ALL || i.status === (effectiveStatus as IncidentStatus)) &&
      (effectiveRisk === ALL || i.initial_risk_level === (effectiveRisk as RiskLevel)),
  );
  // FINOPS만 담긴 목록에서는 위험도가 전부 null이라 이 정렬이 곧 `created_at` 오름차순이다
  // — v1.6이 자산 화면에 정한 정렬과 같아서 함수를 가르지 않는다(§4.4 정렬).
  return sortByRisk(filtered);
}

/** URL 프리셋 키 ↔ 내부 값. 소문자 kebab을 쓰는 건 주소창에 그대로 노출되기 때문이다. */
const PRESET_BY_SLUG: Record<string, IncidentPreset> = {
  pending: 'PENDING',
  preemptive: 'PREEMPTIVE',
  history: 'HISTORY',
};

export const PRESET_SLUG: Record<IncidentPreset, string | null> = {
  ACTIVE: null, // 기본 — `전체` 칸이 없으므로 쿼리도 없다
  PENDING: 'pending',
  PREEMPTIVE: 'preemptive',
  HISTORY: 'history',
};

/**
 * 모르는 값은 **기본(ACTIVE)으로 접는다.** 구 `?status=` 패스스루와 달리 프리셋은 화면이 정의한
 * 값이라 서버에 그대로 넘길 대상이 아니고, 422를 띄울 계약도 아니다.
 * 배열(`?preset=a&preset=b`)도 같은 이유로 기본으로 떨어진다.
 */
export function parsePreset(value: string | string[] | undefined): IncidentPreset {
  return (typeof value === 'string' ? PRESET_BY_SLUG[value] : undefined) ?? 'ACTIVE';
}

/**
 * 프리셋이 **서버 필터로 표현되는지**. 계약의 `?status=`는 값 하나만 받으므로 진행 중 4종은
 * 서버에서 못 거른다 — `ACTIVE`·`PREEMPTIVE`는 전량을 받아 클라이언트가 거른다(§4.4).
 */
export function presetServerStatus(preset: IncidentPreset): IncidentStatus | undefined {
  if (preset === 'PENDING') return 'AWAITING_APPROVAL';
  if (preset === 'HISTORY') return 'RESOLVED';
  return undefined;
}
