'use client';

// AST-001 자산 관제 본체 — 유형 지표 띠 + 목록⇄토폴로지 전환과 필터를 담습니다(화면설계서 v1.5 §4.2).
//
// ## 맨 위 지표 띠 (2026-09-18 추가)
//
// 종전에는 명함 카드가 곧장 깔려서, 화면을 열자마자 보이는 것이 "이 계정에 EC2가 몇 대인가"가
// 아니라 **첫 화면에 우연히 들어온 카드 몇 장**이었다. 관제 화면의 첫 줄은 셈이어야 한다 —
// 메인 대시보드(DSH-001)와 같은 타일(`components/metric-strip.tsx`)을 써서 전체 + 유형 7종을
// 세고, 타일을 누르면 그 유형만 남는다(누른 타일을 다시 누르면 전체로 돌아온다).
//
// **띠의 숫자는 수집 전량이다 — 목록에 서는 수가 아니다.** NACL·Auto Scaling 그룹·시작 템플릿·
// 대상 그룹은 Rule 판정 대상이 아니라 목록에서 빠지는데(`isJudgedAsset`), 띠에서까지 빼면
// "수집이 안 된 것"으로 읽힌다. 대신 그 유형 타일에는 `토폴로지 전용`이라 적고, 눌렀을 때
// 목록 대신 토폴로지로 넘어가는 빈 상태를 준다.
//
// 리전·낭비 후보 필터는 띠에 반영하지 않는다 — 띠는 "이 계정에 무엇이 있나"를 말하는 자리이고,
// 거기까지 필터를 먹이면 분모가 흔들려 두 수집 회차를 견줄 수 없다.

import { useMemo, useState } from 'react';

import { AssetCard } from '@/components/assets/asset-card';
import { AssetGraph } from '@/components/assets/asset-graph';
import { AssetDetail } from '@/components/assets/asset-detail';
import { SavingsCard } from '@/components/assets/savings-card';
import { CpuTrendChart, NetworkTrendChart } from '@/components/dashboard/trend-charts';
import { EmptyState } from '@/components/empty-state';
import { FilterSelect } from '@/components/filter-select';
import { MetricStrip, MetricTile } from '@/components/metric-strip';
import { Panel } from '@/components/panel';
import { isJudgedAsset } from '@/lib/asset-filter';
import { StatusBadge } from '@/components/status-badge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { inventoryCounts, IDLE_CPU_AVG } from '@/lib/dashboard';
import { ASSET_TYPE_LABELS } from '@/lib/enum-labels';
import type { SavingsSummary } from '@/lib/savings';
import { cn, formatKst } from '@/lib/utils';
import type {
  AssetItem,
  AssetsResponse,
  IncidentListItem,
  MetricsTimeseriesResponse,
} from '@/types/api';

/** §4.2 `낭비 후보만` — Rule이 낭비로 판정한 두 verdict. */
const WASTE_VERDICTS = ['COST_CANDIDATE', 'UNUSED'] as const;

const ALL = '전체';

