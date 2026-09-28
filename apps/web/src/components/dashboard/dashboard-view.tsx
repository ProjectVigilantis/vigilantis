// DSH-001 메인 대시보드 본문 — 구성은 PR #299가 채택한 5지표 + 카드 구성이고, 값은 전부 계약 필드에서
// 파생한다(lib/dashboard). 서버 컴포넌트다 — 실시간 이벤트가 오면 RealtimeProvider의 `router.refresh()`가
// 이 트리를 다시 그려 숫자가 따라온다.
//
// AI 조치 제안 카드(`action-proposal-card.tsx`)는 페이지가 `proposalSlot`으로 넘기고 이 격자가 자리를 준다.
// 토폴로지의 외부 출발지 노드·공격 경로는 인시던트 목록 계약의 `threat_context`에서 온다(#362 · PR #374) —
// 그래서 이 컴포넌트가 이미 들고 있는 `incidents`를 토폴로지 카드로 그대로 내려보낸다.
//
// ## 배치 — 9칸 | 3칸 두 열 (`xl` 이상)
//
// 전부 전폭으로 쌓으면 한 화면에 지표밖에 안 들어와 관제자가 스크롤로 상태를 재구성해야 한다.
// 왼쪽 9칸은 **보는 것**, 오른쪽 3칸은 **누르는 것과 그 근거**로 가른다.
//
//   상태줄 (테두리 없는 얇은 캡션 — 격자 밖 맨 위)
//   ┌ 지표 5종 띠 ─────────────────────────┬ AI 조치 제안 ────┐
//   │ 인벤토리(4) │ 헬스 스코어(4) │ 판정(4)   │ 인터넷 개방 SG    │
//   │ 추이 2축(CPU · 개방 SG) ──────────────┤                  │
//   └───────────────────────────────┴───────────────┘
//   ┌ 자산 토폴로지 (전폭 — 격자 밖) ─────────────────────────┐
//   │ 그래프 │ 인스턴스 목록 │ 트래픽 경로 밖                    │
//   └──────────────────────────────────────────────┘
//
// **두 열은 각자 쌓는다.** 맞추는 것은 윗선 하나 — 두 열의 첫 카드(지표 띠 · AI 제안) 윗변이다.
// 그 아래로는 각자 제 내용 길이대로 붙는다. 그래서 왼쪽의 세 덩이(지표 띠 → 집계 3열 → 추이)는
// 오른쪽 AI 카드가 아무리 길어도 서로 붙어 있고, 왼쪽 것들의 폭은 열 하나를 공유하니 저절로 같다.
//
// **토폴로지만 격자 밖 전폭이다.** 그 카드는 안에서 다시 세 칸으로 갈라지는데(그래프 │ 인스턴스 │
// 경로 밖), 9칸 열 안에 두면 목록 두 칸을 빼고 그래프에 900px도 남지 않아 5열 정렬이 눌린다.
//
// 조각들을 한 격자에 평평하게 늘어놓으면(= 9·3·9·3을 auto-placement에 맡기면) 둘째 덩이부터
// 행 높이에 묶여 위아래 사이가 벌어진다. **상태줄과 토폴로지가 격자 밖**인데, 상태줄은 왼쪽 열
// 안에 두면 지표 띠가 그 높이만큼 내려앉아 맞춰야 할 윗선 하나가 어긋나기 때문이고, 토폴로지는
// 폭 때문이다(바로 위 문단).
//
// **집계 3열은 균등이다(4 : 4 : 4).** 셋 다 `이름 + 숫자 + 막대` 한 줄짜리라 같은 폭이면 된다.
// 종전에는 헬스 스코어가 넓어야 해서 4 : 5 : 3이었는데, 그 카드에서 판정 배지를 뺀 뒤로 이름 옆에
// 자리를 다투는 것이 없어졌다 — 폭이 갈리면 나란히 선 세 카드의 막대 길이를 서로 견줄 수 없다.
//
// `xl` 미만에서는 한 열로 접힌다. 이때는 왼쪽 열이 통째로 먼저 와서 AI 제안이 집계 3열 아래로
// 밀린다 — 두 열을 각자 쌓는 대가다. 토폴로지 그래프의 열 전환 기준은 뷰포트가 아니라 **그래프가
// 실제로 받은 폭**이다(`asset-graph.tsx`의 `@container/graph`) — 자산 화면(AST-001)과 폭이 달라서다.

