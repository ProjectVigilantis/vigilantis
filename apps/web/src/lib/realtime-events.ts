// WS 이벤트 → 화면 동작 매핑 — 화면설계서 v1.5 §4.8. 전송(소켓)과 분리해 단위 검증이 가능합니다.

// 최종 상태 정의는 한 곳이다 — `node --test`가 별칭을 못 풀어 상대 경로를 쓴다(incident-filter와 같은 이유).
import { isTerminalStatus } from './execution-status.ts';
import type { ExecutionStatus, IsoDateTime, WsEvent } from '@/types/api';

/**
 * 이벤트 하나가 화면에 시키는 일. **판단은 여기서 끝나고 소켓은 배달만 한다.**
 *
 * `WS 이벤트는 알림이고 상태의 기준이 아니다`(§4.8 전달 원칙) — 그래서 인시던트 계열은
 * 값을 들고 오지 않고 **REST 재조회**를 시킨다. `EXECUTION_UPDATED`만 `status`가 `data`에
 * 직접 오므로(§4.8 표) ACT-002가 그 값을 바로 그린다.
 */
export type RealtimeAction =
  | { kind: 'refresh'; incidentId: string; toast: boolean }
  | {
      kind: 'execution';
      incidentId: string;
      executionId: string;
      status: ExecutionStatus;
      updatedAt: IsoDateTime;
      toast: boolean;
    };

/**
 * `INCIDENT_CREATED`·`INCIDENT_UPDATED`는 **SecOps 전용**이고 `EXECUTION_UPDATED`만 FinOps
 * 공통이다(§4.8). Toast 정책도 이벤트별로 다르다.
 *
 * - `INCIDENT_CREATED` — 항상 표시
 * - `INCIDENT_UPDATED` — Toast 없음(REST 재조회만). 상태 변화마다 띄우면 소음이 된다
 * - `EXECUTION_UPDATED` — **최종 상태에서만**
 */
export function actionFor(event: WsEvent): RealtimeAction {
  if (event.event_type === 'EXECUTION_UPDATED') {
    const { incident_id, execution_id, status, updated_at } = event.data;
    return {
      kind: 'execution',
      incidentId: incident_id,
      executionId: execution_id,
      status,
      updatedAt: updated_at,
      // Toast는 §4.7 최종 상태에서만 — 중간 전이마다 띄우면 한 실행이 여러 회 알림이 된다.
      toast: isTerminalStatus(status),
    };
  }
  return {
    kind: 'refresh',
    incidentId: event.data.incident_id,
    toast: event.event_type === 'INCIDENT_CREATED',
  };
}

/** ACT-002 진행 기록 한 줄 — `execution-status-panel`의 `ExecutionTransition`과 같은 모양이다. */
export interface Transition {
  at: string;
  from: ExecutionStatus | null;
  to: ExecutionStatus;
}

/**
 * 진행 기록에 전이를 한 줄 얹는다. **`prev`의 첫 원소는 접수 행**이라 첫 이벤트에서도 비교 대상이 있다.
 *
 * 접수 행을 `outcome`에서 파생하면 같은 리스너가 `outcome`을 덮어쓰면서 접수 시각·상태까지
 * 최신값으로 뭉개지고, 첫 이벤트의 중복 차단도 비교 대상이 없어 무력해진다(PR #181 리뷰).
 */
export function appendTransition(
  prev: readonly Transition[],
  next: { at: string; status: ExecutionStatus },
): Transition[] {
  const from = prev.at(-1)?.to ?? null;
  // 같은 status 재수신은 줄을 늘리지 않는다 — event_id 멱등과 별개로 값이 안 바뀐 경우다.
  if (from === next.status) return prev as Transition[];
  return [...prev, { at: next.at, from, to: next.status }];
}

/**
 * 같은 `event_id` 재수신은 중복 반영하지 않는다 — 수신 측 멱등 키다(계약 `ws.py`).
 * 무한히 쌓이지 않게 최근 것만 남긴다. 재연결 시 목록 재조회로 상태를 복구하므로
 * 오래된 id를 기억할 이유가 없다.
 */
export class SeenEvents {
  private readonly ids = new Set<string>();
  private readonly order: string[] = [];
  // 파라미터 프로퍼티(`constructor(private limit)`)는 Node 타입 스트리핑이 거부한다
  // (ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX) — 필드를 따로 선언한다.
  private readonly limit: number;

  constructor(limit = 200) {
    this.limit = limit;
  }

  /** 처음 보는 이벤트면 true. 두 번째부터는 false. */
  accept(eventId: string): boolean {
    if (this.ids.has(eventId)) return false;
    this.ids.add(eventId);
    this.order.push(eventId);
    if (this.order.length > this.limit) {
      const dropped = this.order.shift();
      if (dropped !== undefined) this.ids.delete(dropped);
    }
    return true;
  }
}

/**
 * 재연결 대기 — `1s → 2s → 4s → 8s → 최대 30s`(§4.8). `attempt`는 0부터.
 * ponytail: 지터 없음. 관제자 소수가 보는 화면이라 동시 재접속 폭주가 없다.
 */
export function backoffMs(attempt: number): number {
  return Math.min(1000 * 2 ** attempt, 30_000);
}

/**
 * WS 주소. 계약 경로는 `/api/v1/ws`이고 오리진은 REST와 같은 곳을 쓴다.
 *
 * `NEXT_PUBLIC_API_BASE_URL`이 없으면 **mock 단계**다 — 자체 origin의 Route Handler에는
 * WS가 없으므로 `null`을 돌려 연결을 시도하지 않는다. 무한 재접속으로 콘솔을 채우는 대신
 * 인디케이터가 그 사실을 그린다(2026-08-14 확정: mock 단계 WS 제외).
 */
export function websocketUrl(apiBaseUrl: string | undefined): string | null {
  if (!apiBaseUrl) return null;
  const url = `${apiBaseUrl.replace(/\/$/, '').replace(/^http/, 'ws')}/api/v1/ws`;
  // 스킴 없는 값(`localhost:8000`)은 위 치환에 걸리지 않아 그대로 통과한다 —
  // `new WebSocket()`이 SyntaxError를 던지므로 여기서 걸러 연결 자체를 열지 않는다(PR #181 리뷰).
  return /^wss?:\/\//.test(url) ? url : null;
}
