'use client';

// DSH-001 토폴로지 카드 — AST-001의 `AssetGraph`(#146)를 그대로 쓴다. 배치 계산(`buildTopology`)을
// 다시 쓰지 않아야 대시보드와 자산 화면이 같은 그래프를 그린다(PR #299 리뷰).

import { useRouter } from 'next/navigation';

import { AssetGraph } from '@/components/assets/asset-graph';
import type { AssetItem } from '@/types/api';

export function DashboardTopology({ items }: { items: AssetItem[] }) {
  const router = useRouter();

  // AST-002는 Drawer라 자체 URL이 없다 — 자산 화면이 그 항목을 고른 상태로 여는 딥링크로 보낸다(§4.2).
  return (
    <AssetGraph
      items={items}
      onSelect={(asset) => router.push(`/assets?asset=${encodeURIComponent(asset.arn)}`)}
    />
  );
}
