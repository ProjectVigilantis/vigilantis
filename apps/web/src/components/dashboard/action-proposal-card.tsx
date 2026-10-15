// DSH-001 「AI 조치 제안」 카드 — 화면설계서 §4.1. **요약 자리다(2026-09-29).** 미조치 인시던트
// 전량을 한 줄씩 세운다. 줄마다 공통으로 이름 · 유형(자산/보안) · 상태 · 발생 시각을 싣고,
// 보안 건은 위험도 배지를, 자산 건은 채택 시 추정 절감액을 더한다. 순서는 `lib/dashboard` `proposalRows`.
//
// 판단 근거·추천 런북·실행 버튼은 싣지 않는다 — 줄을 누르면 가는 INC-002 상세가 그 자리다.
// 대시보드에서 근거 일부만 보고 실행하게 두면, 상세의 근거 3줄·대상 확인을 건너뛴 실행이 생긴다.

import Link from 'next/link';

import { EmptyState } from '@/components/empty-state';
import { StatusBadge } from '@/components/status-badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import type { ProposalRow } from '@/lib/dashboard';
import { incidentTitle } from '@/lib/enum-labels';
import { formatTick } from '@/lib/metrics-chart';
import { formatUsd } from '@/lib/savings';
import { formatKst } from '@/lib/utils';

function href(incidentId: string): string {
  return `/incidents/${encodeURIComponent(incidentId)}`;
}

function Row({ row }: { row: ProposalRow }) {
  const { incident, savings } = row;
  return (
    <Link
      href={href(incident.incident_id)}
      className="hover:bg-muted flex flex-col gap-1 rounded-md px-2 py-1.5"
    >
      <span className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-sm">{incidentTitle(incident)}</span>
        {/* 3칸 폭에 전체 시각(`2026. 9. 21. 오전 9:44:51 KST`)이 서지 않는다 — 월·일·시·분으로 줄이고
            초·연도까지는 호버 제목으로 남긴다. */}
        <span
          title={formatKst(incident.created_at)}
          className="text-muted-foreground shrink-0 font-mono text-xs tabular-nums"
        >
          {formatTick(Date.parse(incident.created_at))}
        </span>
      </span>
      <span className="flex flex-wrap items-center gap-1.5">
        <StatusBadge field="category" value={incident.category} />
        <StatusBadge field="incident_status" value={incident.status} />
        {incident.category === 'SECOPS' ? (
          // 계약상 분석 전 SECOPS도 위험도가 null일 수 있다 — 없는 등급을 지어내지 않는다.
          incident.initial_risk_level !== null ? (
            <StatusBadge field="risk_level" value={incident.initial_risk_level} />
          ) : null
        ) : (
          // AI 참고 추정이지 청구액이 아니다(#347) — 금액 옆에 "추정"을 함께 적는다(`lib/savings.ts` 머리말).
          <span className="text-muted-foreground ml-auto font-mono text-xs tabular-nums">
            {savings === null ? '절감 추정 없음' : `추정 ${formatUsd(savings)}/월`}
          </span>
        )}
      </span>
    </Link>
  );
}

export function ActionProposalCard({
  rows,
  errorSlot,
}: {
  /**
   * `미조치` 전량을 줄 순서대로(`proposalRows`). 지표 띠의 `미조치` 두 칸 합과 같은 집합이다.
   * **null은 목록 조회 실패**다 — 빈 배열(대기 0건)과 다르게 그린다(PR #351 리뷰 2).
   */
  rows: ProposalRow[] | null;
  /**
   * 목록 조회가 실패했을 때 그 자리에 놓을 CMN-002. **카드만** 오류로 두고 대시보드는 살린다(§4.1 예외).
   * 서버에서 그린 엘리먼트를 받는다 — `ErrorState`를 클라이언트 경계 너머로 보내면 RSC 직렬화가
   * `ApiError`의 `code`·`requestId`를 버린다(`error-state.tsx` 파일 상단 주의).
   */
  errorSlot: React.ReactNode;
}) {
  const body = (() => {
    if (rows === null) return errorSlot;
    if (rows.length === 0) {
      // "지금 승인할 것이 없다"도 관제 정보다(§3.1) — 조회에 **성공한** 빈 목록만 여기 온다.
      return <EmptyState message="승인을 기다리는 조치 제안이 없습니다." />;
    }
    return (
      // 목록 스크롤 — 넓은 화면에서는 카드가 받은 높이(왼쪽 열)를
      // 다 쓰고, 한 열로 접히면 고정 상한(`max-h-60`)으로 같은 일을 한다.
      <ul className="-mx-2 flex max-h-60 min-h-0 flex-col divide-y overflow-y-auto xl:max-h-none xl:flex-1">
        {rows.map((row) => (
          <li key={row.incident.incident_id}>
            <Row row={row} />
          </li>
        ))}
      </ul>
    );
  })();

  return (
    // `xl`에서 격자 칸(dashboard-view.tsx 오른쪽 열)에 꽉 찬다 — 높이는 왼쪽 열이 정한다.
    <Card className="xl:absolute xl:inset-0">
      <CardHeader className="border-b">
        <CardTitle className="text-sm">
          AI 조치 제안
          {rows !== null && rows.length > 0 ? (
            <span className="text-muted-foreground ml-1.5 font-mono font-normal tabular-nums">
              {rows.length}건
            </span>
          ) : null}
        </CardTitle>
        <CardDescription className="text-xs">
          보안은 위험도순, 자산은 추정 절감액순 — 누르면 상세에서 판단 근거와 조치를 봅니다
        </CardDescription>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col">{body}</CardContent>
    </Card>
  );
}
