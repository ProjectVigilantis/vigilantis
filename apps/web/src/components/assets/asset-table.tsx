'use client';

// AST-001 자산 목록 표. 한 줄이 자산 1건이고, 줄을 고르면 옆 상세 창(넓은 화면) 또는 서랍(좁은 화면)이 그 자산을 보인다.
// 줄마다 보이는 값은 종전 명함 카드와 같다(유형 · 이름 · 리전과 상태 · 판정 · 헬스 · 인시던트 수).
// 표로 바꾼 이유: 상세 창이 화면 오른쪽을 차지하면 카드 격자는 한 줄에 한두 장밖에 서지 않아 훑어볼 수 없다.

import { HealthArea, UnknownValue, VerdictArea } from '@/components/assets/asset-card';
import { StatusBadge } from '@/components/status-badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ASSET_TYPE_LABELS, NO_VALUE, assetStateEntry } from '@/lib/enum-labels';
import { cn } from '@/lib/utils';
import type { AssetItem, IncidentListItem } from '@/types/api';

export function AssetTable({
  items,
  selectedArn,
  incidentsByArn,
  onSelect,
}: {
  items: AssetItem[];
  /** 지금 상세에 보이는 자산. 그 줄을 강조한다. */
  selectedArn: string | null;
  /** `null`은 인시던트 조회 실패다. 0건과 같은 자리에 그리지 않는다(PR #137 리뷰). */
  incidentsByArn: Record<string, IncidentListItem[]> | null;
  onSelect: (asset: AssetItem) => void;
}) {
  return (
    <div className="bg-card ring-foreground/10 overflow-hidden rounded-xl ring-1">
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow className="bg-secondary hover:bg-secondary">
              <TableHead className="text-foreground-secondary w-full pl-4 text-xs font-semibold">자산</TableHead>
              <TableHead className="text-foreground-secondary text-xs font-semibold whitespace-nowrap">판정</TableHead>
              <TableHead className="text-foreground-secondary text-right text-xs font-semibold whitespace-nowrap">헬스</TableHead>
              <TableHead className="text-foreground-secondary pr-4 text-right text-xs font-semibold whitespace-nowrap">인시던트</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((asset) => {
              const selected = asset.arn === selectedArn;
              const state = assetStateEntry(asset);
              const name = asset.name ?? asset.resource_id;
              const incidentCount = incidentsByArn === null ? null : (incidentsByArn[asset.arn] ?? []).length;
              return (
                <TableRow
                  key={asset.arn}
                  // 줄 아무 곳이나 눌러도 고른다. 키보드는 이름 버튼이 받는다(표의 줄은 버튼 역할을 할 수 없다).
                  onClick={() => onSelect(asset)}
                  className={cn('h-14 cursor-pointer', selected && 'bg-accent hover:bg-accent')}
                >
                  <TableCell className="w-full max-w-0 min-w-48 pl-4">
                    <div className="flex min-w-0 items-center gap-2">
                      <StatusBadge field="asset_type" value={asset.asset_type} />
                      <div className="min-w-0">
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            onSelect(asset);
                          }}
                          aria-label={`${ASSET_TYPE_LABELS[asset.asset_type]?.label ?? asset.asset_type} ${name} 상세 보기`}
                          aria-current={selected ? 'true' : undefined}
                          className="focus-visible:ring-ring/50 block max-w-full truncate rounded-sm text-left font-semibold hover:underline focus-visible:ring-3 focus-visible:outline-none"
                          title={name}
                        >
                          {name}
                        </button>
                        <p className="text-muted-foreground truncate text-xs">
                          {asset.region}
                          {state ? <> · {state.label}</> : null}
                        </p>
                      </div>
                    </div>
                  </TableCell>
                  <TableCell className="whitespace-nowrap [&>span]:flex-nowrap">
                    {/* NOT_APPLICABLE(NACL·ASG·LT·TG)은 목록에 서지 않지만, 서더라도 판정 칸을 비워 둔다(§3.2). */}
                    {asset.evaluation_status === 'NOT_APPLICABLE' ? null : <VerdictArea asset={asset} />}
                  </TableCell>
                  <TableCell className="text-right whitespace-nowrap">
                    <span className="inline-flex justify-end">
                      <HealthArea score={asset.health_score} />
                    </span>
                  </TableCell>
                  <TableCell className="pr-4 text-right whitespace-nowrap tabular-nums">
                    {incidentCount === null ? (
                      <span className="inline-flex justify-end"><UnknownValue /></span>
                    ) : incidentCount > 0 ? (
                      <span className="font-semibold">{incidentCount}건</span>
                    ) : (
                      <span className="text-muted-foreground">{NO_VALUE}</span>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