import { DashboardTopology } from '@/components/dashboard/dashboard-topology';
import { HealthScoreList } from '@/components/dashboard/health-score-list';
import { EmptyState } from '@/components/empty-state';
import { MetricStrip, MetricTile } from '@/components/metric-strip';
import { Panel } from '@/components/panel';
import { StatusBadge } from '@/components/status-badge';
import {
  dashboardMetrics,
  exposureRows,
  healthSummary,
  IDLE_CPU_AVG,
  inventoryCounts,
  verdictCounts,
} from '@/lib/dashboard';
import { ASSET_TYPE_LABELS, NO_VALUE, VERDICT_LABELS } from '@/lib/enum-labels';
import { cn, formatKst } from '@/lib/utils';
import type {
  AssetsResponse,
  IncidentListItem,
  MetricsTimeseriesResponse,
  Verdict,
} from '@/types/api';

import { CpuTrendChart, SgExposureTrendChart } from './trend-charts';

function StatLine({ label, value }: { label: string; value: number }) {
  return (
    <div className="mt-3 flex items-center justify-between border-t pt-3 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono tabular-nums">{value}</span>
    </div>
  );
}

function Muted({ children }: { children: React.ReactNode }) {
  return <p className="text-muted-foreground py-4 text-center text-sm">{children}</p>;
}

const VERDICT_BAR: Record<Verdict, string> = {
  THREAT: 'bg-danger',
  COST_CANDIDATE: 'bg-amber-400',
  UNUSED: 'bg-amber-400',
  SKIP: 'bg-muted-foreground/50',
};

function Bar({ ratio, className }: { ratio: number; className: string }) {
  return (
    <span className="bg-muted block h-1.5 overflow-hidden rounded-full">
      <span className={cn('block h-full rounded-full', className)} style={{ width: `${ratio * 100}%` }} />
    </span>
  );
}

