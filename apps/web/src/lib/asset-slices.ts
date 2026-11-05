// 자산 구성 조각의 색과 이름(EC2 · 보안 그룹 · EBS · 판정 비대상). 도넛(components/dashboard/asset-composition-chart.tsx),
// 자산 수 추이 그래프(trend-charts.tsx), 고객사 대시보드 자산 현황의 비율 막대가 같은 표를 쓴다.
// 서버 컴포넌트도 읽어야 해서 'use client' 파일이 아니라 여기에 둔다(클라이언트 모듈의 값은 서버에서 참조만 된다).

import type { CompositionKey } from './dashboard.ts';
import { ASSET_TYPE_LABELS } from './enum-labels.ts';

export const SLICE_COLOR: Record<CompositionKey, string> = {
  EC2: 'var(--chart-series-1)',
  SG: 'var(--chart-series-2)',
  EBS: 'var(--chart-series-3)',
  OTHER: 'color-mix(in oklch, var(--muted-foreground) 55%, transparent)',
};

export const sliceLabel = (key: CompositionKey) => (key === 'OTHER' ? '판정 비대상' : (ASSET_TYPE_LABELS[key]?.label ?? key));
