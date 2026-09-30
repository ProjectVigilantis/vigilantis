// AST-001 자산 요약 패널 2종 — 헬스 스코어 · 판정 현황.
//
// 종전에는 메인 대시보드(DSH-001)의 집계 3열(자산 인벤토리 · 헬스 스코어 · 판정 현황)이었다
// (2026-09-28 이전). 대시보드는 **"지금 몇 건"(지표 띠)과 "무엇을 누를까"(AI 제안·개방 SG)**만
// 남기고, EC2 한 대씩의 점수와 판정 분포처럼 자산을 들여다보는 것은 자산 화면으로 옮겼다 —
// 같은 수를 두 화면이 각자 그리면 관제자가 어느 화면을 봐야 할지부터 정해야 한다.
// 자산 인벤토리 패널은 따로 두지 않는다: 자산 화면의 유형 지표 띠가 이미 같은 함수
// (`inventoryCounts`)로 세고 있다.
//
// 셈은 `lib/dashboard`의 `healthSummary`·`verdictCounts` 그대로다 — 옮긴 것은 그리는 자리뿐이다.
// 훅이 없어 `'use client'`를 붙이지 않는다(panel.tsx와 같은 이유). 안의 `HealthScoreList`만
// 클라이언트 컴포넌트다(호버 팝업).

import { HealthScoreList } from '@/components/assets/health-score-list';
import { Panel } from '@/components/panel';
import { Bar, Muted, StatLine } from '@/components/panel-parts';
import { healthSummary, IDLE_CPU_AVG, verdictCounts } from '@/lib/dashboard';
import { NO_VALUE, VERDICT_LABELS } from '@/lib/enum-labels';
import type { AssetItem, Verdict } from '@/types/api';

const VERDICT_BAR: Record<Verdict, string> = {
  THREAT: 'bg-danger',
  COST_CANDIDATE: 'bg-amber-400',
  UNUSED: 'bg-amber-400',
  SKIP: 'bg-muted-foreground/50',
};

/** EC2 헬스 스코어 — 점수 낮은 순 막대 + 임계선. 분모는 수집 전량이다(유형 지표 띠와 같다). */
export function HealthScorePanel({
  items,
  className,
}: {
  items: readonly AssetItem[];
  className?: string;
}) {
  const health = healthSummary(items);
  return (
    <Panel
      className={className}
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
  );
}

/** Rule 판정 분포 — 판정 대상(NACL·ASG·시작 템플릿·대상 그룹 제외)만 분모로 센다. */
export function VerdictPanel({
  items,
  className,
}: {
  items: readonly AssetItem[];
  className?: string;
}) {
  const verdicts = verdictCounts(items);
  return (
    <Panel
      className={className}
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
  );
}