export function AssetsView({
  data,
  incidentsByArn,
  metrics,
  savings,
  savingsFailed,
  openArn,
}: {
  data: AssetsResponse;
  /**
   * 인시던트는 자산 계약에 없다 — 목록 API의 `subject_arn` 역조인 결과다(§4.2·§4.3).
   * `null`은 **조회 실패**다. 0건과 구분해서 화면에 그대로 전달한다.
   */
  incidentsByArn: Record<string, IncidentListItem[]> | null;
  /**
   * CloudWatch 시계열 3축. **조회 실패면 null** — 자산 목록과 달리 화면을 죽이지 않는다:
   * 추이는 현재 상태를 읽는 데 필요한 것이 아니라 그 옆에 붙는 맥락이다(대시보드와 같은 규칙).
   */
  metrics: MetricsTimeseriesResponse | null;
  /** 절감 예상(AI 추정) 집계. 원천·한계는 `lib/savings` 머리말 참조 — 실 청구액이 아니다. */
  savings: SavingsSummary;
  /** 절감 예상을 위해 부른 인시던트 상세 중 실패한 건수. 0원과 구분해 카드에 적는다. */
  savingsFailed: number;
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
  // 탭을 상태로 쥔다 — 토폴로지 전용 유형(NACL 등)을 띠에서 눌렀을 때 빈 목록에 세워 두지 않고
  // 그 자산이 실제로 있는 토폴로지로 넘겨야 한다.
  const [tab, setTab] = useState('list');

  /**
   * v1.6 팀 회의 결정 — 목록 뷰에서 **판정 대상이 아닌 자산을 뺀다**(§4.2).
   *
   * **판정 기준은 `evaluation_status`이지 `resource_role`이 아니다.** 계약에서 두 값은 다른 축이다
   * (`packages/schemas/api/assets.py`):
   *
   * - `_PRIMARY_TYPES = {EC2, SG}` — **EBS는 `PRIMARY`가 아니다.** validator가 EBS의
   *   `resource_role`을 `RUNBOOK_SUPPORT`로 강제한다
   * - `_RULE_TARGET_TYPES = {EC2, SG, EBS}` — **그런데 EBS는 Rule 판정 대상이다.** `verdict`가
   *   나오고 `NOT_APPLICABLE`일 수 없다
   *
   * `resource_role`로 거르면 **미연결 EBS가 목록에서 사라진다** — 비용이 계속 청구되는 대표
   * 낭비 후보이자 `RUNBOOK_EBS_DELETE_UNATTACHED`의 실행 대상이라, 회의 결정("빌링이 없는 것을
   * 뺀다")과 정반대가 된다. `NOT_APPLICABLE`은 정확히 NACL·ASG·시작 템플릿·ALB 대상 그룹
   * 4종만 걸러 그 결정과 일치한다.
   *
   * 구 `주요 관제만` 토글은 이 규칙이 상시가 되면서 **제거**했다(켤 대상이 없다).
   *
   * **토폴로지 뷰가 서면(#146) 그쪽은 `data.items` 전량을 써야 한다** — 관계 6종 중 4종이 이
   * 노드 위에 그려지고 `RUNBOOK_NACL_ADD_DENY`의 대상도 NACL이다(§4.2).
   */
  const judgedItems = useMemo(
    () => data.items.filter(isJudgedAsset),
    [data.items],
  );
  const supportCount = data.items.length - judgedItems.length;

  /**
   * 토폴로지의 초점 집합 — 필터 3종(유형·리전·낭비 후보) 전부다. 초점은 **흐리게 하는 것이지
   * 빼는 것이 아니라서**(`asset-graph.tsx`) 노드를 지워 엣지가 끊기는 문제가 없다(#146).
   *
   * 유형도 초점 축에 넣는다(2026-09-18) — 지표 띠에서 `NACL`을 누른 사람은 그 유형이 그래프
   * 어디에 있는지를 보러 오는데, 종전처럼 유형을 초점에서 빼면 넘어간 토폴로지가 누르기 전과
   * 똑같아 **왜 넘어왔는지**가 사라진다. 노드는 그대로 두고 나머지만 흐려진다.
   *
   * 초점은 `data.items` **전량**에서 고른다 — 목록에서 뺀 지원 자산도 그래프에는 노드로 있고,
   * 리전만 걸었을 때 그 노드가 흐려지면 같은 리전인데 빠진 것처럼 읽힌다.
   */
  const focusedArns = useMemo(() => {
    if (assetType === ALL && region === ALL && !wasteOnly) return null;
    return new Set(
      data.items
        .filter((a) => {
          if (assetType !== ALL && a.asset_type !== assetType) return false;
          if (region !== ALL && a.region !== region) return false;
          if (wasteOnly && !WASTE_VERDICTS.some((v) => v === a.verdict)) return false;
          return true;
        })
        .map((a) => a.arn),
    );
  }, [data.items, assetType, region, wasteOnly]);

  const regions = useMemo(
    () => [...new Set(judgedItems.map((a) => a.region))].sort(),
    [judgedItems],
  );

  // 필터 3종 전부 클라이언트 필터다 — 계약에 Query Parameter가 없어 전량 응답에서 거른다(§4.2).
  const visible = useMemo(
    () =>
      judgedItems.filter((a) => {
        if (assetType !== ALL && a.asset_type !== assetType) return false;
        if (region !== ALL && a.region !== region) return false;
        if (wasteOnly && !WASTE_VERDICTS.some((v) => v === a.verdict)) return false;
        return true;
      }),
    [judgedItems, assetType, region, wasteOnly],
  );

  /**
   * 유형 지표 띠의 원천. 대시보드 「자산 인벤토리」 패널과 **같은 함수**를 쓴다(`lib/dashboard`) —
   * 세는 자리가 둘이면 두 화면이 서로 다른 EC2 대수를 말한다.
   */
  const inventory = useMemo(
    () => inventoryCounts(data.items, data.uncollected),
    [data.items, data.uncollected],
  );

  /**
   * 고른 유형이 **수집은 됐는데 목록에는 설 수 없는** 유형이면 그 행(NACL·ASG·시작 템플릿·대상 그룹).
   * 유형 목록을 상수로 또 적지 않고 데이터에서 센다 — 계약이 바뀌어도 화면이 따라간다.
   */
  const topologyOnlyRow =
    inventory.find((row) => row.type === assetType && row.count > 0 && row.judged === 0) ?? null;

  // 셀렉트 옵션도 띠와 같은 수를 쓴다 — 한 화면의 두 컨트롤이 다른 숫자를 말하면 어느 쪽도 못 믿는다.
  // 0건 유형은 고를 수 없게 뺀다(고르면 빈 화면밖에 안 나온다).
  const typeOptions = [
    { value: ALL, label: `${ALL} (${data.items.length})` },
    ...inventory
      .filter(({ count }) => count > 0)
      .map(({ type, count }) => ({
        value: type,
        label: `${ASSET_TYPE_LABELS[type]?.label ?? type} (${count})`,
      })),
  ];

  /** 타일 토글 — 이미 걸린 유형을 다시 누르면 전체로 돌아온다(`aria-pressed`와 같은 뜻). */
  const toggleType = (type: string) => setAssetType((prev) => (prev === type ? ALL : type));

  /**
   * 추이 카드가 그릴 자산 — **리전·낭비 후보 필터만 따른다. 유형 필터는 빼 둔다.**
   *
   * CPU·네트워크는 EC2 에만 있는 값이라, 유형을 `보안 그룹`으로 좁힌 순간 두 카드가 통째로
   * "관측치가 없습니다"가 된다. 그건 조회가 안 된 것으로 읽히지, 고른 유형에 그 메트릭이
   * 없다는 뜻으로는 읽히지 않는다. 필터가 곡선을 지우는 대신 **다른 카드에서 유형을 본다**.
   */
  const chartArns = useMemo(() => {
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

  /** 절감 예상 막대에서 자산으로 — 목록에 없는 자산이면 무시한다(토폴로지 전용 유형 등). */
  const openAsset = (arn: string) => {
    const asset = data.items.find((a) => a.arn === arn);
    if (asset === undefined) return;
    setSelected(asset);
    setDetailOpen(true);
  };

  return (
    <Tabs value={tab} onValueChange={setTab} className="gap-4">
      {/* 지표 띠 — 화면 맨 위. 전체 + 유형 7종이라 8칸이고, 2 → 4 → 8열로 접힌다(빈 칸이 없다). */}
      <MetricStrip className="sm:grid-cols-4 xl:grid-cols-8">
        <MetricTile
          label="전체 자산"
          value={data.items.length}
          note={`목록 대상 ${judgedItems.length}건`}
          onClick={() => setAssetType(ALL)}
          selected={assetType === ALL}
        />
        {inventory.map(({ type, count, judged, uncollectedReason }) => (
          <MetricTile
            key={type}
            label={ASSET_TYPE_LABELS[type]?.label ?? type}
            // 조회를 못 한 유형은 0으로 적지 않는다 — 0은 "없다"는 단언이고 여기서 사실은 "모른다"다.
            value={uncollectedReason === null ? count : null}
            note={
              uncollectedReason !== null
                ? `수집 실패 · ${uncollectedReason}`
                : count === 0
                  ? '수집된 자산 없음'
                  : judged === 0
                    ? '토폴로지 전용'
                    : '목록·판정 대상'
            }
            // 0건 유형은 눌러도 보여 줄 것이 없다 — 버튼으로 만들지 않는다.
            onClick={count === 0 ? undefined : () => toggleType(type)}
            selected={assetType === type}
          />
        ))}
      </MetricStrip>

      {/* 추이·비용 3열 — 띠(지금 몇 건)와 목록(무엇이) 사이에서 **그 수가 어디서 왔나**를 말한다.
          CPU·네트워크는 같은 CloudWatch 원계열이지만 단위가 %와 B/s라 한 격자에 겹치지 않는다.
          셋째 칸이 비용인 이유: 앞의 두 칸이 "이 자산이 한가한가"를 보이고, 그 답이 예라면
          다음 질문이 "그래서 얼마를 아끼나"이기 때문이다. */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Panel
          title="EC2 CPU 추이"
          description={`CloudWatch 원계열 — 파선(저활성 임계 ${
            metrics?.cpu.idle_cpu_avg_threshold ?? IDLE_CPU_AVG
          }%) 아래에 머무는 인스턴스가 다운사이징 후보입니다`}
        >
          {metrics === null ? (
            <Muted>추이를 불러오지 못했습니다.</Muted>
          ) : (
            <CpuTrendChart axis={metrics.cpu} arns={chartArns} />
          )}
        </Panel>

        <Panel
          title="네트워크 처리량 추이"
          description="수신·송신 합계(초당) — CPU가 낮은데 이 선이 살아 있으면 저활성 판정의 반례입니다"
        >
          {metrics === null ? (
            <Muted>추이를 불러오지 못했습니다.</Muted>
          ) : (
            <NetworkTrendChart axis={metrics.network} arns={chartArns} />
          )}
        </Panel>

        <Panel
          title="절감 예상 (AI 추정)"
          description="다운사이징 후보의 월 절감 예상액 — 모델 추정 단가 × 730시간이며 실제 청구액이 아닙니다"
        >
          <SavingsCard summary={savings} failed={savingsFailed} onSelect={openAsset} />
        </Panel>
      </div>

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
        {/* 셈은 **탭마다 분모가 다르다.** 목록은 판정 대상만 담고(13건), 토폴로지는 전량을 그린다
            (16건). 목록 기준을 그대로 두면 지표 띠에서 `NACL`을 누르고 토폴로지로 넘어간 화면이
            노드 2개를 밝혀 놓고 `0 / 13건`이라고 적는다 — 보이는 것과 적힌 것이 어긋난다. */}
        {tab === 'list' ? (
          <>
            <span aria-live="polite">
              {visible.length === judgedItems.length
                ? `${judgedItems.length}건`
                : `${visible.length} / ${judgedItems.length}건`}
            </span>
            {/* 응답에 있었는데 화면에 없는 것이 있으면 그 사실을 말한다 — 숨긴 줄 모르면
                "수집이 안 된 것"으로 읽힌다(§4.2). */}
            {supportCount > 0 ? (
              <span>판정 비대상 {supportCount}건 제외 (NACL·ASG·시작 템플릿·대상 그룹 · 토폴로지에는 남는다)</span>
            ) : null}
          </>
        ) : (
          <span aria-live="polite">
            {focusedArns === null
              ? `${data.items.length}건`
              : `초점 ${focusedArns.size} / ${data.items.length}건`}
          </span>
        )}
      </div>

      <TabsContent value="list">
        {visible.length === 0 ? (
          topologyOnlyRow !== null ? (
            // 띠에서 누른 유형이 목록에 설 수 없는 경우다. "없습니다"로 끝내면 수집 누락으로
            // 읽히므로, 왜 없는지와 어디서 볼 수 있는지를 함께 준다(§4.9 빈 상태는 오류가 아니다).
            <EmptyState
              message={`${ASSET_TYPE_LABELS[topologyOnlyRow.type]?.label ?? topologyOnlyRow.type} 자산 ${topologyOnlyRow.count}건은 목록에 서지 않습니다.`}
              description="NACL · Auto Scaling 그룹 · 시작 템플릿 · 대상 그룹은 Rule 판정 대상이 아니라 명함 카드가 없습니다 — 토폴로지에서 관계로 봅니다."
              action={
                <Button size="sm" variant="outline" onClick={() => setTab('topology')}>
                  토폴로지에서 보기
                </Button>
              }
            />
          ) : (
            <EmptyState
              message="조건에 맞는 자산이 없습니다."
              description="필터를 바꾸면 다른 자산을 볼 수 있습니다."
            />
          )
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
          uncollected={data.uncollected}
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
        metrics={metrics}
        open={detailOpen}
        onOpenChange={setDetailOpen}
      />
    </Tabs>
  );
}

/** 카드 안에서 값 대신 쓰는 한 줄. 대시보드의 같은 자리와 무게를 맞춘다. */
function Muted({ children }: { children: React.ReactNode }) {
  return <p className="text-muted-foreground py-4 text-center text-sm">{children}</p>;
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