export function DashboardView({
  assets,
  incidents,
  metrics: timeseries,
  proposalSlot,
}: {
  assets: AssetsResponse;
  /** 인시던트 조회 실패면 null — 자산 화면과 같은 규칙으로 0건과 구분한다. */
  incidents: IncidentListItem[] | null;
  /**
   * 시계열 2축. **조회 실패면 null** — 인시던트와 같은 규칙이다. 자산과 달리 화면 전체를
   * 죽이지 않는다: 추이는 현재 상태를 읽는 데 필요한 것이 아니라 그 옆에 붙는 맥락이다.
   */
  metrics: MetricsTimeseriesResponse | null;
  /**
   * 「AI 조치 제안」 카드 — 페이지가 서버에서 그려 넘긴다(`app/page.tsx`).
   * 이 컴포넌트는 **자리만** 준다. 카드를 여기서 만들면 그 안의 CMN-002(`ErrorState`)가
   * RSC 경계를 넘으며 `ApiError`의 code·requestId를 잃는다.
   */
  proposalSlot: React.ReactNode;
}) {
  const items = assets.items;
  const metrics = dashboardMetrics(items, incidents);
  const inventory = inventoryCounts(items, assets.uncollected);
  const exposure = exposureRows(items);
  const health = healthSummary(items);
  const verdicts = verdictCounts(items);

  return (
    <div className="flex flex-col gap-4">
      {/* 상태줄은 격자 밖 맨 위다 — 테두리 없는 얇은 캡션이라 왼쪽 열 안에 두면 지표 띠가 그
          높이만큼 내려앉아 오른쪽 AI 카드와 **윗선이 어긋난다.**
          collection_status가 READY면 배지를 그리지 않는다(§3.2) — 자산 화면과 같은 줄이다. */}
      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
        <StatusBadge field="collection_status" value={assets.collection_status} />
        <span>마지막 수집 {formatKst(assets.last_collected_at)}</span>
        <span>자산 {items.length}건</span>
        <span>{incidents === null ? '인시던트 조회 실패' : `인시던트 ${incidents.length}건`}</span>
      </div>

      {/* 두 열은 **각자 쌓는다.** 윗선은 두 열의 첫 카드(지표 띠 · AI 제안)끼리 맞고, 그 아래로는
          각자 제 내용 길이대로 붙는다 — 오른쪽 AI 카드가 길어도 왼쪽 집계 3열은 지표 띠 바로
          아래에 온다. 네 조각을 한 격자에 평평하게 넣으면 그 자리가 행 높이에 묶여 빈 채로 남는다. */}
      <div className="grid gap-4 xl:grid-cols-12">
        {/* 왼쪽 — 보는 것. 지표 띠 + 집계 3열. */}
        <div className="flex flex-col gap-4 xl:col-span-9">
          <MetricStrip className="lg:grid-cols-5">
            <MetricTile label="전체 자산" value={metrics.total} note="수집된 자산 전량" />
            <MetricTile
              label="인터넷 개방 보안 그룹"
              value={metrics.openSg}
              note="전체 대역 인바운드 허용"
              tone="warn"
            />
            <MetricTile label="위협 판정 자산" value={metrics.threat} note="규칙 엔진 위협 판정" tone="danger" />
            <MetricTile
              label="미조치 인시던트"
              value={metrics.unhandled}
              note={metrics.unhandled === null ? '인시던트 조회 실패' : '분석·승인 대기·조치 중'}
              tone="warn"
            />
            {/* 홀수(5종)라 2열에서 마지막 칸이 빈다 — 칸 사이를 `gap-px` 테두리로 그리는 띠라
                빈 칸이 색 덩어리로 보인다. 마지막만 2칸을 먹여 줄을 채운다. */}
            <MetricTile
              label="낭비 후보"
              value={metrics.waste}
              note="최적화 후보 + 미사용"
              tone="ok"
              className="col-span-2 lg:col-span-1"
            />
          </MetricStrip>

          {/* 집계 3열 — 지표 띠 바로 아래에 붙는다. 폭을 균등하게 주지 않는다: 자산 인벤토리는
              `유형 이름 + 숫자` 한 줄짜리라 좁아도 읽히고, 헬스 스코어는 인스턴스 이름과
              막대·임계선을 한 줄에 담아 가장 넓어야 하며, 판정 현황은 짧은 판정명과 막대뿐이다.
              4 : 5 : 3으로 나눈 근거다. */}
          <div className="grid gap-4 md:grid-cols-12">
            <Panel
              className="md:col-span-4"
              title="자산 인벤토리"
              description="유형 7종 — 0건도 남겨 수집 누락과 구분합니다"
            >
              <ul className="flex flex-col text-sm">
                {inventory.map(({ type, count, uncollectedReason }) => (
                  <li key={type} className="flex items-center justify-between gap-2 border-b py-2">
                    <span className="text-muted-foreground">{ASSET_TYPE_LABELS[type]?.label ?? type}</span>
                    {/* 조회를 못 한 유형은 0으로 적지 않는다 — 0은 "없다"는 단언이고 여기서 사실은
                        "모른다"다. 이 패널이 0건 유형을 남기는 목적이 수집 누락과의 구분인데,
                        셈만으로는 그 구분이 서지 않았다. 사유는 AWS 오류 코드 원문 그대로 툴팁에. */}
                    {uncollectedReason !== null ? (
                      <span className="text-xs text-amber-400" title={`수집 실패: ${uncollectedReason}`}>
                        수집 실패
                      </span>
                    ) : (
                      <span className={cn('font-mono tabular-nums', count === 0 && 'text-muted-foreground')}>
                        {count}
                      </span>
                    )}
                  </li>
                ))}
                <li className="flex items-center justify-between pt-2 font-medium">
                  <span>합계</span>
                  <span className="font-mono tabular-nums">{items.length}</span>
                </li>
              </ul>
            </Panel>

            <Panel
              className="md:col-span-4"
              title="헬스 스코어"
              description={`EC2 ${health.ec2Total}대 · 임계선 ${IDLE_CPU_AVG} 미만은 저활성(스펙 조정 후보)`}
            >
              {health.ec2Total === 0 ? (
                <Muted>EC2 자산이 없습니다.</Muted>
              ) : health.scored.length === 0 ? (
                <Muted>{NO_VALUE} 확인 불가</Muted>
              ) : (
                <HealthScoreList scored={health.scored} threshold={IDLE_CPU_AVG} />
              )}
              {health.ec2Total > 0 ? <StatLine label="확인 불가 (점수 없는 EC2)" value={health.unknown} /> : null}
            </Panel>

            <Panel
              className="md:col-span-4"
              title="판정 현황"
              description={`판정 대상 ${verdicts.judged}건 (NACL · Auto Scaling 그룹 · 시작 템플릿 · 대상 그룹 제외)`}
            >
              <ul className="flex flex-col gap-3 text-xs">
                {verdicts.byVerdict.map(({ verdict, count }) => (
                  <li key={verdict} className="flex flex-col gap-1">
                    <span className="flex items-center justify-between">
                      <span className="text-muted-foreground">{VERDICT_LABELS[verdict]?.label ?? verdict}</span>
                      <span className="font-mono tabular-nums">{count}</span>
                    </span>
                    <Bar ratio={verdicts.judged === 0 ? 0 : count / verdicts.judged} className={VERDICT_BAR[verdict]} />
                  </li>
                ))}
                <li className="flex flex-col gap-1">
                  <span className="flex items-center justify-between">
                    <span className="text-muted-foreground">판정 대기·실패</span>
                    <span className="font-mono tabular-nums">{verdicts.pending}</span>
                  </span>
                  <Bar
                    ratio={verdicts.judged === 0 ? 0 : verdicts.pending / verdicts.judged}
                    className="bg-muted-foreground/30"
                  />
                </li>
              </ul>
              <StatLine label="판정 불가 (데이터 부족 · 대기 · 실패)" value={verdicts.undecidable} />
            </Panel>
          </div>

          {/* 추이 2축 — 집계(현재)와 토폴로지(구조) 사이다. 위 지표 띠가 "지금 몇 건"을
              말하고 여기가 "그 수가 어디서 왔나"를 말한다. 두 차트를 나란히 두되 **한 격자에
              겹치지 않는다** — 단위가 %와 건수로 달라 y축을 공유할 수 없다. */}
          <div className="grid gap-4 md:grid-cols-2">
            <Panel
              title="EC2 CPU 추이"
              description={`CloudWatch 원계열(1시간 입자) — 파선(저활성 임계 ${
                timeseries?.cpu.idle_cpu_avg_threshold ?? IDLE_CPU_AVG
              }%) 아래에 머무는 인스턴스가 다운사이징 후보입니다`}
            >
              {timeseries === null ? (
                <Muted>추이를 불러오지 못했습니다.</Muted>
              ) : (
                <CpuTrendChart axis={timeseries.cpu} />
              )}
            </Panel>

            <Panel
              title="인터넷 개방 보안 그룹 추이"
              description="수집 회차별 위협 판정 건수 — 조치가 반영되면 선이 내려갑니다"
            >
              {timeseries === null ? (
                <Muted>추이를 불러오지 못했습니다.</Muted>
              ) : (
                <SgExposureTrendChart axis={timeseries.sg_exposure} />
              )}
            </Panel>
          </div>

        </div>

        {/* 오른쪽 — 누르는 것과 그 근거. 개방 SG가 AI 카드 바로 아래인 이유: 위 지표 띠도 같은
            위협을 세지만 거기는 **건수**뿐이고, 여기는 어느 SG의 어느 규칙이 몇 대에 걸리는지를
            준다 — 승인 버튼을 누르기 전에 볼 것이다. */}
        <div className="flex flex-col gap-4 xl:col-span-3">
          {proposalSlot}

          <Panel
            title="인터넷 개방 보안 그룹"
            description="전체 대역(0.0.0.0/0 · ::/0)에 열린 인바운드"
          >
            {exposure.length === 0 ? (
              <Muted>인터넷에 열린 보안 그룹이 없습니다.</Muted>
            ) : (
              <ul className="flex flex-col gap-3">
                {exposure.map(({ sg, rules, affectedEc2 }) => (
                  <li key={sg.arn} className="flex flex-col gap-1 border-b pb-3 last:border-b-0 last:pb-0">
                    <span className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-mono text-xs">{sg.name ?? sg.resource_id}</span>
                      {/* verdict가 없으면 판정 대기·실패다 — 사유를 evaluation_status로 적는다. */}
                      {sg.verdict !== null ? (
                        <StatusBadge field="verdict" value={sg.verdict} />
                      ) : (
                        <StatusBadge field="evaluation_status" value={sg.evaluation_status} />
                      )}
                    </span>
                    <span className="text-muted-foreground font-mono text-xs">{rules.join(', ')}</span>
                    <span className="text-muted-foreground text-xs">영향 EC2 {affectedEc2}대</span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
      </div>

      {/* 토폴로지 — **두 열 밖, 전폭이다.** 안에는 `그래프 │ 인스턴스 │ 경로 밖` 세 칸이 나란히
          서는데(dashboard-topology.tsx), 목록 두 칸이 400px쯤 가져가므로 9칸 열 안에서는 그래프에
          900px도 남지 않는다. 전폭으로 내리면 그래프가 1400px을 받아 5열 정렬이 여유 있게 선다.
          위 격자 **밖**에 두는 이유: 격자 안에서 12칸을 차지하게 하면 둘째 행에 묶여 오른쪽
          AI 카드가 길 때 그 높이만큼 빈 자리가 생긴다. */}
      <Panel
        title="자산 토폴로지"
        description="외부 출발지 → 트래픽 경로(대상 그룹 → EC2 → EBS)와 보호 계층 — 노드를 누르면 자산 상세로 이동합니다"
      >
        {items.length === 0 ? (
          <EmptyState message="수집된 자산이 없습니다." />
        ) : (
          <DashboardTopology
            items={items}
            uncollected={assets.uncollected}
            incidents={incidents}
          />
        )}
      </Panel>
    </div>
  );
}
