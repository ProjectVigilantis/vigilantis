'use client';

// AST-001 자산 관제 본체 — 목록⇄토폴로지 전환과 필터를 담습니다(화면설계서 v1.5 §4.2).

import { useMemo, useState } from 'react';

import { AssetCard } from '@/components/assets/asset-card';
import { AssetGraph } from '@/components/assets/asset-graph';
import { AssetDetail } from '@/components/assets/asset-detail';
import { EmptyState } from '@/components/empty-state';
import { FilterSelect } from '@/components/filter-select';
import { StatusBadge } from '@/components/status-badge';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ASSET_TYPE_LABELS } from '@/lib/enum-labels';
import { cn, formatKst } from '@/lib/utils';
import type { AssetItem, AssetType, AssetsResponse, IncidentListItem } from '@/types/api';

/** §4.2 `낭비 후보만` — Rule이 낭비로 판정한 두 verdict. */
const WASTE_VERDICTS = ['COST_CANDIDATE', 'UNUSED'] as const;

const ALL = '전체';

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

  /**
   * v1.6 팀 회의 결정 — **목록 뷰는 `resource_role = PRIMARY`만 다룬다**(§4.2).
   * 지원 자산(NACL·ASG·시작 템플릿·ALB 대상 그룹)은 비용이 붙지 않고 `evaluation_status =
   * NOT_APPLICABLE`이라 카드의 판정·헬스·인시던트 세 열이 전부 `—`다 — 자리를 쓰면서 아무것도
   * 답하지 않는다. 구 `주요 관제만` 토글은 이 규칙이 상시가 되면서 **제거**했다(켤 대상이 없다).
   *
   * **토폴로지 뷰가 서면(#146) 그쪽은 `data.items` 전량을 써야 한다** — 관계 6종 중 4종이 이
   * 노드 위에 그려지고 `RUNBOOK_NACL_ADD_DENY`의 대상도 NACL이다(§4.2).
   */
  const primaryItems = useMemo(
    () => data.items.filter((a) => a.resource_role === 'PRIMARY'),
    [data.items],
  );
  const supportCount = data.items.length - primaryItems.length;

  /**
   * 토폴로지의 초점 집합 — **유형 필터만 뺀 나머지**다(#146). 위 TabsContent 주석 참조.
   *
   * v1.6에서 `주요 관제만` 토글이 사라져(위 judgedItems 주석) 초점 축은 리전·낭비 후보 2종이다.
   * 초점은 `data.items` **전량**에서 고른다 — 목록에서 뺀 지원 자산도 그래프에는 노드로 있고,
   * 리전만 걸었을 때 그 노드가 흐려지면 같은 리전인데 빠진 것처럼 읽힌다.
   */
  const focusedArns = useMemo(() => {
    if (region === ALL && !wasteOnly) return null;
    return new Set(
      data.items
        .filter((a) => {
          if (region !== ALL && a.region !== region) return false;
          if (wasteOnly && !WASTE_VERDICTS.some((v) => v === a.verdict)) return false;
          return true;
        })
        .map((a) => a.arn),
    );
  }, [data.items, region, wasteOnly]);

  const regions = useMemo(
    () => [...new Set(primaryItems.map((a) => a.region))].sort(),
    [primaryItems],
  );

  // 필터 3종 전부 클라이언트 필터다 — 계약에 Query Parameter가 없어 전량 응답에서 거른다(§4.2).
  const visible = useMemo(
    () =>
      primaryItems.filter((a) => {
        if (assetType !== ALL && a.asset_type !== assetType) return false;
        if (region !== ALL && a.region !== region) return false;
        if (wasteOnly && !WASTE_VERDICTS.some((v) => v === a.verdict)) return false;
        return true;
      }),
    [primaryItems, assetType, region, wasteOnly],
  );

  // 유형 옵션도 PRIMARY 기준으로 세운다 — 목록에 없는 유형을 고를 수 있으면 0건만 나온다.
  const typeOptions = [
    { value: ALL, label: `${ALL} (${primaryItems.length})` },
    ...(Object.keys(ASSET_TYPE_LABELS) as AssetType[])
      .map((t) => ({ type: t, count: primaryItems.filter((a) => a.asset_type === t).length }))
      .filter(({ count }) => count > 0)
      .map(({ type, count }) => ({
        value: type,
        label: `${ASSET_TYPE_LABELS[type]?.label ?? type} (${count})`,
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
          <FilterSelect label="유형" value={assetType} options={typeOptions} onChange={setAssetType} />
          <FilterSelect
            label="리전"
            value={region}
            options={[{ value: ALL, label: ALL }, ...regions.map((r) => ({ value: r, label: r }))]}
            onChange={setRegion}
          />
          <Toggle checked={wasteOnly} onChange={setWasteOnly} label="낭비 후보만" />
        </div>
      </div>

      {/* collection_status가 READY면 배지를 그리지 않는다(§3.2). PARTIAL·FAILED여도
          확보된 items는 그대로 렌더한다(§4.2 예외). */}
      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
        <StatusBadge field="collection_status" value={data.collection_status} />
        <span>갱신 {formatKst(data.last_collected_at)}</span>
        <span aria-live="polite">
          {visible.length === primaryItems.length
            ? `${primaryItems.length}건`
            : `${visible.length} / ${primaryItems.length}건`}
        </span>
        {/* 응답에 있었는데 화면에 없는 것이 있으면 그 사실을 말한다 — 숨긴 줄 모르면
            "수집이 안 된 것"으로 읽힌다(§4.2). */}
        {supportCount > 0 ? (
          <span>지원 자산 {supportCount}건 제외 (판정·비용 대상 아님 · 토폴로지에는 남는다)</span>
        ) : null}
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
        {/* 유형 필터는 걸지 않는다 — 노드를 빼면 엣지의 도착 노드가 사라져 그래프가 끊어진
            것처럼 보인다. 나머지 필터는 초점(흐리게)으로만 반영한다(§9.3 FE 판단). */}
        <AssetGraph
          items={data.items}
          focusedArns={focusedArns}
          onSelect={(a) => {
            setSelected(a);
            setDetailOpen(true);
          }}
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
