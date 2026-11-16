// 관리자 대시보드 "확인 필요" 그래프. 연결 상태는 GET /tenants 응답 하나로, API 이상 호출은 플랫폼 상태의 `api_usage`로 센다.
// 형태 · 색 규칙은 DESIGN.md §데이터 시각화를 따른다: 분류끼리 건수 비교라 가로 막대, 막대는 파랑 한 계열,
// 상태 색은 분류 이름 옆 점에만 쓴다.
//
// 고객이 수백 곳이어도 높이가 그대로다(분류 줄 수 고정 · 2026-10-29 개편 — 종전 고객마다 한 줄 막대는 고객 수만큼 길어졌다).
// 어느 고객사인지는 고객사 목록에서 본다(대시보드는 숫자와 그래프만 둔다).
// 2026-11-03 PM 지시: 정상 · 확인 전 · 오래됨 줄을 빼고 API 이상 호출 줄을 더했다. 미납 점은 주황.
// 담당 그룹 없음(#572 8번, 11/3 결정): 중앙 관리계정에게만 센다. `group_id`가 null인 고객사이며 그룹에 넣으면 빠진다.
// GET /tenants?unassigned_only=true와 같은 집합이다. 대시보드는 이미 전체 목록을 받으므로 따로 부르지 않는다.

import { BarRows, type BarRow } from '@/components/admin-dashboard/chart-parts';
import { connectionPoints, type ConnectionState } from '@/lib/admin-dashboard';
import type { TenantListItem } from '@/types/api';

/** 확인 필요로 세는 연결 상태. 순서가 막대 순서다. */
export const CHECK_STATES = ['FAILED', 'UNREACHABLE', 'UNPAID'] as const satisfies readonly ConnectionState[];
type CheckState = (typeof CHECK_STATES)[number];

const STATE: Record<CheckState, { label: string; dot: string; hint: string }> = {
  FAILED: { label: '연결 실패', dot: 'bg-status-critical', hint: '역할 · 권한 문제로 확인 실패' },
  UNREACHABLE: { label: '확인 불가', dot: 'bg-status-idle', hint: '응답 없음, 다시 시도하면 풀릴 수 있음' },
  UNPAID: { label: '미납', dot: 'bg-status-serious', hint: '미납으로 기능 중단, 연결 확인을 보내지 않음' },
};

/** API 이상 호출 집계. 플랫폼 상태를 받는 계정(lib/roles.ts `canViewPlatformStatus`)에만 있다. */
export interface ApiOverCall {
  /** 기준을 넘은 고객사 이름. 조회에 실패했으면 null. */
  tenants: readonly string[] | null;
  windowHours: number;
  /** 과다 호출 기준이 정해진 고객사가 한 곳이라도 있는가. 기준은 고객사마다 다르고 서버가 정한다. */
  judged: boolean;
}

/** 담당 그룹이 없는 고객사 수(`group_id` null). */
export function unassignedTenantCount(tenants: readonly TenantListItem[]): number {
  return tenants.filter((t) => t.group_id === null).length;
}

export function needCheckCounts(tenants: readonly TenantListItem[], nowMs: number): Record<CheckState, number> {
  const counts: Record<CheckState, number> = { FAILED: 0, UNREACHABLE: 0, UNPAID: 0 };
  for (const p of connectionPoints(tenants, nowMs)) {
    if ((CHECK_STATES as readonly string[]).includes(p.state)) counts[p.state as CheckState] += 1;
  }
  return counts;
}

export function NeedCheckRows({
  tenants,
  nowMs,
  apiOverCall,
  showUnassigned = false,
}: {
  tenants: readonly TenantListItem[];
  nowMs: number;
  /** 없으면(플랫폼 상태를 받지 않는 계정) API 이상 호출 줄을 그리지 않는다. */
  apiOverCall?: ApiOverCall;
  /** 담당 그룹 없음 줄을 그린다(중앙 관리계정). 관리자 직원계정은 미배정 고객사를 받지 않으므로 그리지 않는다. */
  showUnassigned?: boolean;
}) {
  const counts = needCheckCounts(tenants, nowMs);
  const rows: BarRow[] = CHECK_STATES.map((s) => ({
    key: s,
    label: STATE[s].label,
    value: counts[s],
    dot: STATE[s].dot,
    title: `${STATE[s].label} ${counts[s]}곳 · ${STATE[s].hint}`,
  }));
  if (showUnassigned) {
    const n = unassignedTenantCount(tenants);
    rows.push({
      key: 'UNASSIGNED',
      label: '담당 그룹 없음',
      value: n,
      dot: 'bg-status-warning',
      title: `담당 그룹 없음 ${n}곳 · 맡은 관리자 직원계정이 없음, 그룹에 넣으면 빠짐`,
    });
  }
  // 기준이 없으면(미판정) 줄을 그리지 않는다. 0을 그리면 "판정했더니 0곳"으로 읽힌다(PR #644 리뷰).
  if (apiOverCall !== undefined && apiOverCall.tenants !== null && apiOverCall.judged) {
    const names = apiOverCall.tenants;
    rows.push({
      key: 'API_OVER_CALL',
      label: 'API 이상 호출',
      value: names.length,
      dot: 'bg-status-critical',
      title: `최근 ${apiOverCall.windowHours}시간 중 고객사별 기준을 넘은 고객사 ${names.length}곳${
        names.length > 0 ? `: ${names.join(', ')}` : ''
      }`,
    });
  }
  return (
    <>
      <BarRows rows={rows} labelWidth={112} />
      {apiOverCall === undefined ? null : apiOverCall.tenants === null ? (
        <p role="status" className="text-muted-foreground text-xs">
          고객사 API 호출 집계를 불러오지 못해 API 이상 호출은 빠졌습니다.
        </p>
      ) : !apiOverCall.judged ? (
        <p role="status" className="text-muted-foreground text-xs">
          과다 호출 기준이 없어 API 이상 호출은 판정하지 않았습니다.
        </p>
      ) : null}
    </>
  );
}
