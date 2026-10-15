import { isResolvable } from './incident-filter.ts';
import { isTerminalStatus } from './execution-status.ts';
import type { IncidentResponse, ResolutionJudgement } from '../types/api.ts';

export const RESOLUTION_LABELS: Record<ResolutionJudgement, string> = {
  JUSTIFIED: '수행한 조치가 정당했음',
  NO_FURTHER_ACTION: '추가 조치 없이 종료',
};

/** 상태·분석·실행을 각각 확인한다. 제안이 남아 있어도 종료하며 무효화할 수 있다. */
export function closureBlockedReason(incident: IncidentResponse): string | null {
  if (incident.executions.some((e) => !isTerminalStatus(e.status)) ||
      incident.status === 'ACTION_IN_PROGRESS') return '진행 중인 실행이 끝난 뒤 종료할 수 있습니다.';
  const analysis = incident.analysis_result?.status;
  if (incident.status === 'ANALYZING' || (incident.category === 'SECOPS' &&
      (analysis == null || analysis === 'PENDING' || analysis === 'IN_PROGRESS'))) {
    return '분석이 끝난 뒤 종료할 수 있습니다.';
  }
  return isResolvable(incident.status) ? null : '이미 종료된 인시던트입니다.';
}

/** 현재 해제 제안과 최신 NACL 실행이 모두 차단 유지를 뒷받침할 때만 그렇게 표시한다. */
export function closureAction(incident: IncidentResponse): { label: string; description: string } {
  const naclExecutions = incident.executions.filter((e) =>
    e.runbook_id === 'RUNBOOK_NACL_ADD_DENY' || e.runbook_id === 'RUNBOOK_NACL_RESTORE');
  const latestAt = Math.max(...naclExecutions.map((e) => Date.parse(e.updated_at)));
  const latest = naclExecutions.filter((e) => Date.parse(e.updated_at) === latestAt);
  const keepBlock = incident.category === 'SECOPS' &&
    incident.recommendations.some((r) => r.runbook_id === 'RUNBOOK_NACL_RESTORE') &&
    latest.length > 0 && latest.every((e) =>
      e.runbook_id === 'RUNBOOK_NACL_ADD_DENY' && e.status === 'SUCCESS');

  if (keepBlock) {
    return {
      label: '차단 유지하고 종료',
      description: `남은 제안 ${incident.recommendations.length}건을 모두 거절하고 종료합니다. 차단 해제를 포함한 추가 조치는 실행하지 않습니다.`,
    };
  }
  if (incident.recommendations.length > 0) {
    return {
      label: '제안 거절하고 종료',
      description: `남은 제안 ${incident.recommendations.length}건을 실행하지 않고 모두 거절한 뒤 종료합니다.`,
    };
  }
  return {
    label: incident.executions.length === 0 ? '종료' : '추가 조치 없이 종료',
    description: '추가 실행 없이 인시던트를 종료합니다.',
  };
}
