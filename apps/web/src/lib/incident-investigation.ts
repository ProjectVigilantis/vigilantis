// 인시던트 상세의 사고 조사 항목(#622 · #640 B9 · B13). 상세(`GET /incidents/{id}`)와 감사 목록
// (`GET /audit-events?incident_id=`)을 `execution_id`로 잇는다. 항목별 원천과 해석 규칙은 #622 코멘트의 표다.
//
//   - `ACTION_EXECUTION_STARTED`는 실행 예약 기록이다. 결과는 상세 `executions[].status`로만 본다.
//   - 시작 주체 · `trigger_source` · `target_arn`은 그 실행의 감사 기록에서만 얻는다. 감사 기록이 없는 실행
//     (T-2 감사 기록 도입 전)은 "확인 안 됨"으로 두고 추천의 `target_arn`이나 `subject_arn`으로 채우지 않는다.
//   - 원복은 별도 자식 실행 행이다. `parent_execution_id`로 원 실행 아래에 묶되, 값이 있다고 원복 성공은 아니다.

import { actorLabel } from './activity-log.ts';
import type {
  AuditEventItem,
  AuditEventsResponse,
  ExecutionStartedEvent,
  ExecutionSummaryItem,
  GuardrailStep,
  IncidentResolvedEvent,
} from '../types/api.ts';

/** 감사 기록이 없거나 읽지 못한 칸. "없음"으로 쓰면 일이 없었다고 읽힌다(#622 해석 3). */
export const UNCONFIRMED = '확인 안 됨';

/** 한 인시던트의 감사 기록을 몇 쪽까지 읽나(쪽당 100건). 넘치면 잘렸다고 적는다. */
export const INCIDENT_AUDIT_MAX_PAGES = 10;

export type IncidentAudit =
  | { kind: 'ready'; items: AuditEventItem[]; truncated: boolean }
  | { kind: 'failed' };

/**
 * 감사 목록이 온전하지 않을 때 구역에 붙이는 안내. 이 안내가 없으면 조회 실패와 "원래 기록 없음"이
 * 같은 "확인 안 됨"으로 보인다(PR #647 리뷰). `what`은 그 구역이 감사 기록에서 얻는 칸이다(목적격 조사 포함).
 */
export function auditGapNotice(audit: IncidentAudit, what: string): string | null {
  if (audit.kind === 'failed') return `감사 기록을 읽지 못해 ${what} 확인하지 못했습니다.`;
  if (audit.truncated) return `감사 기록이 많아 최근 ${audit.items.length}건만 읽었습니다.`;
  return null;
}

/**
 * `next_cursor`가 null이 될 때까지 받아 합친다. 커서는 받은 값을 그대로 돌려줄 뿐 해석하지 않는다.
 * 쪽 상한에 닿으면 `truncated`로 알린다. 실패는 호출부가 접는다(401 redirect를 삼키지 않게).
 */
export async function loadIncidentAudit(
  fetchPage: (cursor: string | undefined) => Promise<AuditEventsResponse>,
  maxPages: number = INCIDENT_AUDIT_MAX_PAGES,
): Promise<{ items: AuditEventItem[]; truncated: boolean }> {
  const items: AuditEventItem[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < maxPages; page += 1) {
    const res = await fetchPage(cursor);
    items.push(...res.items);
    if (res.next_cursor === null) return { items, truncated: false };
    cursor = res.next_cursor;
  }
  return { items, truncated: true };
}

/** 수행된 조치 한 줄. 원복 자식은 원 실행의 `recoveries`에 들어간다. */
export interface ExecutionRow {
  execution: ExecutionSummaryItem;
  /** 이 실행의 예약 기록. 감사 기록이 없거나 읽지 못했으면 null. */
  started: ExecutionStartedEvent | null;
  recoveries: ExecutionRow[];
}

/**
 * 상세 `executions[]`를 원 실행 아래 원복 자식으로 묶고 감사 예약 기록을 붙인다. 순서는 상세 응답 순서를 따른다.
 * 원 실행이 목록에 없는 자식은 맨 위 줄로 둔다. 감사 목록을 읽지 못했으면 `audit`에 빈 배열을 넘긴다.
 */
export function executionRows(
  executions: readonly ExecutionSummaryItem[],
  audit: readonly AuditEventItem[],
): ExecutionRow[] {
  const started = new Map<string, ExecutionStartedEvent>();
  for (const e of audit) {
    if (e.action === 'ACTION_EXECUTION_STARTED' && !started.has(e.execution_id)) started.set(e.execution_id, e);
  }
  const rows = new Map<string, ExecutionRow>(
    executions.map((execution) => [
      execution.execution_id,
      { execution, started: started.get(execution.execution_id) ?? null, recoveries: [] },
    ]),
  );
  const roots: ExecutionRow[] = [];
  for (const execution of executions) {
    const row = rows.get(execution.execution_id)!;
    const parentId = execution.parent_execution_id;
    const parent = parentId === null || parentId === execution.execution_id ? undefined : rows.get(parentId);
    if (parent === undefined) roots.push(row);
    else parent.recoveries.push(row);
  }
  return roots;
}

/** 누가 · 무엇이 시작했나. 사람 승인이면 이메일 뒤에 "승인"을 붙이고, 자동 발동이면 시스템 사유를 적는다. */
export function startedByLabel(event: ExecutionStartedEvent): string {
  const who = actorLabel(event.actor, event.trigger_source);
  return event.trigger_source === 'USER_APPROVAL' ? `${who} 승인` : who;
}

/**
 * 지금 종료 판단을 남긴 감사 기록. 복구로 재개된 뒤 다시 종료하면 기록이 여럿이라 가장 늦은 것을 고른다.
 * 감사 기록이 없으면 null(상세의 `resolution`은 그대로 보인다).
 */
export function latestResolution(audit: readonly AuditEventItem[]): IncidentResolvedEvent | null {
  let latest: IncidentResolvedEvent | null = null;
  for (const e of audit) {
    if (e.action !== 'INCIDENT_RESOLVED') continue;
    if (latest === null || Date.parse(e.occurred_at) > Date.parse(latest.occurred_at)) latest = e;
  }
  return latest;
}

/** 가드레일 단계 이름. 실행 순서대로 번호를 붙인다. */
export const GUARDRAIL_STEP_LABELS: Record<GuardrailStep, string> = {
  SCHEMA_CHECK: '① 스키마 검사',
  ACTION_WHITELIST: '② 허용 목록',
  ARN_MATCH: '③ ARN 대조',
  AWS_DRY_RUN: '④ AWS 사전 실행',
};
