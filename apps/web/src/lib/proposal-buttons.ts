// 제안 조치 버튼 규칙 — INC-002 상세(§4.5 버튼 노출 규칙)와 DSH-001 「AI 조치 제안」 카드(§4.1)가
// **같은 함수**를 쓴다. 두 화면이 규칙을 따로 들고 있으면 한쪽만 고쳐져 갈린다(#363 · PR #351 리뷰).
//
// 문구는 **인시던트 분류(`category`)가 아니라 실행 후보 런북**으로 정한다. 분류로 가르면 SECOPS의
// 해제(`RUNBOOK_NACL_RESTORE`)와 삭제(`RUNBOOK_SG_DELETE_ISOLATED`)에도 `승인하고 차단`이 붙어
// **버튼이 실제 동작과 반대로 말한다** — T2 시연 7단계 「원클릭 해제」가 그 핵심 컷이다
// (`docs/E2E_DEMO_SCENARIOS.md` T2 표). 최종 승인 모달(ACT-001)에 런북 이름이 나오지만,
// 관제자는 **누르기 전에 보이는 문구**로 판단하므로 모달은 이 문제를 덮지 못한다.
//
// 렌더와 분리해 `node --test`로 고정한다. 같은 디렉터리 상대 경로만 쓴다 — `node --test`는 `@/`
// 별칭을 해석하지 못한다(타입 전용 import는 스트리핑돼 사라진다).

import type { AiRecommendableRunbookId, IncidentResponse } from '@/types/api';

/** 실행 후보의 동작 계열 — 버튼 문구를 고르는 유일한 축이다. */
export type RunbookActionKind = 'BLOCK' | 'RELEASE' | 'DELETE' | 'ADJUST';

/**
 * AI 추천 가능 7종(본편)의 동작 계열. 롤백 3종은 계약 validator가 `recommendations`에서 막으므로
 * 여기 없다 — 키 타입이 `AiRecommendableRunbookId`라 **런북이 늘면 컴파일이 이 표를 먼저 막는다.**
 *
 * `DELETE`는 `DESTRUCTIVE_RUNBOOK_IDS`(ACT-001 경고 블록의 판별 근거)와 **같은 집합이어야 한다** —
 * 되돌릴 수 없다고 경고해 놓고 버튼은 다른 계열로 부르면 두 표시가 어긋난다. 양방향 등식을
 * `proposal-buttons.test.ts`가 고정한다.
 */
export const RUNBOOK_ACTION_KINDS: Record<AiRecommendableRunbookId, RunbookActionKind> = {
  RUNBOOK_EC2_ISOLATE: 'BLOCK',
  RUNBOOK_NACL_ADD_DENY: 'BLOCK',
  RUNBOOK_NACL_RESTORE: 'RELEASE',
  RUNBOOK_SG_DELETE_ISOLATED: 'DELETE',
  RUNBOOK_EBS_DELETE_UNATTACHED: 'DELETE',
  RUNBOOK_EC2_RIGHTSIZING: 'ADJUST',
  RUNBOOK_EC2_ENABLE_AUTOSCALING: 'ADJUST',
};

/**
 * 계열별 문구. `ADJUST`가 중립 문구를 겸한다 — 스펙 조정·Auto Scaling 전환은 차단도 해제도 삭제도
 * 아니어서 동작을 한 낱말로 줄일 자리가 없고, 그 자리를 억지로 만들면 문구가 동작을 좁혀 말한다.
 */
const KIND_LABELS: Record<RunbookActionKind, string> = {
  BLOCK: '승인하고 차단',
  RELEASE: '승인하고 해제',
  DELETE: '승인하고 삭제',
  ADJUST: '이 조치 실행',
};

/**
 * 계열이 섞인 후보 묶음의 문구. 버튼 하나가 후보 **전부**를 한 번에 실행하므로(§4.6 요청 3필드),
 * 한쪽 계열로 적으면 나머지가 그 문구에 가려진다 — 중립으로 두고 모달이 런북을 밝힌다.
 */
export const MIXED_APPROVE_LABEL = KIND_LABELS.ADJUST;

/**
 * 후보 전부가 같은 계열일 때만 그 계열을 돌려준다. 섞였거나 후보가 없으면 `null`이다.
 * 후보 0건은 호출부가 버튼 자체를 만들지 않는 자리이며(§4.5 조회 전용), 여기서도 계열을 지어내지 않는다.
 */
export function proposalActionKind(
  recommendations: readonly { runbook_id: AiRecommendableRunbookId }[],
): RunbookActionKind | null {
  if (recommendations.length === 0) return null;
  const first = RUNBOOK_ACTION_KINDS[recommendations[0].runbook_id];
  return recommendations.every((r) => RUNBOOK_ACTION_KINDS[r.runbook_id] === first) ? first : null;
}

/**
 * §4.5 버튼 노출 규칙 — 실행 버튼 문구와 반려 버튼 노출 여부.
 *
 * | 후보 계열 | 실행 버튼 | 반려 버튼(`response_mode = AGENT_WAIT`일 때) |
 * | --- | --- | --- |
 * | 차단(`EC2_ISOLATE`·`NACL_ADD_DENY`) | `승인하고 차단` | `차단 안 함` |
 * | 해제(`NACL_RESTORE`) | `승인하고 해제` | 없음 |
 * | 삭제(`SG_DELETE_ISOLATED`·`EBS_DELETE_UNATTACHED`) | `승인하고 삭제` | 없음 |
 * | 조정(`EC2_RIGHTSIZING`·`EC2_ENABLE_AUTOSCALING`)·섞임 | `이 조치 실행` | 없음 |
 *
 * **반려는 차단 제안에만 붙는다.** `차단 안 함`은 *막지 않기로 한다*는 판단이라, 해제·삭제 후보
 * 옆에 두면 누르지 않은 차단을 되돌리겠다는 말이 된다. 종전 규칙(SECOPS 전체)은 `AGENT_WAIT`가
 * SECOPS 전용이라 넓어 보이지 않았을 뿐, 해제 후보에서도 켜졌다.
 */
export function proposalButtons(
  incident: Pick<IncidentResponse, 'recommendations' | 'response_mode'>,
): { approveLabel: string; canReject: boolean } {
  const kind = proposalActionKind(incident.recommendations);
  return {
    approveLabel: kind === null ? MIXED_APPROVE_LABEL : KIND_LABELS[kind],
    canReject: kind === 'BLOCK' && incident.response_mode === 'AGENT_WAIT',
  };
}
