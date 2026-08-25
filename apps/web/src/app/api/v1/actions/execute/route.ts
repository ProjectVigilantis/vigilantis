// POST /api/v1/actions/execute mock — idempotency·오류 5종·실행 status 6종 분기를 재현합니다.
// mock 전용 쿼리: ?fail=internal(500 재현) · ?mock_result=<status>(결과 강제) — 계약 밖이라 실 BE는 무시.

import type { NextRequest } from 'next/server';

import {
  ROLLBACK_RUNBOOK_IDS,
  RUNBOOK_IDS,
  type ExecuteActionRequest,
  type ExecuteActionResponse,
  type ExecutionStatus,
  type ExecutionSummaryItem,
  type RollbackRunbookId,
  type RunbookId,
} from '@/types/api';

import {
  errorEnvelope,
  executionsByIdempotencyKey,
  incidentView,
  incidents,
} from '../../_mock/data';

const ALLOWED_KEYS = ['incident_id', 'runbook_id', 'idempotency_key'];

/** runbook_id별 새 Execution의 결과 상태. 롤백 3종(자식)은 IN_PROGRESS·SUCCESS·FAILED만 쓴다. */
const RESULT_STATUS: Record<RunbookId, ExecutionStatus> = {
  RUNBOOK_EC2_ISOLATE: 'IN_PROGRESS',
  RUNBOOK_NACL_ADD_DENY: 'SUCCESS',
  RUNBOOK_NACL_RESTORE: 'SUCCESS',
  RUNBOOK_SG_DELETE_ISOLATED: 'FAILED',
  RUNBOOK_EC2_RIGHTSIZING: 'ROLLBACK_INITIATED',
  RUNBOOK_EC2_ENABLE_AUTOSCALING: 'IN_PROGRESS',
  RUNBOOK_EBS_DELETE_UNATTACHED: 'SUCCESS',
  RUNBOOK_EC2_UNISOLATE: 'SUCCESS',
  RUNBOOK_SG_RECREATE: 'IN_PROGRESS',
  RUNBOOK_EC2_REVERT_SIZE: 'FAILED',
};

/** 롤백 실행 시 "원본" Execution에 기록되는 상태 — ROLLED_BACK·ROLLBACK_FAILED는 원본 전용이다. */
const ORIGIN_STATUS_AFTER_ROLLBACK: Record<RollbackRunbookId, ExecutionStatus> = {
  RUNBOOK_EC2_UNISOLATE: 'ROLLED_BACK',
  RUNBOOK_SG_RECREATE: 'ROLLBACK_INITIATED',
  RUNBOOK_EC2_REVERT_SIZE: 'ROLLBACK_FAILED',
};

/**
 * 새 Execution이 관제자에게 열어 주는 복구 조치(롤백 3종만).
 * 짝은 ADR-0004 표·SSOT §범위(P0 RIGHTSIZING+REVERT_SIZE, P1 SG_DELETE_ISOLATED+SG_RECREATE) 기준.
 * NACL_ADD_DENY의 해제(NACL_RESTORE)는 롤백이 아니라 본편 조치라 여기 오지 않는다(recommendations 경로).
 */
const RECOVERY_BY_RUNBOOK: Partial<Record<RunbookId, RollbackRunbookId[]>> = {
  RUNBOOK_EC2_ISOLATE: ['RUNBOOK_EC2_UNISOLATE'],
  RUNBOOK_SG_DELETE_ISOLATED: ['RUNBOOK_SG_RECREATE'],
  RUNBOOK_EC2_RIGHTSIZING: ['RUNBOOK_EC2_REVERT_SIZE'],
};

/**
 * 복구를 열어 줄 수 있는 원본 상태 — AWS가 실제로 변경된 뒤여야 되돌릴 게 있다.
 * FAILED("AWS 변경 없이 실패")·IN_PROGRESS(아직 안 끝남)에는 복구 조치를 노출하지 않는다.
 * ROLLBACK_INITIATED는 자동 원복이 개시된 상태라 REVERT_SIZE 경로가 열려 있어야 한다.
 */
