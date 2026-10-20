// 고객사 대시보드(미리보기 `/preview/dashboard`)의 집계. 전부 기존 계약(assets · incidents · metrics)에서 계산한다.
// 첫 화면은 "지금 괜찮은가 → 무엇을 해야 하나 → 흐름은 어떤가" 순서로 읽히게 만든다(DESIGN.md §레이아웃).

import type { ProposalRow } from './dashboard.ts';
import type { CollectionStatus, IncidentListItem, IncidentStatus, ThreatEventAxis } from '../types/api.ts';

export type Posture = 'critical' | 'warning' | 'good' | 'unknown';

export interface PostureSummary {
  level: Posture;
  /** 첫 줄. 숫자가 있으면 숫자를 넣어 한 문장으로 끝낸다. */
  headline: string;
  detail: string;
}

/**
 * 맨 위 상태 한 줄. 보안 건이 기준이다. 비용 건은 급하지 않아 상태를 올리지 않고 할 일 목록에만 선다.
 * 인시던트를 못 읽었으면 "정상"이라 말하지 않는다(모름과 0건을 가른다).
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

/** "18분 전" 같은 상대 시각. 화면은 절대 시각을 title로 함께 단다. */
export function timeAgo(iso: string, nowMs: number): string {
  const minutes = Math.max(0, Math.round((nowMs - Date.parse(iso)) / 60_000));
  if (minutes < 1) return '방금';
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  return `${Math.floor(hours / 24)}일 전`;
}
