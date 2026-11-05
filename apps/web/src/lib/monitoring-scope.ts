// 고객사 상세의 관제를 그 고객사 AWS 계정 하나로 좁힌다(2026-11-02).
//
// 자산 · 인시던트 · 시계열 API는 아직 고객사 범위를 받지 않는다(T-1). 중앙 관리계정에게는 전체 고객사 몫이 오므로
// 화면이 고객사의 AWS 계정 ID로 거른다. 자산은 `account_id`, 인시던트와 CPU · 네트워크 시계열은 ARN의 계정 칸으로 고른다.
// SG 개방 · 자산 현황 · 자산 수 · 위협 이벤트 축은 서버가 범위 전체를 한 줄로 합쳐 보내 고객사별로 나눌 수 없다.
// 그 축은 다른 고객사 몫이 섞인 숫자를 보이지 않도록 `UNAVAILABLE`로 바꾼다. T-1이 들어오면 이 파일은 지운다.
// 자산 봉투의 수집 상태 · 수집 못 한 유형 · 마지막 수집 시각도 범위 전체 값 하나다. 유형별 실패는 고객사 화면에서 빼고,
// 셋은 "전체 고객사 기준"으로 따로 넘긴다(`scopeWideCollection`). 거른 자산만 보고 수집 성공을 짐작하지 않는다(PR #578 리뷰).

import { arnAccount } from './admin-dashboard.ts';
import type {
  AssetsResponse,
  CollectionStatus,
  IncidentListItem,
  MetricsTimeseriesResponse,
  UncollectedAssetType,
} from '../types/api.ts';

/** 고객사별로 나눌 수 없는 집계 축에 붙이는 사유. 차트의 조회 실패 자리에 그대로 보인다. */
export const TENANT_AGGREGATE_UNSUPPORTED = '고객사별 집계 미지원';

/** 서버가 조회 범위 전체로 합쳐 보내는 수집 메타. 어느 고객사의 값인지 알 수 없다. */
export interface ScopeWideCollection {
  status: CollectionStatus;
  lastCollectedAt: string | null;
  uncollected: UncollectedAssetType[];
}

/**
 * 고객사 계정의 자산만 남긴다. `uncollected`(수집 못 한 유형)는 비운다. 다른 고객사의 실패가 이 고객사의 유형에 붙으면
 * 정상인 유형이 "수집 실패"로 보이고 자산 수까지 가려진다. 범위 전체 값은 `scopeWideCollection`으로 따로 보인다.
 */
export function scopeAssets(data: AssetsResponse, accountId: string): AssetsResponse {
  return { ...data, items: data.items.filter((a) => a.account_id === accountId), uncollected: [] };
}

export function scopeWideCollection(data: AssetsResponse): ScopeWideCollection {
  return { status: data.collection_status, lastCollectedAt: data.last_collected_at, uncollected: data.uncollected };
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
