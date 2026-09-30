'use client';

// SEC-001 보안 관제 본체 — 자산 관제(AST-001, `assets/assets-view.tsx`)의 틀을 그대로 가져와 축만 보안으로
// 바꿨다(2026-09-28). 위에서 아래로 같은 순서다: 지표 띠 → 상태 행 → 추이 행 → 목록⇄토폴로지⇄인시던트.
//
//   자산 관제                              보안 관제
//   유형 지표 띠(전체 + 유형 7종)           보안 지표 띠(보안 그룹 · 개방 · 영향 EC2 · 위협 · 미조치 · NACL)
//   헬스 스코어 │ 판정 현황 │ 절감 예상      인터넷 개방 보안 그룹(2칸) │ 보안 인시던트 현황
//   EC2 CPU 추이 │ 네트워크 추이            개방 SG 추이 │ 외부 위협 출발지
//   목록 ⇄ 토폴로지 ⇄ 인시던트(FinOps)     목록 ⇄ 토폴로지(공격 경로 포함) ⇄ 인시던트(SecOps)
//
// ## 띠의 칸은 전부 필터다 — 칸 하나 = 자산 집합 하나
//
// 자산 관제의 유형 타일처럼 **누르면 그 칸이 가리키는 자산만 남고, 다시 누르면 기본으로 돌아온다.**
// 기본은 `보안 그룹` 칸이다(자산 관제의 `전체 자산`에 해당). 칸마다 집합은 이렇다.
//
//   보안 그룹        기본 — 목록은 보안 그룹 전량, 토폴로지·인시던트는 자산 전량(초점 없음)
//   인터넷 개방      전체 대역에 열린 보안 그룹
//   영향 EC2         개방 SG를 `SECURED_BY`로 가리키는 EC2
//   위협 판정 자산   `verdict = THREAT`
//   미조치 보안 인시던트  미조치(분석·승인 대기·조치 중) SecOps 인시던트의 대상 자산 — 칸의 숫자는 건수,
//                    집합은 그 건들이 걸린 자산이다
//   NACL             NACL 전량 — 명함 카드가 없어 목록 대신 토폴로지로 안내한다(자산 관제와 같다)
//
// 세 탭이 **같은 집합**을 본다: 목록은 그 집합의 판정 대상 카드, 토폴로지는 그 집합을 밝히고 나머지를
// 흐리며(빼지 않는다 — asset-graph.tsx), 인시던트 탭은 `subject_arn`이 그 집합에 든 건만 남긴다. 리전
// 필터는 그 위에 겹친다. 띠의 숫자는 수집 전량이라 필터를 먹이지 않는다(자산 관제와 같은 이유).
// 셀렉트 `초점`은 띠와 같은 여섯 값이다 — 띠가 화면 위에 붙어 있어도 필터 줄에서 지금 걸린 것을 읽고
// 바꿀 수 있어야 한다(자산 관제의 유형 셀렉트와 같은 이유).
//
// 이름(h1)과 띠는 한 덩이로 `sticky`다 — 자산 관제와 같은 틀.
//
// 구 보안 인시던트 목록(INC-001 `/incidents`)은 세 번째 탭 `인시던트`다(`incidents/incidents-view.tsx`).
// SSH 무차별 대입은 EC2에, 전체 개방은 SG에 걸리므로 `영향 EC2`·`인터넷 개방` 칸이 둘을 가른다.

import { useMemo, useState } from 'react';

