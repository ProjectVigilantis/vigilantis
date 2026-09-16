// DSH-001 메인 대시보드 본문 — 구성은 PR #299가 채택한 5지표 + 카드 구성이고, 값은 전부 계약 필드에서
// 파생한다(lib/dashboard). 서버 컴포넌트다 — 실시간 이벤트가 오면 RealtimeProvider의 `router.refresh()`가
// 이 트리를 다시 그려 숫자가 따라온다.
//
// 아직 없는 것(#294 완료 기준): AI 조치 제안 카드와 `[원클릭 조치]`, 토폴로지의 외부 Source IP 노드·공격 경로.

import { DashboardTopology } from '@/components/dashboard/dashboard-topology';
import { EmptyState } from '@/components/empty-state';
import { StatusBadge } from '@/components/status-badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
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
import type { AssetsResponse, IncidentListItem, Verdict } from '@/types/api';

function Panel({
  title,
  description,
  children,
  className,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card className={className}>
      <CardHeader className="border-b">
        <CardTitle className="text-sm">{title}</CardTitle>
        {description ? <CardDescription className="text-xs">{description}</CardDescription> : null}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

/** 빨강은 `--danger` 하나뿐이다(§0.3). 값이 0이면 색을 입히지 않는다 — 0건 위협을 빨강으로 그리지 않는다. */
const METRIC_TONE = {
  danger: 'text-danger',
  warn: 'text-amber-400',
  ok: 'text-emerald-400',
} as const;

function Metric({
  label,
  value,
  note,
  tone,
}: {
  label: string;
  value: number | null;
  note: string;
  tone?: keyof typeof METRIC_TONE;
}) {
  return (
    <div className="bg-card flex flex-col gap-1.5 px-5 py-4">
      <span className="text-muted-foreground text-xs">{label}</span>
      <span
        className={cn(
          'font-mono text-2xl font-medium tabular-nums',
          tone && value !== null && value > 0 && METRIC_TONE[tone],
        )}
      >
        {value ?? NO_VALUE}
      </span>
      <span className="text-muted-foreground text-xs">{note}</span>
    </div>
  );
}

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
  COST_CANDIDATE: 'bg-orange-400',
  UNUSED: 'bg-orange-400',
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
}: {
  assets: AssetsResponse;
  /** 인시던트 조회 실패면 null — 자산 화면과 같은 규칙으로 0건과 구분한다. */
  incidents: IncidentListItem[] | null;
}) {
  const items = assets.items;
  const metrics = dashboardMetrics(items, incidents);
  const inventory = inventoryCounts(items);
  const exposure = exposureRows(items);
  const health = healthSummary(items);
  const verdicts = verdictCounts(items);

  return (
    <div className="flex flex-col gap-4">
      {/* collection_status가 READY면 배지를 그리지 않는다(§3.2) — 자산 화면과 같은 줄이다. */}
      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
        <StatusBadge field="collection_status" value={assets.collection_status} />
        <span>마지막 수집 {formatKst(assets.last_collected_at)}</span>
        <span>자산 {items.length}건</span>
        <span>{incidents === null ? '인시던트 조회 실패' : `인시던트 ${incidents.length}건`}</span>
      </div>

      <div className="bg-border grid grid-cols-2 gap-px overflow-hidden rounded-xl ring-1 ring-foreground/10 lg:grid-cols-5">
        <Metric label="전체 자산" value={metrics.total} note="수집된 자산 전량" />
        <Metric
          label="인터넷 개방 보안 그룹"
          value={metrics.openSg}
          note="전체 대역 인바운드 허용"
          tone="warn"
        />
        <Metric label="위협 판정 자산" value={metrics.threat} note="규칙 엔진 위협 판정" tone="danger" />
        <Metric
          label="미조치 인시던트"
          value={metrics.unhandled}
          note={metrics.unhandled === null ? '인시던트 조회 실패' : '분석·승인 대기·조치 중'}
          tone="warn"
        />
        <Metric label="낭비 후보" value={metrics.waste} note="최적화 후보 + 미사용" tone="ok" />
      </div>

      <Panel
        title="자산 토폴로지"
        description="트래픽 경로(대상 그룹 → EC2 → EBS)와 보호 계층 — 노드를 누르면 자산 상세로 이동합니다"
      >
        {items.length === 0 ? (
          <EmptyState message="수집된 자산이 없습니다." />
        ) : (
          <DashboardTopology items={items} />
        )}
      </Panel>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Panel title="자산 인벤토리" description="유형 7종 — 0건도 남겨 수집 누락과 구분합니다">
          <ul className="flex flex-col text-sm">
            {inventory.map(({ type, count }) => (
              <li key={type} className="flex items-center justify-between border-b py-2">
                <span className="text-muted-foreground">{ASSET_TYPE_LABELS[type]?.label ?? type}</span>
                <span className={cn('font-mono tabular-nums', count === 0 && 'text-muted-foreground')}>
                  {count}
                </span>
              </li>
            ))}
            <li className="flex items-center justify-between pt-2 font-medium">
              <span>합계</span>
              <span className="font-mono tabular-nums">{items.length}</span>
            </li>
          </ul>
        </Panel>

        <Panel title="인터넷 개방 보안 그룹" description="전체 대역(0.0.0.0/0 · ::/0)에 열린 인바운드">
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

        <Panel
          title="헬스 스코어"
          description={`EC2 ${health.ec2Total}대 · 임계선 ${IDLE_CPU_AVG} 미만은 저활성(스펙 조정 후보)`}
        >
          {health.ec2Total === 0 ? (
            <Muted>EC2 자산이 없습니다.</Muted>
          ) : health.scored.length === 0 ? (
            <Muted>{NO_VALUE} 확인 불가</Muted>
          ) : (
            <ul className="flex flex-col gap-3">
              {health.scored.map((asset) => {
                const score = asset.health_score ?? 0;
                return (
                  <li key={asset.arn} className="flex flex-col gap-1">
                    <span className="flex items-center justify-between gap-2 text-xs">
                      <span className="flex min-w-0 items-center gap-1.5">
                        <span className="truncate">{asset.name ?? asset.resource_id}</span>
                        {asset.verdict !== null ? (
                          <StatusBadge field="verdict" value={asset.verdict} />
                        ) : null}
                      </span>
                      <span className="font-mono tabular-nums">{score}</span>
                    </span>
                    <span className="bg-muted relative block h-1.5 overflow-hidden rounded-full">
                      <span
                        className={cn(
                          'absolute inset-y-0 left-0 rounded-full',
                          score < IDLE_CPU_AVG ? 'bg-orange-400' : 'bg-emerald-500',
                        )}
                        style={{ width: `${score}%` }}
                      />
                      {/* 임계선 — 선 왼쪽에서 끝나는 막대가 조치 대상이다(§4.1). */}
                      <span
                        aria-hidden
                        className="bg-foreground/70 absolute inset-y-0 w-px"
                        style={{ left: `${IDLE_CPU_AVG}%` }}
                      />
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
          {health.ec2Total > 0 ? <StatLine label="확인 불가 (점수 없는 EC2)" value={health.unknown} /> : null}
        </Panel>

        <Panel
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
    </div>
  );
}
