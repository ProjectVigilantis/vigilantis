// 플랫폼 상태(2026-11-02 초안): 중앙 관리계정이 Vigilantis 자체가 돌고 있는지 보는 자리다.
//
// 서버에 이 API가 아직 없다. 지금 있는 것은 생존 확인 `/health` 하나뿐이고 DB도 보지 않는다(apps/core-api/main.py).
// 아래 `PlatformStatusResponse`는 FE가 제안하는 계약이며 목업(fe-mock)만 응답한다. 실서버는 404를 돌려주고, 화면은 그때
// "API 없음"을 적는다. 구성 요소 이름은 서버에 실제로 있는 백그라운드 작업에서 골랐다:
//   수집 스케줄러(services/scheduler.py, SCAN_INTERVAL_SECONDS 기본 300초) · AI 분석 디스패처(DISPATCH_INTERVAL_SECONDS) ·
//   조치 에이전트(AGENT_DISPATCH_INTERVAL_SECONDS) · 알림 발송(NOTIFICATION_*) · 실시간 연결(routers/ws.py) · DB · OpenAI.
// BE가 계약을 정하면 타입을 types/api.ts로 옮기고 이 머리말을 고친다.

import { hoursSince } from './tenants.ts';

type IsoDateTime = string;

export type PlatformComponentKey =
  | 'API'
  | 'DATABASE'
  | 'SCAN_SCHEDULER'
  | 'AI_DISPATCHER'
  | 'AGENT_DISPATCHER'
  | 'NOTIFIER'
  | 'REALTIME'
  | 'OPENAI';

/** `DISABLED`는 설정으로 꺼 둔 것(예: SCAN_ENABLED=false)이라 장애로 세지 않는다. */
export type PlatformComponentState = 'OK' | 'DEGRADED' | 'DOWN' | 'DISABLED';

export interface PlatformComponent {
  key: PlatformComponentKey;
  state: PlatformComponentState;
  /** 마지막으로 정상 동작한 시각. 한 번도 없으면 null. */
  last_success_at: IsoDateTime | null;
  /** 상태가 OK가 아닐 때의 짧은 사유(오류 코드 원문 등). */
  detail: string | null;
}

export interface ScanCycle {
  started_at: IsoDateTime;
  duration_seconds: number;
  tenants_total: number;
  /** 이 주기에서 수집 · 판정에 실패한 고객사 수. */
  tenants_failed: number;
}

export interface HourlyCount {
  at: IsoDateTime;
  requests: number;
  failures: number;
}

export interface PlatformStatusResponse {
  generated_at: IsoDateTime;
  components: PlatformComponent[];
  /** 최근 수집 주기. 오래된 것부터. */
  scan: { interval_seconds: number; cycles: ScanCycle[] };
  /** AI 분석(OpenAI 호출) 최근 `window_hours`시간. `hourly`는 오래된 것부터. */
  ai: {
    window_hours: number;
    requests: number;
    failures: number;
    p95_latency_ms: number | null;
    guardrail_rejections: number;
    hourly: HourlyCount[];
  };
  notifications: { pending: number; failed_24h: number; oldest_pending_at: IsoDateTime | null };
  realtime: { connections: number };
  /** 고객사별 API 호출(2026-11-03 초안). 서버 접근 로그(main.py request_context)에는 지금 user_id · role만 있고
   * tenant_id가 없어, 서버가 이 값을 내려면 접근 로그에 tenant_id를 싣고 시간 칸별로 세는 자리가 먼저 필요하다. */
  api_usage: ApiUsage;
}

export interface TenantApiUsage {
  tenant_id: string;
  tenant_name: string;
  /** `ApiUsage.hours`와 같은 칸 순서의 시간별 호출 건수. */
  hourly: number[];
}

export interface ApiUsage {
  window_hours: number;
  /** 시간 칸의 시작 시각. 오래된 것부터. */
  hours: IsoDateTime[];
  /** 고객사 한 곳이 한 시간에 이 건수를 넘으면 과다 호출로 본다. 서버 설정값이며 정하지 않았으면 null. */
  threshold_per_hour: number | null;
  /** 창 안 호출 합계가 많은 순 상위 고객사. 과다 호출이 있었던 고객사는 순위 밖이어도 싣는다. */
  tenants: TenantApiUsage[];
  /** `tenants`에 없는 나머지 고객사 수. */
  other_tenants: number;
}

export interface OverCallTenant {
  tenant_id: string;
  tenant_name: string;
  /** 가장 많이 부른 시간 칸의 시작 시각과 건수. */
  peakAt: IsoDateTime;
  peakRequests: number;
  /** 기준을 넘은 시간 칸 수와 그중 가장 최근 칸의 시작 시각. */
  hoursOver: number;
  lastOverAt: IsoDateTime;
}

/** 기준을 한 시간이라도 넘은 고객사. 최고 건수가 큰 순. 기준이 없으면 빈 목록이다. */
export function overCallTenants(usage: ApiUsage): OverCallTenant[] {
  const limit = usage.threshold_per_hour;
  if (limit === null) return [];
  const out: OverCallTenant[] = [];
  for (const t of usage.tenants) {
    let peak = -1;
    let peakIdx = -1;
    let hoursOver = 0;
    let lastOverIdx = -1;
    t.hourly.forEach((n, i) => {
      if (i >= usage.hours.length) return;
      if (n > peak) {
        peak = n;
        peakIdx = i;
      }
      if (n > limit) {
        hoursOver += 1;
        lastOverIdx = i;
      }
    });
    if (hoursOver === 0) continue;
    out.push({
      tenant_id: t.tenant_id,
      tenant_name: t.tenant_name,
      peakAt: usage.hours[peakIdx],
      peakRequests: peak,
      hoursOver,
      lastOverAt: usage.hours[lastOverIdx],
    });
  }
  return out.sort((a, b) => b.peakRequests - a.peakRequests);
}

