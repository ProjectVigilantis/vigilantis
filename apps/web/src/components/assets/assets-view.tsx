'use client';

// AST-001 자산 관제 본체 — 목록⇄토폴로지 전환과 필터를 담습니다(화면설계서 v1.5 §4.2).

import { useMemo, useState } from 'react';

import { AssetCard } from '@/components/assets/asset-card';
import { AssetDetail } from '@/components/assets/asset-detail';
import { EmptyState } from '@/components/empty-state';
import { StatusBadge } from '@/components/status-badge';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ASSET_TYPE_LABELS } from '@/lib/enum-labels';
import { cn, formatKst } from '@/lib/utils';
import type { AssetItem, AssetType, AssetsResponse, IncidentListItem } from '@/types/api';

/** §4.2 `낭비 후보만` — Rule이 낭비로 판정한 두 verdict. */
const WASTE_VERDICTS = ['COST_CANDIDATE', 'UNUSED'] as const;

const ALL = '전체';

function Select({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="border-input bg-background focus-visible:ring-ring/50 rounded-md border px-2 py-1 focus-visible:ring-2 focus-visible:outline-none"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function AssetsView({
  data,
  incidentsByArn,
  openArn,
}: {
  data: AssetsResponse;
  /**
   * 인시던트는 자산 계약에 없다 — 목록 API의 `subject_arn` 역조인 결과다(§4.2·§4.3).
   * `null`은 **조회 실패**다. 0건과 구분해서 화면에 그대로 전달한다.
   */
  incidentsByArn: Record<string, IncidentListItem[]> | null;
  /** INC-002에서 넘어온 딥링크 대상 ARN(§4.5 액션). 목록에 없으면 무시한다. */
  openArn?: string;
}) {
  const [assetType, setAssetType] = useState<string>(ALL);
  // AST-002는 이 목록이 이미 받은 단건을 그대로 넘겨 연다 — 신규 페치 없음(§4.3).
  // 닫아도 selected를 비우지 않는다 — 닫는 애니메이션 도중 본문이 사라지면 빈 패널이 미끄러져 나간다.
  // 딥링크는 첫 렌더에만 반영한다 — 이후 열고 닫는 것은 사용자 조작이 정한다.
  const linked = openArn ? (data.items.find((a) => a.arn === openArn) ?? null) : null;
  const [selected, setSelected] = useState<AssetItem | null>(linked);
  const [detailOpen, setDetailOpen] = useState(linked !== null);
  const [region, setRegion] = useState<string>(ALL);
  const [wasteOnly, setWasteOnly] = useState(false);
  const [primaryOnly, setPrimaryOnly] = useState(false);

  const regions = useMemo(
    () => [...new Set(data.items.map((a) => a.region))].sort(),
    [data.items],
  );

  // 필터 4종 전부 클라이언트 필터다 — 계약에 Query Parameter가 없어 전량 응답에서 거른다(§4.2).
  const visible = useMemo(
    () =>
      data.items.filter((a) => {
        if (assetType !== ALL && a.asset_type !== assetType) return false;
        if (region !== ALL && a.region !== region) return false;
        if (primaryOnly && a.resource_role !== 'PRIMARY') return false;
        if (wasteOnly && !WASTE_VERDICTS.some((v) => v === a.verdict)) return false;
        return true;
      }),
    [data.items, assetType, region, wasteOnly, primaryOnly],
  );

  const typeOptions = [
    { value: ALL, label: `${ALL} (${data.items.length})` },
    ...(Object.keys(ASSET_TYPE_LABELS) as AssetType[]).map((t) => ({
      value: t,
      label: `${ASSET_TYPE_LABELS[t]?.label ?? t} (${data.items.filter((a) => a.asset_type === t).length})`,
    })),
  ];

  return (
    <Tabs defaultValue="list" className="gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <TabsList>
          <TabsTrigger value="list">목록</TabsTrigger>
          <TabsTrigger value="topology">토폴로지</TabsTrigger>
        </TabsList>

        <div className="flex flex-wrap items-center gap-3">
          <Select label="유형" value={assetType} options={typeOptions} onChange={setAssetType} />
          <Select
            label="리전"
            value={region}
            options={[{ value: ALL, label: ALL }, ...regions.map((r) => ({ value: r, label: r }))]}
            onChange={setRegion}
          />
          <Toggle checked={wasteOnly} onChange={setWasteOnly} label="낭비 후보만" />
          <Toggle checked={primaryOnly} onChange={setPrimaryOnly} label="주요 관제만" />
        </div>
      </div>

      {/* collection_status가 READY면 배지를 그리지 않는다(§3.2). PARTIAL·FAILED여도
          확보된 items는 그대로 렌더한다(§4.2 예외). */}
      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
        <StatusBadge field="collection_status" value={data.collection_status} />
        <span>갱신 {formatKst(data.last_collected_at)}</span>
        <span aria-live="polite">
          {visible.length === data.items.length
            ? `${data.items.length}건`
            : `${visible.length} / ${data.items.length}건`}
        </span>
      </div>

      <TabsContent value="list">
        {visible.length === 0 ? (
          <EmptyState
            message="조건에 맞는 자산이 없습니다."
            description="필터를 바꾸면 다른 자산을 볼 수 있습니다."
          />
        ) : (
          // 페이지네이션은 MVP 계약에 없다 — 전량 렌더한다(§4.2).
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {visible.map((asset) => (
              <AssetCard
                key={asset.arn}
                asset={asset}
                incidentCount={incidentsByArn === null ? null : (incidentsByArn[asset.arn] ?? []).length}
                onSelect={(a) => {
                  setSelected(a);
                  setDetailOpen(true);
                }}
              />
            ))}
          </div>
        )}
      </TabsContent>

      <TabsContent value="topology">
        <EmptyState
          message="토폴로지는 준비 중입니다."
          description="자산 연결관계(relationships) 산출이 선행 조건입니다."
        />
      </TabsContent>

      <AssetDetail
        asset={selected}
        incidents={
          incidentsByArn === null ? null : selected ? (incidentsByArn[selected.arn] ?? []) : []
        }
        open={detailOpen}
        onOpenChange={setDetailOpen}
      />
    </Tabs>
  );
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-1.5 text-sm">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        aria-label={label}
        className="accent-primary size-4"
      />
      <Badge variant="outline" className={cn(checked && 'border-ring text-foreground')}>
        {label}
      </Badge>
    </label>
  );
}