const RECOVERABLE_ORIGIN_STATUSES = [
  'SUCCESS',
  'ROLLBACK_INITIATED',
] as const satisfies readonly ExecutionStatus[];

/**
 * mock 전용 결과 오버라이드(`?mock_result=`) 허용값.
 * ROLLED_BACK·ROLLBACK_FAILED는 원본 Execution 전용이라 새 실행에 강제할 수 없고,
 * 롤백 자식 Execution은 IN_PROGRESS → SUCCESS | FAILED만 쓴다(actions.py 계약).
 */
const OVERRIDABLE_RESULTS = [
  'IN_PROGRESS',
  'SUCCESS',
  'FAILED',
  'ROLLBACK_INITIATED',
] as const satisfies readonly ExecutionStatus[];

const OVERRIDABLE_ROLLBACK_RESULTS = OVERRIDABLE_RESULTS.filter(
  (s) => s !== 'ROLLBACK_INITIATED',
);

function isRollback(id: RunbookId): id is RollbackRunbookId {
  return (ROLLBACK_RUNBOOK_IDS as readonly string[]).includes(id);
}

/** 계약 검증(extra=forbid 포함). 통과하면 요청 DTO, 실패하면 사유 문자열을 돌려준다. */
function validateRequest(body: unknown): ExecuteActionRequest | string {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    return '요청 본문은 JSON 객체여야 합니다';
  }
  const b = body as Record<string, unknown>;
  const extra = Object.keys(b).filter((k) => !ALLOWED_KEYS.includes(k));
  if (extra.length > 0) {
    return `계약에 없는 필드는 보낼 수 없습니다: ${extra.join(', ')}`;
  }
  const { incident_id, runbook_id, idempotency_key } = b;
  if (typeof incident_id !== 'string' || incident_id.length === 0) {
    return 'incident_id는 1자 이상 문자열이어야 합니다';
  }
  // 상한은 계약(packages/schemas/api/actions.py, PR #119)에 맞춘다 — 없으면 검증을 통과한 값이
  // 실 BE 저장 단계에서 깨진다. FE가 만드는 키는 randomUUID(36자)라 화면 동작에는 닿지 않는다.
  if (
    typeof idempotency_key !== 'string' ||
    idempotency_key.length === 0 ||
    idempotency_key.length > 128
  ) {
    return 'idempotency_key는 1자 이상 128자 이하 문자열이어야 합니다';
  }
  if (typeof runbook_id !== 'string' || !(RUNBOOK_IDS as readonly string[]).includes(runbook_id)) {
    return 'runbook_id는 확정 Runbook 10종 중 하나여야 합니다';
  }
  return { incident_id, runbook_id: runbook_id as RunbookId, idempotency_key };
}

