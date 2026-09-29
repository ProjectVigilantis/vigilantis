// SEC-001 보안 관제 집계 — 요약 API가 없어 `GET /assets`·`GET /incidents` 응답을 FE가 센다(대시보드와 같은
// 규칙, dashboard.ts). 렌더와 분리해 `node --test`로 검증한다. 같은 디렉터리 상대 경로만 쓴다 —
// `node --test`는 `@/` 별칭을 해석하지 못한다(타입 전용 import는 스트리핑돼 사라진다).

import { isOpenSg, UNHANDLED_STATUSES } from './dashboard.ts';
import { INCIDENT_STATUS_LABELS } from './enum-labels.ts';
import type { AssetItem, IncidentListItem, IncidentStatus } from '@/types/api';

export interface SecurityMetrics {
  /** 보안 그룹 전량. */
  sgTotal: number;
  /** 전체 대역(0.0.0.0/0 · ::/0)에 인바운드가 열린 보안 그룹. */
  openSg: number;
  /**
   * 개방 SG를 `SECURED_BY`로 가리키는 EC2 — **중복 없이** 센다. 개방 SG 둘에 걸린 한 대는 한 대다.
   * 대시보드 개방 SG 목록의 `영향 EC2`는 SG마다 세므로 그 합과 다를 수 있다.
   */
  affectedEc2: number;
  /** 규칙 엔진 위협 판정 자산(`verdict = THREAT`). */
  threat: number;
  /** 미조치 **보안** 인시던트(대시보드 `unhandled.SECOPS`와 같은 셈). 조회 실패면 null. */
  unhandled: number | null;
  /** NACL 수 — 차단 런북(`RUNBOOK_NACL_ADD_DENY`)의 대상이라 보안 축에서만 센다. */
  nacl: number;
}

export function securityMetrics(
  items: readonly AssetItem[],
  incidents: readonly IncidentListItem[] | null,
): SecurityMetrics {
  const openArns = new Set(items.filter(isOpenSg).map((sg) => sg.arn));
  return {
    sgTotal: items.filter((a) => a.asset_type === 'SG').length,
    openSg: openArns.size,
    affectedEc2: items.filter(
      (a) =>
        a.asset_type === 'EC2' &&
        a.relationships.some((r) => r.relation_type === 'SECURED_BY' && openArns.has(r.target_arn)),
    ).length,
    threat: items.filter((a) => a.verdict === 'THREAT').length,
    unhandled:
      incidents === null
        ? null
        : incidents.filter(
            (i) =>
              i.category === 'SECOPS' &&
              (UNHANDLED_STATUSES as readonly IncidentStatus[]).includes(i.status),
          ).length,
    nacl: items.filter((a) => a.asset_type === 'NACL').length,
  };
}

/**
 * 지표 띠의 칸 = 초점. **칸 하나가 자산 집합 하나를 가리킨다** — 누르면 목록·토폴로지·인시던트 탭이
 * 그 집합으로 좁혀진다(security-view.tsx 머리말 표). 순서가 띠의 순서다. `SG`가 기본이다.
 */
export const SECURITY_FOCUSES = [
  'SG',
  'OPEN_SG',
  'AFFECTED_EC2',
  'THREAT',
  'UNHANDLED',
  'NACL',
] as const;
export type SecurityFocus = (typeof SECURITY_FOCUSES)[number];

/** 초점 칸이 가리키는 자산. 리전은 걸지 않는다 — 호출부가 그 위에 겹친다. */
export function focusAssets(
  items: readonly AssetItem[],
  /** 인시던트 목록 전량. 조회 실패(null)면 `UNHANDLED`는 빈 집합이다 — 모르는 것을 지어내지 않는다. */
  incidents: readonly IncidentListItem[] | null,
  focus: SecurityFocus,
): AssetItem[] {
  const openArns = new Set(items.filter(isOpenSg).map((sg) => sg.arn));
  switch (focus) {
    case 'SG':
      return items.filter((a) => a.asset_type === 'SG');
    case 'OPEN_SG':
      return items.filter((a) => openArns.has(a.arn));
    case 'AFFECTED_EC2':
      return items.filter(
        (a) =>
          a.asset_type === 'EC2' &&
          a.relationships.some((r) => r.relation_type === 'SECURED_BY' && openArns.has(r.target_arn)),
      );
    case 'THREAT':
      return items.filter((a) => a.verdict === 'THREAT');
    case 'UNHANDLED': {
      // 칸의 숫자는 **건수**, 집합은 그 건들이 걸린 **자산**이다 — 한 자산에 두 건이 걸리면 한 장이다.
      const subjects = new Set(
        (incidents ?? [])
          .filter(
            (i) =>
              i.category === 'SECOPS' &&
              (UNHANDLED_STATUSES as readonly IncidentStatus[]).includes(i.status),
          )
          .map((i) => i.subject_arn),
      );
      return items.filter((a) => subjects.has(a.arn));
    }
    case 'NACL':
      return items.filter((a) => a.asset_type === 'NACL');
  }
}

/**
 * 토폴로지 초점·인시던트 탭의 범위(ARN 집합). **아무것도 안 걸렸으면 null**이다(초점 없음).
 *
 * 기본 칸(`SG`)은 **자산 전량**이 범위다 — 기본에서 보안 그룹만 밝히면 첫 화면의 그래프가 EC2·EBS를
 * 전부 흐리게 그리고, 인시던트 탭에서는 EC2에 걸린 SSH 무차별 대입 건이 통째로 사라진다. 목록만
 * 보안 그룹으로 좁힌다(호출부). 다른 칸은 그 칸의 집합이 곧 범위다.
 */
export function focusScope(
  items: readonly AssetItem[],
  incidents: readonly IncidentListItem[] | null,
  focus: SecurityFocus,
  /** 리전 필터. 걸지 않았으면 null. */
  region: string | null,
): Set<string> | null {
  if (focus === 'SG' && region === null) return null;
  const scope = focus === 'SG' ? items : focusAssets(items, incidents, focus);
  return new Set(scope.filter((a) => region === null || a.region === region).map((a) => a.arn));
}

export interface StatusCount {
  status: IncidentStatus;
  count: number;
}

/**
 * 보안 인시던트 상태 분포 — 상태 6종을 **0건까지 전부** 낸다(표기 사전 순). 빠지면 "그 상태가 없다"와
 * "안 센다"가 구분되지 않는다(자산 인벤토리와 같은 이유). 조회 실패면 null — 0건과 구분한다.
 */
export function secopsStatusCounts(
  incidents: readonly IncidentListItem[] | null,
): StatusCount[] | null {
  if (incidents === null) return null;
  const secops = incidents.filter((i) => i.category === 'SECOPS');
  return (Object.keys(INCIDENT_STATUS_LABELS) as IncidentStatus[]).map((status) => ({
    status,
    count: secops.filter((i) => i.status === status).length,
  }));
}
