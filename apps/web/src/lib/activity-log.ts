// 로그 화면(`/logs`, 2026-11-03)의 한 줄 목록. 세 원천을 시각순으로 합친다.
//   - 조치 · 종료: GET /api/v1/audit-events(#544). 누가(고객사 사용자 · 플랫폼 운영자 · 시스템) 무엇을 실행했고 종료했나.
//   - 위협: GET /api/v1/threat-events(계약 types/api.ts `ThreatEventsResponse`, #572 10번).
//   - 인시던트 발생: GET /api/v1/incidents의 `created_at`.
// 원천마다 실패할 수 있어 받은 것만 합친다. 무엇이 빠졌는지는 화면이 따로 적는다.

import { RUNBOOK_LABELS, incidentTitle } from './enum-labels.ts';
import { RESOLUTION_LABELS } from './incident-closure.ts';
import { THREAT_KIND_LABELS, threatDetail, threatSource, type ThreatEventItem } from './threat-events.ts';
import type { AuditActor, AuditEventItem, IncidentListItem, TriggerSource } from '../types/api.ts';

/** 필터 묶음. 조치는 실행과 종료를 함께 센다(사람이 내린 판단). */
export type LogKind = 'ACTION' | 'THREAT' | 'INCIDENT';

export const LOG_KIND_LABELS: Record<LogKind, string> = {
  ACTION: '조치',
  THREAT: '위협',
  INCIDENT: '인시던트',
};

export interface LogEntry {
  id: string;
  at: string;
  kind: LogKind;
  /** 무엇이 일어났나. */
  title: string;
  /** 한 줄 보충(실행 사유 · 위협 세부 · 종료 판단 등). */
  detail: string | null;
  /** 누가: 사람 이메일 · 시스템 · 공격 출발지. */
  actor: string;
  /** 누가의 보충: 고객사 직원이면 "직원 · 권한", 플랫폼 운영자면 그렇다고 적는다. 시스템 · 위협은 null. */
  actorNote: string | null;
  /** 사람이 한 일이면 그 이메일(직원별로 거를 때 쓴다). 고객사 계정에 오지 않는 플랫폼 운영자 이메일은 null. */
  person: string | null;
  /** 무엇에: 자산 이름 또는 인시던트 제목. */
  target: string;
  /** 누르면 갈 곳(인시던트 상세). 위협은 인시던트가 없어 null. */
  href: string | null;
}

/** 시스템 발동 사유. 사람 승인(USER_APPROVAL)은 행위자 이메일로 적는다. */
const SYSTEM_TRIGGER: Record<Exclude<TriggerSource, 'USER_APPROVAL'>, string> = {
  PRE_MITIGATION_0_5S: '시스템 · 선제 차단',
  TIMEOUT_ISOLATION_1M: '시스템 · 1분 미응답 자동 격리',
  AUTO_ON_FAILURE: '시스템 · 실패 후 자동 복구',
  NACL_DENY_EXPIRED_24H: '시스템 · 차단 24시간 만료 해제',
};

/** 고객사 계정에는 플랫폼 운영자 이메일이 오지 않는다(계약). 그때는 "플랫폼 운영자"로 적는다. */
export function actorLabel(actor: AuditActor, trigger?: TriggerSource): string {
  if (actor.kind === 'SYSTEM') return trigger !== undefined && trigger !== 'USER_APPROVAL' ? SYSTEM_TRIGGER[trigger] : '시스템';
  if (actor.kind === 'PLATFORM_USER') return actor.email ?? '플랫폼 운영자';
  return actor.email;
}

/** 사람의 보충 설명. 권한은 계정 목록을 읽을 수 있을 때만(고객사 ADMIN) 붙는다. */
export function actorNote(actor: AuditActor, staffRoles: Readonly<Record<string, string>>): string | null {
  if (actor.kind === 'SYSTEM') return null;
  if (actor.kind === 'PLATFORM_USER') return '플랫폼 운영자';
  const role = staffRoles[actor.email];
  return role === undefined ? '직원' : `직원 · ${role} 권한`;
}

const incidentHref = (id: string) => `/incidents/${encodeURIComponent(id)}`;
const fallbackName = (arn: string) => arn.split('/').pop() ?? arn;

export function buildLog(input: {
  audit: readonly AuditEventItem[];
  threats: readonly ThreatEventItem[];
  incidents: readonly IncidentListItem[];
  assetNames: Readonly<Record<string, string>>;
  /** 고객사 직원 이메일 → 권한 이름. 계정 목록을 못 읽으면 빈 객체다. */
  staffRoles?: Readonly<Record<string, string>>;
  /** 이 시각 이후만 싣는다(인시던트 목록은 기간 질의가 없어 여기서 자른다). */
  sinceMs: number;
}): LogEntry[] {
  const { audit, threats, incidents, assetNames, sinceMs, staffRoles = {} } = input;
  const name = (arn: string) => assetNames[arn] ?? fallbackName(arn);
  const byId = new Map(incidents.map((i) => [i.incident_id, i]));
  const out: LogEntry[] = [];

  for (const e of audit) {
    const incident = byId.get(e.incident_id);
    if (e.action === 'ACTION_EXECUTION_STARTED') {
      out.push({
        id: `audit:${e.event_id}`,
        at: e.occurred_at,
        kind: 'ACTION',
        title: `${RUNBOOK_LABELS[e.runbook_id] ?? e.runbook_id} 실행`,
        detail: e.trigger_source === 'USER_APPROVAL' ? '승인 실행' : '자동 실행',
        actor: actorLabel(e.actor, e.trigger_source),
        actorNote: actorNote(e.actor, staffRoles),
        person: e.actor.email,
        target: name(e.target_arn),
        href: incidentHref(e.incident_id),
      });
    } else {
      out.push({
        id: `audit:${e.event_id}`,
        at: e.occurred_at,
        kind: 'ACTION',
        title: '인시던트 종료',
        detail: RESOLUTION_LABELS[e.resolution] ?? e.resolution,
        actor: actorLabel(e.actor),
        actorNote: actorNote(e.actor, staffRoles),
        person: e.actor.email,
        target: incident === undefined ? e.incident_id : incidentTitle(incident),
        href: incidentHref(e.incident_id),
      });
    }
  }

  for (const t of threats) {
    const source = threatSource(t);
    out.push({
      id: `threat:${t.threat_event_id}`,
      at: t.occurred_at,
      kind: 'THREAT',
      title: THREAT_KIND_LABELS[t.event_type],
      detail: threatDetail(t),
      actor: `${source.label} ${source.value}`,
      actorNote: null,
      person: null,
      target: name(t.target_arn),
      href: null,
    });
  }

  for (const i of incidents) {
    if (Date.parse(i.created_at) < sinceMs) continue;
    out.push({
      id: `incident:${i.incident_id}`,
      at: i.created_at,
      kind: 'INCIDENT',
      title: '인시던트 발생',
      detail: i.category === 'SECOPS' ? '보안' : '비용',
      actor: '시스템 탐지',
      actorNote: null,
      person: null,
      target: incidentTitle(i),
      href: incidentHref(i.incident_id),
    });
  }

  // 최신순. 같은 시각이면 id로 고정해 새로고침마다 줄이 흔들리지 않게 한다.
  return out.sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.id.localeCompare(b.id));
}

export function logKindCounts(entries: readonly LogEntry[]): Record<LogKind, number> {
  const counts: Record<LogKind, number> = { ACTION: 0, THREAT: 0, INCIDENT: 0 };
  for (const e of entries) counts[e.kind] += 1;
  return counts;
}