import { AssetCard } from '@/components/assets/asset-card';
import { AssetDetail } from '@/components/assets/asset-detail';
import { AssetGraph } from '@/components/assets/asset-graph';
import { ThreatActivityChart } from '@/components/dashboard/trend-charts';
import { EmptyState } from '@/components/empty-state';
import { ErrorState } from '@/components/error-state';
import { FilterSelect } from '@/components/filter-select';
import { IncidentsView } from '@/components/incidents/incidents-view';
import { MetricStrip, MetricTile, type MetricTone } from '@/components/metric-strip';
import { Panel } from '@/components/panel';
import { Muted } from '@/components/panel-parts';
import {
  OpenSgPanel,
  SecurityIncidentStatusList,
  ThreatSourcePanel,
} from '@/components/security/security-summary-panels';
import { StatusBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { isJudgedAsset } from '@/lib/asset-filter';
import type { IncidentPreset } from '@/lib/incident-filter';
import {
  focusAssets,
  focusScope,
  SECURITY_FOCUSES as FOCUSES,
  securityMetrics,
  type SecurityFocus as Focus,
} from '@/lib/security';
import { threatPaths } from '@/lib/threat-path';
import { formatKst } from '@/lib/utils';
import type {
  AssetItem,
  AssetsResponse,
  IncidentListItem,
  MetricsTimeseriesResponse,
} from '@/types/api';

const ALL = '전체';

/** 칸의 표기. 집합의 정의는 `lib/security`의 `focusAssets`가 쥔다 — 여기는 이름·설명·색뿐이다. */
const FOCUS_LABEL: Record<Focus, { label: string; note: string; tone?: MetricTone }> = {
  SG: { label: '보안 그룹', note: '수집된 보안 그룹 전량' },
  OPEN_SG: { label: '인터넷 개방', note: '전체 대역 인바운드 허용', tone: 'warn' },
  AFFECTED_EC2: { label: '영향 EC2', note: '개방 SG 뒤의 인스턴스', tone: 'warn' },
  THREAT: { label: '위협 판정 자산', note: '규칙 엔진 위협 판정', tone: 'danger' },
  UNHANDLED: { label: '미조치 보안 인시던트', note: '분석·승인 대기·조치 중', tone: 'warn' },
  NACL: { label: 'NACL', note: '차단 런북 대상' },
};

export function SecurityView({
  data,
  incidents,
  incidentsError,
  incidentsByArn,
  metrics,
  openArn,
  initialTab,
  initialPreset,
}: {
  data: AssetsResponse;
  /** 인시던트 목록 전량. `null`은 조회 실패 — 0건과 구분해 패널마다 그 사실을 적는다. */
  incidents: IncidentListItem[] | null;
  /** 조회 실패의 오류 원본 — 인시던트 탭이 CMN-002 인라인으로 그린다. 성공이면 null. */
  incidentsError: unknown;
  /** 목록 API의 `subject_arn` 역조인(자산 관제와 같다). `null`은 조회 실패. */
  incidentsByArn: Record<string, IncidentListItem[]> | null;
  /** CloudWatch 시계열 — 여기서는 `sg_exposure` 축과 상세 Drawer의 스파크라인이 쓴다. 실패면 null. */
  metrics: MetricsTimeseriesResponse | null;
  /** `?asset=<arn>` 딥링크 — 그 자산의 상세를 연 채로 시작한다. 목록에 없으면 무시한다. */
  openArn?: string;
  /** URL `?tab=` — 인시던트 탭 딥링크(구 `/incidents` redirect). 모르는 값은 목록이다. */
  initialTab?: string;
  /** URL `?preset=` — 인시던트 탭의 첫 프리셋. */
  initialPreset?: IncidentPreset;
}) {
  const [focus, setFocus] = useState<Focus>('SG');
  const [region, setRegion] = useState<string>(ALL);
  const [tab, setTab] = useState(
    initialTab === 'topology' || initialTab === 'incidents' ? initialTab : 'list',
  );
  // AST-002 Drawer는 자산 관제와 같은 규칙으로 연다 — 신규 페치 없음, 닫아도 selected는 비우지 않는다.
  const linked = openArn ? (data.items.find((a) => a.arn === openArn) ?? null) : null;
  const [selected, setSelected] = useState<AssetItem | null>(linked);
  const [detailOpen, setDetailOpen] = useState(linked !== null);

  /** 타일 토글 — 걸린 칸을 다시 누르면 기본(`보안 그룹`)으로 돌아온다(`aria-pressed`와 같은 뜻). */
  const toggleFocus = (f: Focus) => setFocus((prev) => (prev === f ? 'SG' : f));

  // 인시던트 탭의 몫 — 이 화면은 보안 인시던트(SECOPS)만 담는다. 자산(FINOPS)은 자산 관제의 탭이다.
  const secopsIncidents = useMemo(
    () => (incidents === null ? null : incidents.filter((i) => i.category === 'SECOPS')),
    [incidents],
  );

  /** 초점 칸이 가리키는 자산 집합(파일 머리말 표). 리전은 아직 안 걸었다. */
  const focusItems = useMemo(
    () => focusAssets(data.items, incidents, focus),
    [data.items, incidents, focus],
  );

  // 목록 — 초점 집합의 판정 대상만. NACL은 명함 카드가 없어 여기서 빠진다(아래 빈 상태가 안내한다).
  const visible = useMemo(
    () => focusItems.filter((a) => (region === ALL || a.region === region) && isJudgedAsset(a)),
    [focusItems, region],
  );

  /**
   * 토폴로지 초점·인시던트 탭 범위 — 기본 칸은 자산 전량, 다른 칸은 그 칸의 집합이다(`focusScope`).
   * 초점은 흐리게 하는 것이지 빼는 것이 아니다(asset-graph.tsx). 아무것도 안 걸렸으면 null.
   */
  const focusedArns = useMemo(
    () => focusScope(data.items, incidents, focus, region === ALL ? null : region),
    [data.items, incidents, focus, region],
  );

  const regions = useMemo(
    () => [...new Set(data.items.map((a) => a.region))].sort(),
    [data.items],
  );
  const paths = useMemo(() => threatPaths(incidents), [incidents]);
  const summary = securityMetrics(data.items, incidents);

  /** 칸의 값 — 띠와 셀렉트가 같은 수를 말해야 한다. */
  const focusValue: Record<Focus, number | null> = {
    SG: summary.sgTotal,
    OPEN_SG: summary.openSg,
    AFFECTED_EC2: summary.affectedEc2,
    THREAT: summary.threat,
    UNHANDLED: summary.unhandled,
    NACL: summary.nacl,
  };

  const select = (asset: AssetItem) => {
    setSelected(asset);
    setDetailOpen(true);
  };

  return (
    <Tabs value={tab} onValueChange={setTab} className="gap-4">
      {/* 화면 이름 + 지표 띠 — 한 덩이로 스크롤을 따라온다(자산 관제와 같은 틀 · 그쪽 주석 참조).
          띠는 6칸이라 3 → 6열로 접힌다(빈 칸이 없다). 칸은 전부 필터다(파일 머리말). */}
      <div className="bg-background sticky top-0 z-20 -mx-6 -mt-6 px-6 pt-6 pb-3">
      <h1 className="mb-3 text-lg font-semibold">보안 관제</h1>
      <MetricStrip className="sm:grid-cols-3 xl:grid-cols-6">
        {FOCUSES.map((f) => {
          const value = focusValue[f];
          const { label, note, tone } = FOCUS_LABEL[f];
          return (
            <MetricTile
              key={f}
              label={label}
              value={value}
              // 조회를 못 한 값은 `—`이고 눌러도 보여 줄 것이 없다 — 버튼으로 만들지 않는다.
              note={f === 'UNHANDLED' && value === null ? '인시던트 조회 실패' : note}
              tone={tone}
              onClick={value === null ? undefined : () => toggleFocus(f)}
              selected={focus === f}
            />
          );
        })}
      </MetricStrip>
      </div>

      {/* 요약 행 — 한 줄 4칸(2026-09-30). 전체 위협 추이가 2칸으로 가장 크고 맨 앞이다 — 이 화면에서 먼저
          읽을 것은 "위협이 얼마나 들어왔고 얼마가 남았나"다. 그 옆에 어느 SG가·무엇이 열렸나(개방 SG + 노출
          포트)와 누가·어느 대역이 들어왔나(외부 위협 출발지)가 1칸씩 선다. 전체 위협 박스는 2칸이라 넓은
          화면에서는 추이 옆에 현황을 나란히 두고, 좁아지면 아래로 쌓는다. */}
      <div className="grid gap-4 lg:grid-cols-4">
        <Panel
          className="lg:col-span-2"
          title="전체 위협 추이"
          description="위협 판정 자산(선)과 외부 위협 이벤트 발생(막대) · 보안 인시던트 처리 현황"
        >
          <div className="grid gap-4 xl:grid-cols-5">
            <div className="xl:col-span-3">
              {metrics === null ? (
                <Muted>추이를 불러오지 못했습니다.</Muted>
              ) : (
                <ThreatActivityChart assetStatus={metrics.asset_status} threatEvents={metrics.threat_events} />
              )}
            </div>
            <div className="border-t pt-3 xl:col-span-2 xl:border-t-0 xl:border-l xl:pt-0 xl:pl-4">
              <SecurityIncidentStatusList incidents={incidents} />
            </div>
          </div>
        </Panel>
        <OpenSgPanel items={data.items} onSelect={select} />
        <ThreatSourcePanel paths={paths} items={data.items} />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <TabsList>
          <TabsTrigger value="list">목록</TabsTrigger>
          <TabsTrigger value="topology">토폴로지</TabsTrigger>
          <TabsTrigger value="incidents">인시던트</TabsTrigger>
        </TabsList>

        <div className="flex flex-wrap items-center gap-3">
          {/* 셀렉트도 띠와 같은 수를 쓴다 — 한 화면의 두 컨트롤이 다른 숫자를 말하면 어느 쪽도 못 믿는다. */}
          <FilterSelect
            label="초점"
            value={focus}
            options={FOCUSES.map((f) => ({
              value: f,
              label: `${FOCUS_LABEL[f].label} (${focusValue[f] ?? '—'})`,
            }))}
            onChange={(v) => setFocus(v as Focus)}
          />
          <FilterSelect
            label="리전"
            value={region}
            options={[{ value: ALL, label: ALL }, ...regions.map((r) => ({ value: r, label: r }))]}
            onChange={setRegion}
          />
        </div>
      </div>

      {/* collection_status가 READY면 배지를 그리지 않는다(§3.2). 셈은 탭마다 분모가 다르다 — 목록은
          초점 집합의 판정 대상, 토폴로지는 전량이다(자산 관제와 같은 이유). */}
      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
        <StatusBadge field="collection_status" value={data.collection_status} />
        <span>갱신 {formatKst(data.last_collected_at)}</span>
        {tab === 'list' ? (
          <>
            <span aria-live="polite">
              {focus === 'SG' && region === ALL
                ? `보안 그룹 ${visible.length}건`
                : `${FOCUS_LABEL[focus].label} ${visible.length}건`}
            </span>
            {focus === 'SG' ? <span>EC2 · EBS 카드는 자산 관제에 있습니다</span> : null}
          </>
        ) : tab === 'topology' ? (
          <span aria-live="polite">
            {focusedArns === null
              ? `${data.items.length}건`
              : `초점 ${focusedArns.size} / ${data.items.length}건`}
          </span>
        ) : (
          // 건수는 탭 안의 목록이 프리셋 기준으로 센다 — 여기서는 필터가 걸린다는 사실만 말한다.
          <span>보안 인시던트(SecOps) — 위 초점·리전 필터가 이 탭에도 걸립니다</span>
        )}
      </div>

      <TabsContent value="list">
        {visible.length === 0 ? (
          focus === 'NACL' && focusItems.length > 0 ? (
            // 띠에서 누른 칸이 목록에 설 수 없는 경우다. "없습니다"로 끝내면 수집 누락으로
            // 읽히므로, 왜 없는지와 어디서 볼 수 있는지를 함께 준다(자산 관제와 같은 빈 상태).
            <EmptyState
              message={`NACL 자산 ${focusItems.length}건은 목록에 서지 않습니다.`}
              description="NACL은 Rule 판정 대상이 아니라 명함 카드가 없습니다 — 토폴로지에서 관계로 봅니다."
              action={
                <Button size="sm" variant="outline" onClick={() => setTab('topology')}>
                  토폴로지에서 보기
                </Button>
              }
            />
          ) : (
            <EmptyState
              message="조건에 맞는 자산이 없습니다."
              description="띠의 다른 칸을 누르거나 리전을 바꾸면 다른 자산을 볼 수 있습니다."
            />
          )
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
            {visible.map((asset) => (
              <AssetCard
                key={asset.arn}
                asset={asset}
                incidentCount={incidentsByArn === null ? null : (incidentsByArn[asset.arn] ?? []).length}
                onSelect={select}
              />
            ))}
          </div>
        )}
      </TabsContent>

      <TabsContent value="topology">
        {/* 전량을 그린다 — 노드를 빼면 엣지의 도착 노드가 사라져 그래프가 끊어진 것처럼 보인다.
            공격 경로는 대시보드(DSH-001)와 같은 파생이다(`lib/threat-path`). */}
        <AssetGraph
          items={data.items}
          uncollected={data.uncollected}
          focusedArns={focusedArns}
          threatPaths={paths}
          onSelect={select}
        />
      </TabsContent>

      <TabsContent value="incidents">
        {/* 조회 실패는 대기 0건이 아니라 오류다 — 인라인으로 강제한다(주변 화면이 살아 있다). */}
        {secopsIncidents === null ? (
          <ErrorState error={incidentsError} variant="inline" />
        ) : (
          <IncidentsView
            items={secopsIncidents}
            assets={data.items}
            initialPreset={initialPreset}
            showPreemptive
            subjectArns={focusedArns}
          />
        )}
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
