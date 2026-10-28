// 인시던트 상세의 자동 발동 안내(§4.5). Medium 1분 자동 격리 고지와 High 선차단 기록 줄을 정한다(#532).
//
// 판정은 서버가 준 `auto_isolation_block`만 따른다. 테넌트 정책(자동 조치 끄기 · 재활성화 시각)을
// FE에서 다시 계산하지 않는다. 서버의 실제 접수 판정과 화면이 갈라지면 안 된다(#521 · PR #524).

import { agentWaitTimes } from './realtime-events.ts';
import type { IncidentResponse, IsoDateTime } from '../types/api.ts';

/**
 * B-Medium 타임아웃 창 —**고지를 그리는 조건이자 기준 시각을 래치하는 조건**이다.
 * 두 곳이 따로 판정하면 화면에는 시각이 떠 있는데 기준은 안 잡히는 식으로 어긋난다.
 */
export function inAgentWaitWindow(
  incident: Pick<IncidentResponse, 'response_mode' | 'initial_risk_level' | 'status'>,
): boolean {
  return (
    incident.response_mode === 'AGENT_WAIT' &&
    incident.initial_risk_level !== 'LOW' &&
    incident.status === 'AWAITING_APPROVAL'
  );
}

export type AgentWaitNotice =
  /** 자동 발동이 막혀 있다. 실행 예고 대신 승인이 필요하다고 알린다. */
  | { kind: 'BLOCKED' }
  /** 전환 이벤트의 `occurred_at`에서 파생한 두 시각을 보여 준다. */
  | { kind: 'SCHEDULED'; startedAt: IsoDateTime; deadlineAt: IsoDateTime }
  /** 전환 이벤트를 못 받았거나 값이 계약 밖이다. 시각 없는 고정 안내문을 쓴다. */
  | { kind: 'UNTIMED' };

export const AUTO_ISOLATION_BLOCKED_NOTICE =
  '이 건은 자동으로 격리되지 않습니다. 조치하려면 제안을 검토하고 승인해 주세요.';

/**
 * 창 밖이면 null. 차단 사유는 시각 유무보다 먼저 본다. 시각이 있든 없든 「실행 예정」은 틀린 말이 된다.
 * `auto_isolation_block`이 null이면 기존 안내를 그대로 쓴다(null은 실행 보장이 아니라 "막힌 이유 없음"이다).
 */
export function agentWaitNotice(
  incident: Pick<
    IncidentResponse,
    'response_mode' | 'initial_risk_level' | 'status' | 'auto_isolation_block'
  >,
  agentWaitAt: IsoDateTime | null,
): AgentWaitNotice | null {
  if (!inAgentWaitWindow(incident)) return null;
  if (incident.auto_isolation_block !== null) return { kind: 'BLOCKED' };
  const times = agentWaitTimes(agentWaitAt);
  return times === null ? { kind: 'UNTIMED' } : { kind: 'SCHEDULED', ...times };
}

/**
 * High 선차단 건의 조치 기록 줄. `response_mode`는 대응 정책일 뿐 실행 결과가 아니다. 기록이 없으면
 * 없다고 적고, 있으면 「수행된 조치」의 상태 배지로 보낸다. 실행 주체 필드가 없어 어느 기록이
 * 자동 선차단이었는지는 추정하지 않는다.
 */
export function preMitigationRecordNote(
  incident: Pick<IncidentResponse, 'response_mode' | 'executions'>,
): string | null {
  if (incident.response_mode !== 'PRE_MITIGATION_0_5S') return null;
  const count = incident.executions.length;
  return count === 0 ? '실행 기록 없음' : `${count}건 · 상태는 아래 「수행된 조치」에서 확인`;
}