/** 계약 형식(초 단위 "Z")의 현재 시각. */
function nowIso(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export async function POST(request: NextRequest) {
  // mock 전용 트리거: 500 오류 경로 재현 (요청 본문 계약은 건드리지 않는다)
  if (request.nextUrl.searchParams.get('fail') === 'internal') {
    return errorEnvelope(500, 'INTERNAL_ERROR', '요청을 처리하지 못했습니다');
  }

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return errorEnvelope(422, 'REQUEST_VALIDATION_FAILED', 'JSON 본문을 파싱하지 못했습니다');
  }

  const parsed = validateRequest(raw);
  if (typeof parsed === 'string') {
    return errorEnvelope(422, 'REQUEST_VALIDATION_FAILED', parsed);
  }

  // 계약 4.7 처리 순서: 형식 검증 → Idempotency 확인 → Incident 존재 확인 → 제안 확인 → 예약
  const stored = executionsByIdempotencyKey.get(parsed.idempotency_key);
  if (stored) {
    // 같은 요청 재전송이면 202가 아니라 200 + 기존 실행 상태
    if (
      stored.incident_id !== parsed.incident_id ||
      stored.runbook_id !== parsed.runbook_id
    ) {
      return errorEnvelope(
        409,
        'IDEMPOTENCY_KEY_CONFLICT',
        '같은 idempotency_key가 다른 요청에 이미 사용됐습니다',
      );
    }
    const replay: ExecuteActionResponse = {
      execution_id: stored.execution.execution_id,
      status: stored.execution.status,
      updated_at: stored.execution.updated_at,
    };
    return Response.json(replay, { status: 200 });
  }

  const incident = incidents.find((i) => i.incident_id === parsed.incident_id);
  if (!incident) {
    return errorEnvelope(
      404,
      'INCIDENT_NOT_FOUND',
      `인시던트를 찾을 수 없습니다: ${parsed.incident_id}`,
    );
  }

  // 저장된 Guardrail PASS 제안이거나, 원본 Execution이 열어 준 복구 조치여야 실행 가능.
  // 이미 실행된 조치는 본편·복구 모두 재실행 불가 — 본편은 recommendations에서 이미 빠지지만,
  // 복구는 원본의 available_recovery_runbook_ids가 남아 있어 여기서 막아야 한다(이중 롤백 방지).
  const view = incidentView(incident);
  const executed = view.executions.some((e) => e.runbook_id === parsed.runbook_id);
  const recommended = view.recommendations.some((r) => r.runbook_id === parsed.runbook_id);
  const origin = executed
    ? undefined
    : view.executions.find((e) =>
        (e.available_recovery_runbook_ids as readonly string[]).includes(parsed.runbook_id),
      );
  if (!recommended && !origin) {
    return errorEnvelope(
      409,
      'PROPOSAL_NOT_EXECUTABLE',
      `현재 실행 가능한 제안·복구 조치가 아닙니다: ${parsed.runbook_id}`,
    );
  }

  // mock 전용: runbook별 고정 결과 대신 임의 결과를 강제한다(예: RIGHTSIZING 성공 경로).
  // 허용 목록 밖의 값은 조용히 무시하고 고정 결과를 쓴다.
  const requested = request.nextUrl.searchParams.get('mock_result');
  const allowed: readonly string[] = isRollback(parsed.runbook_id)
    ? OVERRIDABLE_ROLLBACK_RESULTS
    : OVERRIDABLE_RESULTS;
  const status = allowed.includes(requested ?? '')
    ? (requested as ExecutionStatus)
    : RESULT_STATUS[parsed.runbook_id];

  const updatedAt = nowIso();
  const execution: ExecutionSummaryItem = {
    execution_id: `exec-mock-${crypto.randomUUID().slice(0, 8)}`,
    runbook_id: parsed.runbook_id,
    status,
    available_recovery_runbook_ids: (
      RECOVERABLE_ORIGIN_STATUSES as readonly ExecutionStatus[]
    ).includes(status)
      ? (RECOVERY_BY_RUNBOOK[parsed.runbook_id] ?? [])
      : [],
    updated_at: updatedAt,
  };
  executionsByIdempotencyKey.set(parsed.idempotency_key, {
    incident_id: parsed.incident_id,
    runbook_id: parsed.runbook_id,
    execution,
  });

  // 롤백 자식 실행은 원본의 최종 상태(ROLLED_BACK·ROLLBACK_FAILED 포함)를 갱신한다
  if (origin && isRollback(parsed.runbook_id)) {
    origin.status = ORIGIN_STATUS_AFTER_ROLLBACK[parsed.runbook_id];
    origin.updated_at = updatedAt;
  }

  const body: ExecuteActionResponse = {
    execution_id: execution.execution_id,
    status: execution.status,
    updated_at: execution.updated_at,
  };
  return Response.json(body, { status: 202 });
}