/**
 * 담당 고객사만 남긴다. 중앙 관리계정이 아닌 계정(고객 담당자)은 배정된 고객사 이름 · 건수만 봐야 한다.
 * `allowedIds`는 서버가 이 계정에 돌려준 고객사 목록(GET /tenants)이다. 서버가 api_usage를 담당 범위로 거르면 이 함수는 할 일이 없다.
 */
export function scopeApiUsage(usage: ApiUsage, allowedIds: readonly string[]): ApiUsage {
  const allowed = new Set(allowedIds.map((id) => id.toLowerCase()));
  const tenants = usage.tenants.filter((t) => allowed.has(t.tenant_id.toLowerCase()));
  return { ...usage, tenants, other_tenants: Math.max(0, allowed.size - tenants.length) };
}

/** 히트맵 칸 진하기 단계(0-4). 0은 호출 없음, 4는 표에서 가장 큰 값 근처다. */
export function usageLevel(requests: number, max: number): 0 | 1 | 2 | 3 | 4 {
  if (requests <= 0 || max <= 0) return 0;
  return Math.min(4, Math.max(1, Math.ceil((requests / max) * 4))) as 1 | 2 | 3 | 4;
}

export const COMPONENT_ORDER: readonly PlatformComponentKey[] = [
  'API',
  'DATABASE',
  'SCAN_SCHEDULER',
  'AI_DISPATCHER',
  'AGENT_DISPATCHER',
  'NOTIFIER',
  'REALTIME',
  'OPENAI',
];

export const COMPONENT_LABELS: Record<PlatformComponentKey, { label: string; note: string }> = {
  API: { label: 'API 서버', note: '화면 · 고객사 요청' },
  DATABASE: { label: 'DB', note: 'PostgreSQL' },
  SCAN_SCHEDULER: { label: '수집 스케줄러', note: '주기 수집 · 판정' },
  AI_DISPATCHER: { label: 'AI 분석', note: '인시던트 분석 배분' },
  AGENT_DISPATCHER: { label: '조치 에이전트', note: '승인 · 자동 조치 실행' },
  NOTIFIER: { label: '알림 발송', note: '메일 · 알림' },
  REALTIME: { label: '실시간 연결', note: 'WebSocket' },
  OPENAI: { label: 'OpenAI', note: '외부 AI API' },
};

export const STATE_LABELS: Record<PlatformComponentState, { label: string; dot: string }> = {
  OK: { label: '정상', dot: 'bg-status-good' },
  DEGRADED: { label: '저하', dot: 'bg-status-warning' },
  DOWN: { label: '중단', dot: 'bg-status-critical' },
  DISABLED: { label: '꺼 둠', dot: 'bg-status-idle' },
};

const SEVERITY: Record<PlatformComponentState, number> = { DISABLED: 0, OK: 1, DEGRADED: 2, DOWN: 3 };

/** 전체 상태: 가장 나쁜 구성 요소를 따른다. 꺼 둔 것은 세지 않는다. 구성 요소가 없으면 OK다. */
export function overallState(components: readonly PlatformComponent[]): Exclude<PlatformComponentState, 'DISABLED'> {
  const worst = components.reduce<PlatformComponentState>((w, c) => (SEVERITY[c.state] > SEVERITY[w] ? c.state : w), 'OK');
  return worst === 'DISABLED' ? 'OK' : worst;
}

/** 구성 요소를 정해진 순서로 편다. 응답에 없는 것은 빼고, 모르는 키는 뒤에 붙인다. */
export function orderedComponents(components: readonly PlatformComponent[]): PlatformComponent[] {
  const rank = (k: string) => {
    const i = COMPONENT_ORDER.indexOf(k as PlatformComponentKey);
    return i === -1 ? COMPONENT_ORDER.length : i;
  };
  return [...components].sort((a, b) => rank(a.key) - rank(b.key));
}

export interface ScanSummary {
  /** 마지막 주기가 시작된 지 몇 분. 주기가 없으면 null. */
  lastAgeMinutes: number | null;
  /** 주기 두 번 이상 새 수집이 없으면 늦음이다. */
  late: boolean;
  avgDurationSeconds: number | null;
  /** 마지막 주기에서 실패한 고객사 수. */
  lastFailed: number;
  /** 실패가 한 곳이라도 있었던 주기 수. */
  cyclesWithFailure: number;
}

export function scanSummary(scan: PlatformStatusResponse['scan'], nowMs: number): ScanSummary {
  const { cycles, interval_seconds } = scan;
  const last = cycles.at(-1) ?? null;
  const lastAgeMinutes = last === null ? null : Math.round(hoursSince(last.started_at, nowMs) * 60);
  return {
    lastAgeMinutes,
    late: lastAgeMinutes === null || lastAgeMinutes * 60 > interval_seconds * 2,
    avgDurationSeconds:
      cycles.length === 0 ? null : Math.round(cycles.reduce((s, c) => s + c.duration_seconds, 0) / cycles.length),
    lastFailed: last?.tenants_failed ?? 0,
    cyclesWithFailure: cycles.filter((c) => c.tenants_failed > 0).length,
  };
}

/** 실패율(%) — 소수 첫째 자리. 요청이 없으면 null. */
export function failureRate(requests: number, failures: number): number | null {
  return requests === 0 ? null : Math.round((failures / requests) * 1000) / 10;
}
