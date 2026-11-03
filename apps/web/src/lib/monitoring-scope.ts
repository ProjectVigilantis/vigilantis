// 고객사 상세의 관제를 그 고객사 AWS 계정 하나로 좁힌다(2026-11-02).
//
// 자산 · 인시던트 · 시계열 API는 아직 고객사 범위를 받지 않는다(T-1). 중앙 관리계정에게는 전체 고객사 몫이 오므로
// 화면이 고객사의 AWS 계정 ID로 거른다. 자산은 `account_id`, 인시던트와 CPU · 네트워크 시계열은 ARN의 계정 칸으로 고른다.
// SG 개방 · 자산 현황 · 자산 수 · 위협 이벤트 축은 서버가 범위 전체를 한 줄로 합쳐 보내 고객사별로 나눌 수 없다.
// 그 축은 다른 고객사 몫이 섞인 숫자를 보이지 않도록 `UNAVAILABLE`로 바꾼다. T-1이 들어오면 이 파일은 지운다.

import { arnAccount } from './admin-dashboard.ts';
import type { AssetsResponse, IncidentListItem, MetricsTimeseriesResponse } from '../types/api.ts';

/** 고객사별로 나눌 수 없는 집계 축에 붙이는 사유. 차트의 조회 실패 자리에 그대로 보인다. */
export const TENANT_AGGREGATE_UNSUPPORTED = '고객사별 집계 미지원';

export function scopeAssets(data: AssetsResponse, accountId: string): AssetsResponse {
  return { ...data, items: data.items.filter((a) => a.account_id === accountId) };
}

export function scopeIncidents(incidents: IncidentListItem[], accountId: string): IncidentListItem[] {
  return incidents.filter((i) => arnAccount(i.subject_arn) === accountId);
}

export function scopeMetrics(metrics: MetricsTimeseriesResponse, accountId: string): MetricsTimeseriesResponse {
  const mine = (arn: string) => arnAccount(arn) === accountId;
  const unsupported = { status: 'UNAVAILABLE' as const, points: [], reason_code: TENANT_AGGREGATE_UNSUPPORTED };
  return {
    ...metrics,
    cpu: { ...metrics.cpu, series: metrics.cpu.series.filter((s) => mine(s.arn)) },
    network: { ...metrics.network, series: metrics.network.series.filter((s) => mine(s.arn)) },
    sg_exposure: unsupported,
    asset_status: unsupported,
    asset_inventory: unsupported,
    threat_events: unsupported,
  };
}
