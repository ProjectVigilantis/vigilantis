// 위협 이벤트 목록(2026-11-03 초안 · #572 10번 계약): 고객사 대시보드의 "위협 이벤트" 칸과 로그 화면이 이벤트 하나하나의
// 발생 시각을 보여 주는 자리다.
//
// 계약(`ThreatEventsResponse` · `ThreatEventItem` · `ThreatRangeDays`)은 types/api.ts에 있다(packages/schemas/api/threat_events.py).
// 원천은 DB `threat_events`이고, 범위 · 관제 리전 · 기간 조건은 메트릭 축 6(`GET /metrics/timeseries`의 `threat_events`)과
// 같다. 최근 것부터 최대 500건이며 넘치면 `total`이 실제 건수를 알린다. 이 파일은 화면 계산만 둔다.

import type {
  IsoDateTime,
  OpenIpPayload,
  SshBruteForcePayload,
  ThreatEventItem,
  ThreatEventKind,
  ThreatEventsResponse,
  ThreatRangeDays,
} from '../types/api.ts';

export type { OpenIpPayload, SshBruteForcePayload, ThreatEventItem, ThreatEventsResponse, ThreatRangeDays };

/** 조회 기간. 하루 · 일주일 · 한 달(30일). */
export const THREAT_RANGES: readonly { days: ThreatRangeDays; label: string }[] = [
  { days: 1, label: '하루' },
  { days: 7, label: '일주일' },
  { days: 30, label: '한 달' },
];

export const THREAT_KIND_LABELS: Record<ThreatEventKind, string> = {
  SSH_BRUTE_FORCE: 'SSH 무차별 대입',
  OPEN_IP: '전체 개방 포트',
  GUARDDUTY_FINDING: 'GuardDuty 탐지',
};

/**
 * 출발지: SSH는 공격 IP, 개방 포트는 허용 대역. 둘은 다른 사실이라 화면 문구도 가른다.
 * GuardDuty 탐지는 출발지 칸이 없어 탐지 유형을 둔다(#588 C).
 */
export function threatSource(item: ThreatEventItem): { label: string; value: string } {
  if (item.event_type === 'GUARDDUTY_FINDING') return { label: '탐지 유형', value: item.payload.finding_type };
  return item.event_type === 'SSH_BRUTE_FORCE'
    ? { label: '출발지', value: item.payload.source_ip }
    : { label: '허용 대역', value: item.payload.source_cidr };
}

/** 집계 구간. 분으로 나누어떨어지면 분, 아니면 초로 적는다(30초를 "1분"으로 올리지 않는다). */
function windowLabel(seconds: number): string {
  return seconds >= 60 && seconds % 60 === 0 ? `${seconds / 60}분` : `${seconds}초`;
}

/** 유형별 한 줄 세부. 실패 횟수 · 열린 포트 · GuardDuty 심각도. */
export function threatDetail(item: ThreatEventItem): string {
  if (item.event_type === 'GUARDDUTY_FINDING') {
    return `심각도 ${item.payload.severity.toFixed(1)} · ${item.payload.resource_type}`;
  }
  if (item.event_type === 'SSH_BRUTE_FORCE') {
    return `${windowLabel(item.payload.window_seconds)} 동안 로그인 실패 ${item.payload.failed_attempt_count}회`;
  }
  // 계약은 두 끝을 따로 null로 허용한다. 한쪽만 있으면 있는 값을 단일 포트로 적는다("22-null"로 적지 않는다).
  const { protocol, from_port, to_port } = item.payload;
  const lo = from_port ?? to_port;
  const hi = to_port ?? from_port;
  const port = lo === null || hi === null ? '전체 포트' : lo === hi ? `${lo}` : `${lo}-${hi}`;
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
  const counts: Record<ThreatEventKind, number> = { SSH_BRUTE_FORCE: 0, OPEN_IP: 0, GUARDDUTY_FINDING: 0 };
  for (const i of items) counts[i.event_type] += 1;
  return counts;
}
