// 고객사 대시보드(미리보기 `/preview/dashboard`)의 집계. 전부 기존 계약(assets · incidents · metrics)에서 계산한다.
// 첫 화면은 "지금 괜찮은가 → 무엇을 해야 하나 → 흐름은 어떤가" 순서로 읽히게 만든다(DESIGN.md §레이아웃).

import type { ProposalRow } from './dashboard.ts';
import { INCIDENT_STATUS_LABELS } from './enum-labels.ts';
import type {
  AssetItem,
  CollectionStatus,
  IncidentListItem,
  IncidentStatus,
  ResponseMode,
  ThreatEventAxis,
} from '../types/api.ts';

export type Posture = 'critical' | 'warning' | 'good' | 'unknown';

export interface PostureSummary {
  level: Posture;
  /** 첫 줄. 숫자가 있으면 숫자를 넣어 한 문장으로 끝낸다. */
  headline: string;
  detail: string;
}

/**
 * 맨 위 상태 한 줄. 보안 건이 기준이다. 비용 건은 급하지 않아 상태를 올리지 않고 할 일 목록에만 선다.
 * 인시던트를 못 읽었으면 "정상"이라 말하지 않는다(모름과 0건을 가른다). 수집 전 · 수집 중도 같다.
 * 위협 0건은 아직 수집하지 못한 결과일 수 있다.
 */
export function postureOf(input: {
  queue: readonly IncidentListItem[] | null;
  threatAssets: number;
  collection: CollectionStatus;
}): PostureSummary {
  const { queue, threatAssets, collection } = input;
  if (queue === null) {
    return { level: 'unknown', headline: '인시던트를 불러오지 못했습니다', detail: '잠시 뒤 다시 확인하세요.' };
  }
  const secops = queue.filter((i) => i.category === 'SECOPS');
  const high = secops.filter((i) => i.initial_risk_level === 'HIGH').length;
  const waiting = queue.filter((i) => i.status === 'AWAITING_APPROVAL').length;
  if (high > 0) {
    return {
      level: 'critical',
      headline: `지금 확인할 위협이 ${high}건 있습니다`,
      detail: `위험도 높음 보안 건 ${high}건 · 승인을 기다리는 조치 ${waiting}건`,
    };
  }
  if (secops.length > 0 || threatAssets > 0) {
    return {
      level: 'warning',
      headline: `주의가 필요한 보안 항목이 ${Math.max(secops.length, threatAssets)}건 있습니다`,
      detail: `처리 중인 보안 건 ${secops.length}건 · 위협으로 판정된 자산 ${threatAssets}개`,
    };
  }
  if (collection === 'PARTIAL' || collection === 'FAILED') {
    return { level: 'warning', headline: '자산 수집이 일부 실패했습니다', detail: '보이지 않는 자산이 있을 수 있습니다.' };
  }
  if (collection === 'NOT_COLLECTED') {
    return { level: 'unknown', headline: '아직 자산을 수집하지 않았습니다', detail: '첫 수집이 끝나면 상태를 알려 드립니다.' };
  }
  if (collection === 'COLLECTING') {
    return { level: 'unknown', headline: '자산을 수집하는 중입니다', detail: '수집이 끝나면 상태를 다시 판단합니다.' };
  }
  return {
    level: 'good',
    headline: '모든 자산이 정상입니다',
    detail: waiting > 0 ? `비용 절감 제안 ${waiting}건이 승인을 기다립니다` : '처리할 보안 건이 없습니다',
  };
}

export interface SavingsTotal {
  /** 추정이 있는 제안의 월 절감액 합(USD). */
  amount: number;
  counted: number;
  /** 추정이 없는 비용 제안 수. 0달러로 세지 않는다. */
  unknown: number;
}

export function savingsTotal(rows: readonly ProposalRow[]): SavingsTotal {
  let amount = 0;
  let counted = 0;
  let unknown = 0;
  for (const row of rows) {
    if (row.incident.category !== 'FINOPS') continue;
    if (row.savings === null) unknown += 1;
    else {
      amount += row.savings;
      counted += 1;
    }
  }
  return { amount, counted, unknown };
}

export interface HourBucket {
  /** 칸 시작 시각(ISO, 정시). */
  at: string;
  total: number;
}

const HOUR = 3_600_000;

/**
 * 최근 24시간 위협 이벤트를 1시간 칸으로 편다. 이 축은 **점이 없는 칸이 0건**이다(계약 · ThreatEventAxis)
 * 이라 빈 칸을 0으로 채운다. 축을 못 읽었으면 null: 0건 그래프로 그리지 않는다.
 */
