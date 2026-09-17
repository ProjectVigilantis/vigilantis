// ACT-001 승인 요청 — 모달을 여는 순간의 **인시던트와 후보를 한 객체로 고정**합니다(화면설계서 §4.6).
// 렌더와 분리해 `node --test`로 검증한다. 타입만 `@/` 별칭으로 가져온다(스트리핑돼 사라진다).

import type {
  AssetItem,
  ExecuteActionRequest,
  IncidentResponse,
  RunbookId,
} from '@/types/api';

/** 실행 후보 1건. 주 조치는 `recommendations[]`의 값을 그대로 싣는다. */
export interface ActionCandidate {
  runbookId: RunbookId;
  /**
   * **실제로 바뀌는 자원**이다. `subject_arn`과 다를 수 있다 — 예를 들어 SG 인시던트의
   * `RUNBOOK_NACL_ADD_DENY`는 NACL을 고친다(PR #169 리뷰). 복구 런북은 계약에 이 값이 없어 null이다.
   */
  targetArn: string | null;
  /** 표시 전용. FE가 key 표시명을 지어내지 않고 원문을 쓴다. */
  displayParameters: Record<string, string> | null;
  /**
   * `targetArn`을 `GET /assets`에 조인한 자산(#183 A안). 수집 목록에 없거나 조회가 실패하면 null이다.
   * **조인은 호출부가 한다** — 진입 경로마다 자산을 부르는 방식이 달라서(상세는 이미 부른 결과 재사용,
   * 목록은 병렬 조회) 모달이 그 차이를 알 필요가 없다.
   */
  targetAsset: AssetItem | null;
}

/**
 * 모달 한 인스턴스가 다루는 요청. **열 때 한 번 만들어 모달 수명 동안 바꾸지 않는다.**
 *
 * - `idempotencyKey` — 버튼 클릭 시점에 만들면 중복 클릭이 서로 다른 키가 되어 멱등성이 무력화된다(§4.6).
 *   취소 후 재진입은 호출부가 새 객체를 만들므로 자연히 새 키가 된다.
 * - `incidentId` — **보이는 후보와 전송하는 인시던트가 같은 스냅샷에서 나와야 한다.** 모달이 인시던트를
 *   prop으로 따로 받으면, 연 사이에 호출부가 재조회로 다른 인시던트를 넘길 때 A의 대상·파라미터를
 *   보여 준 채 B의 `incident_id`를 보낸다 — B에 같은 런북 후보가 있으면 서버는 B를 실행한다
 *   (PR #351 리뷰 1, 대시보드 1순위 교체로 재현).
 */
export interface ActionRequest {
  idempotencyKey: string;
  incidentId: string;
  /** 후보에 `targetArn`이 없을 때(복구 런북) 모달의 `대상` 줄에 대신 보이는 인시던트 자산. */
  subjectArn: string;
  /** 실행 후보. 주 조치는 `recommendations`, 복구는 해제할 롤백 런북 1종. */
  candidates: ActionCandidate[];
  variant: 'ACTION' | 'RECOVERY';
  /** B 변형 표시용 — 어느 실행을 해제하는지. 전송하지 않는다(계약에서 폐기된 필드다). */
  originExecutionId?: string;
}

/**
 * 제안 조치(`recommendations`)로 여는 승인 요청. INC-001 목록과 DSH-001 카드가 같은 규칙을 쓴다.
 * `assets`는 후보의 `target_arn` 조인에만 쓰고, 조회가 실패했으면 빈 배열을 넘긴다 — 실행을 막지 않는다.
 */
export function proposalRequest(
  incident: IncidentResponse,
  assets: readonly AssetItem[],
  idempotencyKey: string,
): ActionRequest {
  return {
    idempotencyKey,
    incidentId: incident.incident_id,
    subjectArn: incident.subject_arn,
    variant: 'ACTION',
    candidates: incident.recommendations.map((r) => ({
      runbookId: r.runbook_id,
      targetArn: r.target_arn,
      displayParameters: r.display_parameters,
      targetAsset: assets.find((a) => a.arn === r.target_arn) ?? null,
    })),
  };
}

/**
 * 전송 본문은 **요청 스냅샷에서만** 만든다 — 인시던트를 인자로 받지 않는 것이 이 함수의 요점이다.
 * 모달에 보이는 ARN·스펙·IP는 보내지 않는다(`extra=forbid` → 422).
 */
export function executeBody(request: ActionRequest, runbookId: RunbookId): ExecuteActionRequest {
  return {
    incident_id: request.incidentId,
    runbook_id: runbookId,
    idempotency_key: request.idempotencyKey,
  };
}
