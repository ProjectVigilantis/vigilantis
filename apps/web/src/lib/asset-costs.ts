// 자산 유형별 비용(2026-11-03 초안 · #572 11번 계약): 고객사 대시보드 자산 현황의 "유형별 비용"이 최근 30일 동안
// 자원이 쓴 금액을 보이는 자리다.
//
// 계약(`AssetCostsResponse` · `CostBucket`)은 types/api.ts에 있다(packages/schemas/api/costs.py). 원천은 AWS Cost Explorer
// `GetCostAndUsage`(고객사 계정 역할로 조회, 비혼합 비용, RECORD_TYPE=Usage)라 크레딧 · 환불 · 세금이 빠진 사용 금액이고,
// 고객사의 관제 리전(`regions`)으로 거른다. 이 파일은 화면 계산만 둔다.
//
// 자산 유형과 Cost Explorer 항목의 대응(서버가 묶는다, apps/core-api/services/costs.py):
//   EC2              SERVICE = "Amazon Elastic Compute Cloud - Compute"
//   EBS              SERVICE = "EC2 - Other" 중 USAGE_TYPE 이름에 `EBS:`가 든 항목(볼륨 · 스냅샷 · IOPS · 처리량)
//   ALB_TARGET_GROUP SERVICE = "Amazon Elastic Load Balancing"(돈은 대상 그룹이 아니라 ALB에 붙는다)
//   OTHER            위에 들지 않는 나머지(데이터 전송 · NAT 게이트웨이 · CloudWatch 등)
// EBS 분류는 서버 쪽 규칙이다. `GetCostAndUsage`의 차원 필터는 `EBS:` 같은 와일드카드를 받지 않으므로,
// 서버는 SERVICE · USAGE_TYPE 기준으로 묶어 받은 뒤 USAGE_TYPE 이름을 보고 가른다.
// 보안 그룹 · NACL · Auto Scaling 그룹 · 시작 템플릿은 AWS 요금이 없다.

import type { AssetCostsResponse, AssetType, CostBucket } from '../types/api.ts';

export type { AssetCostsResponse, CostBucket };

export const COST_BUCKETS: readonly CostBucket[] = ['EC2', 'EBS', 'ALB_TARGET_GROUP', 'OTHER'];

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

/**
 * 화면이 비용 모드로 그릴지 정하는 값. 조회 성공(READY이고 합계를 읽음)이면 합계가 0원이어도 값을 준다.
 * 막대에 그릴 조각(`slices`)이 비었는지는 조회 성공 여부와 따로 본다. 못 읽었으면 null.
 */
export function readyCostView(res: AssetCostsResponse): { total: number; slices: CostSlice[]; other: number } | null {
  const total = costTotal(res);
  if (total === null) return null;
  const slices = costSlices(res);
  return { total, slices, other: slices.find((c) => c.bucket === 'OTHER')?.amount ?? 0 };
}

/** 비용을 못 읽었을 때 화면에 적는 사유. 조회 성공(합계 0원 포함)이면 null이다. */
export function costFailureNote(costs: CostLoad): string | null {
  if (costs.kind === 'missing') return '비용 API가 서버에 아직 없습니다.';
  if (costs.kind === 'failed') return '비용을 불러오지 못했습니다.';
  if (readyCostView(costs.data) !== null) return null;
  return `비용을 읽지 못했습니다${costs.data.reason_code ? `(${costs.data.reason_code})` : ''}.`;
}

/**
 * 지금 자산 목록에는 없지만 구간 안에 비용이 나간 유형(삭제한 EC2 · EBS · ALB 등). 요약에서 빠지지 않게 따로 줄을 만든다.
 * 순서는 COST_BUCKETS를 따른다.
 */
export function pastCostTypes(res: AssetCostsResponse, presentTypes: readonly AssetType[]): AssetType[] {
  const typed: AssetType[] = ['EC2', 'EBS', 'ALB_TARGET_GROUP'];
  return typed.filter((t) => {
    const amount = costOfType(res, t);
    return !presentTypes.includes(t) && typeof amount === 'number' && amount > 0;
  });
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
