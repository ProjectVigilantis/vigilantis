// 위협 이벤트 목록(2026-11-03 초안): 고객사 대시보드의 "위협 이벤트" 칸이 이벤트 하나하나의 발생 시각을 보여 주는 자리다.
//
// 서버에 이 API가 아직 없다. 이벤트는 DB `threat_events` 테이블(apps/core-api/db/models.py `ThreatEvent`)에 쌓이지만,
// 공개된 것은 시간 칸별 건수 축(`GET /metrics/timeseries`의 `threat_events`, 최대 14일)뿐이다.
// 아래 `ThreatEventsResponse`는 FE가 제안하는 계약이며 목업(fe-mock)만 응답한다. 필드는 정규화된 이벤트
// (packages/schemas/events.py `NormalizedThreatEvent`)에서 화면에 필요한 것만 골랐다. 실서버는 404를 돌려주고 화면은 그 사실을 적는다.
// BE가 계약을 정하면 타입을 types/api.ts로 옮기고 이 머리말을 고친다.

import type { ThreatEventKind } from '../types/api.ts';

type IsoDateTime = string;

/** 조회 기간. 하루 · 일주일 · 한 달(30일). */
export const THREAT_RANGES = [
  { days: 1, label: '하루' },
  { days: 7, label: '일주일' },
  { days: 30, label: '한 달' },
] as const;
export type ThreatRangeDays = (typeof THREAT_RANGES)[number]['days'];

export interface OpenIpPayload {
  protocol: string;
  from_port: number | null;
  to_port: number | null;
  source_cidr: string;
}

export interface SshBruteForcePayload {
  source_ip: string;
  failed_attempt_count: number;
  window_seconds: number;
}

export type ThreatEventItem =
  | {
      threat_event_id: string;
      event_type: 'OPEN_IP';
      target_arn: string;
      occurred_at: IsoDateTime;
      payload: OpenIpPayload;
    }
  | {
      threat_event_id: string;
      event_type: 'SSH_BRUTE_FORCE';
      target_arn: string;
      occurred_at: IsoDateTime;
      payload: SshBruteForcePayload;
    };

/** `GET /api/v1/threat-events?days=1|7|30` — 범위 안 이벤트를 최근 것부터. 서버가 고객사 범위로 거른다. */
export interface ThreatEventsResponse {
  window_start: IsoDateTime;
  window_end: IsoDateTime;
  /** 기간 안 전체 건수. `items`가 상한에 걸려 잘렸으면 `items.length`보다 크다. */
  total: number;
  items: ThreatEventItem[];
}

export const THREAT_KIND_LABELS: Record<ThreatEventKind, string> = {
  SSH_BRUTE_FORCE: 'SSH 무차별 대입',
  OPEN_IP: '전체 개방 포트',
};

/** 출발지: SSH는 공격 IP, 개방 포트는 허용 대역. 둘은 다른 사실이라 화면 문구도 가른다. */
export function threatSource(item: ThreatEventItem): { label: string; value: string } {
  return item.event_type === 'SSH_BRUTE_FORCE'
    ? { label: '출발지', value: item.payload.source_ip }
    : { label: '허용 대역', value: item.payload.source_cidr };
}

/** 유형별 한 줄 세부. 실패 횟수 · 열린 포트. */
export function threatDetail(item: ThreatEventItem): string {
  if (item.event_type === 'SSH_BRUTE_FORCE') {
    const minutes = Math.max(1, Math.round(item.payload.window_seconds / 60));
    return `${minutes}분 동안 로그인 실패 ${item.payload.failed_attempt_count}회`;
  }
  const { protocol, from_port, to_port } = item.payload;
  const port = from_port === null ? '전체 포트' : from_port === to_port ? `${from_port}` : `${from_port}-${to_port}`;
  return `${protocol.toUpperCase()} ${port}`;
}

/** 기간 안에서 이벤트의 가로 위치(0-100%). 기간 밖은 양 끝에 붙인다. */
export function timelinePercent(at: IsoDateTime, windowStart: IsoDateTime, windowEnd: IsoDateTime): number {
  const start = Date.parse(windowStart);
  const span = Date.parse(windowEnd) - start;
  if (span <= 0) return 100;
  return Math.min(100, Math.max(0, ((Date.parse(at) - start) / span) * 100));
}

/** 유형별 건수(받은 `items` 기준). */
export function threatKindCounts(items: readonly ThreatEventItem[]): Record<ThreatEventKind, number> {
  const counts: Record<ThreatEventKind, number> = { SSH_BRUTE_FORCE: 0, OPEN_IP: 0 };
  for (const i of items) counts[i.event_type] += 1;
  return counts;
}