export function threatHours(axis: ThreatEventAxis, generatedAt: string, hours = 24): HourBucket[] | null {
  if (axis.status !== 'READY') return null;
  const end = Math.floor(Date.parse(generatedAt) / HOUR) * HOUR;
  const byHour = new Map<number, number>();
  for (const p of axis.points) {
    const slot = Math.floor(Date.parse(p.at) / HOUR) * HOUR;
    byHour.set(slot, (byHour.get(slot) ?? 0) + p.total);
  }
  const buckets: HourBucket[] = [];
  for (let k = hours - 1; k >= 0; k--) {
    const slot = end - k * HOUR;
    buckets.push({ at: new Date(slot).toISOString(), total: byHour.get(slot) ?? 0 });
  }
  return buckets;
}

const DONE_STATUSES: readonly IncidentStatus[] = ['RESOLVED', 'AWAITING_CLOSURE'];

/** 최근 끝난 건: 플랫폼이 쉬지 않고 일했다는 근거다. 최근 `days`일 안에 갱신된 것만, 최신순으로. */
export function recentDone(
  incidents: readonly IncidentListItem[] | null,
  nowMs: number,
  days = 7,
  limit = 5,
): IncidentListItem[] | null {
  if (incidents === null) return null;
  const since = nowMs - days * 24 * HOUR;
  return incidents
    .filter((i) => DONE_STATUSES.includes(i.status) && Date.parse(i.updated_at) >= since)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, limit);
}

const AUTO_ATTEMPT: Partial<Record<ResponseMode, string>> = {
  PRE_MITIGATION_0_5S: '선제 차단 시도',
  TIMEOUT_ISOLATION_1M: '자동 격리 시도',
};

/**
 * 끝난 건 한 줄. **조치가 성공했다고 말하지 않는다.** 목록 응답에는 실행 결과가 없고 `response_mode`는
 * 대응 경로일 뿐이다. 시간 초과 자동 격리가 가드레일에 거절된 뒤 관제자가 「추가 조치 없음」으로 닫은 건도
 * 같은 값을 갖는다(core-api tests/test_auto_isolation_workflow.py). 결과는 인시던트 상세의 실행 이력이 말한다.
 */
export function doneNote(incident: Pick<IncidentListItem, 'status' | 'response_mode'>): string {
  const status = INCIDENT_STATUS_LABELS[incident.status]?.label ?? incident.status;
  const attempt = incident.response_mode === null ? undefined : AUTO_ATTEMPT[incident.response_mode];
  return attempt === undefined ? status : `${status} · ${attempt}`;
}

export interface CoverageCounts {
  /** 판정 대상(`evaluation_status`가 NOT_APPLICABLE이 아닌 자산) 수. 아래 다섯 칸의 합이다. */
  judged: number;
  threat: number;
  /** 비용 후보(COST_CANDIDATE · UNUSED). */
  waste: number;
  /** 판정이 끝났고 위협 · 비용 후보가 아닌 것(SKIP). */
  ok: number;
  /** 아직 판정하지 않음(PENDING). */
  pending: number;
  /** 판정 실패(FAILED). */
  failed: number;
}

/**
 * 관제 범위 카드의 판정 결과. 판정 대기 · 실패는 `verdict`가 null이지만 정상이 아니다. `evaluation_status`로
 * 먼저 갈라 정상 · 제외에 섞지 않는다. 판정 완료(COMPLETED)는 `verdict`가 반드시 있다(packages/schemas/api/assets.py).
 */
export function coverageCounts(items: readonly Pick<AssetItem, 'evaluation_status' | 'verdict'>[]): CoverageCounts {
  const counts: CoverageCounts = { judged: 0, threat: 0, waste: 0, ok: 0, pending: 0, failed: 0 };
  for (const a of items) {
    if (a.evaluation_status === 'NOT_APPLICABLE') continue;
    counts.judged += 1;
    if (a.evaluation_status === 'PENDING') counts.pending += 1;
    else if (a.evaluation_status === 'FAILED') counts.failed += 1;
    else if (a.verdict === 'THREAT') counts.threat += 1;
    else if (a.verdict === 'COST_CANDIDATE' || a.verdict === 'UNUSED') counts.waste += 1;
    else counts.ok += 1;
  }
  return counts;
}

/** "18분 전" 같은 상대 시각. 화면은 절대 시각을 title로 함께 단다. */
export function timeAgo(iso: string, nowMs: number): string {
  const minutes = Math.max(0, Math.round((nowMs - Date.parse(iso)) / 60_000));
  if (minutes < 1) return '방금';
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  return `${Math.floor(hours / 24)}일 전`;
}
