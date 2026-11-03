// 자산 유형별 비용(2026-11-03 초안): 고객사 대시보드 자산 현황의 "유형별 비용"이 최근 30일 동안 실제로 나간 돈을 보이는 자리다.
//
// 서버에 이 API가 아직 없다. 원천은 AWS Cost Explorer `GetCostAndUsage`(고객사 계정 역할로 조회, 일 단위 · 비혼합 비용)이며,
// 아래 `AssetCostsResponse`는 FE가 제안하는 계약이다. 목업(fe-mock)만 응답하고 실서버는 404라 화면이 그 사실을 적는다.
// BE가 계약을 정하면 타입을 types/api.ts로 옮기고 이 머리말을 고친다.
//
// 자산 유형과 Cost Explorer 항목의 대응(서버가 묶는다):
//   EC2              SERVICE = "Amazon Elastic Compute Cloud - Compute"
//   EBS              SERVICE = "EC2 - Other" 중 USAGE_TYPE `*EBS:*`(볼륨 · 스냅샷 · IOPS · 처리량)
//   ALB_TARGET_GROUP SERVICE = "Elastic Load Balancing"(돈은 대상 그룹이 아니라 ALB에 붙는다)
//   OTHER            위에 들지 않는 나머지(데이터 전송 · NAT 게이트웨이 · CloudWatch 등)
// 보안 그룹 · NACL · Auto Scaling 그룹 · 시작 템플릿은 AWS 요금이 없다.

import type { AssetType } from '../types/api.ts';

type IsoDateTime = string;

export type CostBucket = 'EC2' | 'EBS' | 'ALB_TARGET_GROUP' | 'OTHER';

export const COST_BUCKETS: readonly CostBucket[] = ['EC2', 'EBS', 'ALB_TARGET_GROUP', 'OTHER'];

export interface AssetCostsResponse {
  /** UNAVAILABLE = Cost Explorer를 읽지 못했다(권한 없음 · 계정에서 꺼짐 · 호출 실패). */
  status: 'READY' | 'UNAVAILABLE';
  /** UNAVAILABLE 사유. 예: COST_EXPLORER_ACCESS_DENIED · COST_EXPLORER_NOT_ENABLED · COST_EXPLORER_THROTTLED. READY면 null. */
  reason_code: string | null;
  currency: 'USD';
  /** 조회 구간. 날짜(YYYY-MM-DD, UTC)이고 끝은 포함하지 않는다(Cost Explorer 규칙). */
  period_start: string;
  period_end: string;
  /** 구간 합계. 소수 2자리 문자열. UNAVAILABLE이면 null. */
  total: string | null;
  /** 네 칸 전부 싣는다(0원 포함). UNAVAILABLE이면 빈 배열. */
  items: { bucket: CostBucket; amount: string }[];
  /** 서버가 Cost Explorer에서 받아 둔 시각. 하루 한 번 받고(호출당 과금 · 반영 지연 최대 24시간) 그 사이는 저장분을 준다. */
  retrieved_at: IsoDateTime;
}

/** AWS 요금이 붙지 않는 유형. 화면은 금액 대신 "과금 없음"으로 적는다. */
export const FREE_ASSET_TYPES: readonly AssetType[] = ['SG', 'NACL', 'AUTO_SCALING_GROUP', 'LAUNCH_TEMPLATE'];

export const COST_BUCKET_LABELS: Record<CostBucket, string> = {
  EC2: 'EC2',
  EBS: 'EBS 볼륨',
  ALB_TARGET_GROUP: '로드 밸런서',
  OTHER: '기타',
};

/** 막대 색. EC2 · EBS는 위 추이 그래프와 같은 색이다. */
export const COST_COLOR: Record<CostBucket, string> = {
  EC2: 'var(--chart-series-1)',
  EBS: 'var(--chart-series-3)',
  ALB_TARGET_GROUP: 'var(--chart-series-4)',
  OTHER: 'color-mix(in oklch, var(--muted-foreground) 55%, transparent)',
};

/** 화면이 받는 상태. missing = 서버에 경로가 없다(404 · 405), failed = 그 밖의 실패. */
export type CostLoad = { kind: 'ready'; data: AssetCostsResponse } | { kind: 'missing' } | { kind: 'failed' };

export interface CostSlice {
  bucket: CostBucket;
  amount: number;
  /** 합계 대비(0-1). */
  ratio: number;
}

const toNumber = (s: string | null) => {
  const n = s === null ? NaN : Number(s);
  return Number.isFinite(n) ? n : null;
};

/** 막대 조각. 0원과 읽히지 않는 금액은 뺀다. 순서는 COST_BUCKETS 고정이다(금액 순으로 돌리면 색이 순위를 따라간다). */
export function costSlices(res: AssetCostsResponse): CostSlice[] {
  if (res.status !== 'READY') return [];
  const amounts = COST_BUCKETS.map((bucket) => ({ bucket, amount: toNumber(res.items.find((i) => i.bucket === bucket)?.amount ?? null) ?? 0 }));
  const sum = amounts.reduce((s, a) => s + a.amount, 0);
  if (sum <= 0) return [];
  return amounts.filter((a) => a.amount > 0).map((a) => ({ ...a, ratio: a.amount / sum }));
}

/** 유형 한 줄의 금액. 요금 없는 유형은 'FREE', 계약에 칸이 없거나 읽지 못하면 null. */
export function costOfType(res: AssetCostsResponse, type: AssetType): number | 'FREE' | null {
  if (FREE_ASSET_TYPES.includes(type)) return 'FREE';
  if (res.status !== 'READY') return null;
  const bucket: CostBucket | null = type === 'EC2' || type === 'EBS' || type === 'ALB_TARGET_GROUP' ? type : null;
  if (bucket === null) return null;
  return toNumber(res.items.find((i) => i.bucket === bucket)?.amount ?? null);
}

export function costTotal(res: AssetCostsResponse): number | null {
  return res.status === 'READY' ? toNumber(res.total) : null;
}

/** 조회 구간을 "10/4부터 11/2까지"로 적는다. 끝 날짜는 포함하지 않는 값이라 하루 앞을 적는다. */
export function costPeriodLabel(res: AssetCostsResponse): string {
  const md = (ymd: string, minusDays = 0) => {
    const d = new Date(`${ymd}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() - minusDays);
    return `${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
  };
  return `${md(res.period_start)}부터 ${md(res.period_end, 1)}까지`;
}
