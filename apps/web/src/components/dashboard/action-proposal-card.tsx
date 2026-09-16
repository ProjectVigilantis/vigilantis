'use client';

// DSH-001 「AI 조치 제안」 카드 — 화면설계서 §4.1. 위험도 1순위 1건의 판단 근거·추천 런북·
// `[원클릭 조치]`를 첫 화면에 세우고, 2순위 이하는 `다음 대기`로 줄만 남깁니다.
//
// **실행 결과(ACT-002)는 여기서 그리지 않는다** — 202를 받으면 INC-002 상세로 보낸다(§2.2가
// 대시보드 경로에 정해 둔 "시작한 화면에서 INC-002로 이동"이며 INC-001 목록과 같은 처리다).
// 판단 근거가 없는 자리에 실행 상태만 띄우면 관제자가 근거 없이 후속 판단을 하게 된다.

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState } from 'react';

import {
  ActionExecuteDialog,
  type ActionCandidate,
  type ActionRequest,
} from '@/components/incidents/action-execute-dialog';
import { EmptyState } from '@/components/empty-state';
import { StatusBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { newIdempotencyKey } from '@/lib/api/client';
import { arnShort, incidentTitle, RUNBOOK_LABELS } from '@/lib/enum-labels';
import { formatKst } from '@/lib/utils';
import type { AssetItem, IncidentListItem, IncidentResponse } from '@/types/api';

function href(incidentId: string): string {
  return `/incidents/${encodeURIComponent(incidentId)}`;
}

/**
 * 판단 근거 3줄. 계약이 분석 완료 시 정확히 3개, 분석 중·실패 시 빈 배열로 강제한다 —
 * 빈 배열을 "근거 없음"으로 적으면 분석이 아직 안 끝난 건이 근거가 없는 건으로 읽힌다(§4.5와 같다).
 */
function Summary({ incident }: { incident: IncidentResponse }) {
  if (incident.summary_lines.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        {incident.status === 'ANALYZING' ? '분석 중' : '분석 실패'}
      </p>
    );
  }
  return (
    <ol className="flex list-decimal flex-col gap-1.5 pl-5 text-sm">
      {incident.summary_lines.map((line, i) => (
        <li key={i}>{line}</li>
      ))}
    </ol>
  );
}

/** 2순위 이하 — 판단 근거는 싣지 않는다. 여기서 고르는 자리가 아니라 다음이 무엇인지 아는 자리다. */
function NextUp({ items }: { items: IncidentListItem[] }) {
  if (items.length === 0) return null;
  return (
    <div className="mt-4 border-t pt-3">
      <p className="text-muted-foreground mb-2 text-xs">다음 대기 {items.length}건</p>
      <ul className="flex flex-col">
        {items.map((incident) => (
          <li key={incident.incident_id}>
            <Link
              href={href(incident.incident_id)}
              className="hover:bg-muted flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-xs"
            >
              <span className="truncate">{incidentTitle(incident)}</span>
              <span className="flex shrink-0 items-center gap-1.5">
                {incident.initial_risk_level !== null ? (
                  <StatusBadge field="risk_level" value={incident.initial_risk_level} />
                ) : null}
                <StatusBadge field="incident_status" value={incident.status} />
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ActionProposalCard({
  top,
  errorSlot,
  queue,
  assets,
}: {
  top: IncidentResponse | null;
  /**
   * 1순위 상세 조회가 실패했을 때 그 자리에 놓을 CMN-002. **카드만** 오류로 두고 대시보드는
   * 살린다(§4.1 예외).
   *
   * 오류 객체가 아니라 **서버에서 그린 엘리먼트**를 받는다 — `ErrorState`는 RSC 직렬화가
   * `ApiError`의 `code`·`requestId`를 조용히 버려서 클라이언트로 넘기면 6종 분기가 무너진다
   * (`error-state.tsx` 파일 상단 주의).
   */
  errorSlot: React.ReactNode;
  /** `미조치` 전량(위험도 정렬). 1순위는 `top`과 같은 건이다. */
  queue: IncidentListItem[];
  /** 제안의 `target_arn`을 조인해 승인 모달에 자산 사실값을 넘긴다(#183). */
  assets: AssetItem[];
}) {
  const router = useRouter();
  const [request, setRequest] = useState<ActionRequest | null>(null);

  const body = (() => {
    if (queue.length === 0) {
      // "지금 승인할 것이 없다"도 관제 정보다(§3.1) — 오류나 빈 화면으로 그리지 않는다.
      return <EmptyState message="승인을 기다리는 조치 제안이 없습니다." />;
    }
    if (top === null) return errorSlot;

    // §4.5 버튼 노출 규칙 그대로 — `recommendations`가 비면 버튼을 만들지 않는다(조회 전용).
    // `ANALYZING`은 계약이 빈 배열을 강제하므로 자연히 여기서 걸린다.
    const canExecute = top.recommendations.length > 0;
    const approveLabel = top.category === 'SECOPS' ? '승인하고 차단' : '이 조치 실행';
    // 진행 중 실행이 있으면 같은 Incident의 실행 버튼을 잠근다(§4.5).
    const locked = top.status === 'ACTION_IN_PROGRESS';

    return (
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <Link href={href(top.incident_id)} className="text-sm font-medium hover:underline">
            {incidentTitle(top)}
          </Link>
          <StatusBadge field="category" value={top.category} />
          {top.initial_risk_level !== null ? (
            <StatusBadge field="risk_level" value={top.initial_risk_level} />
          ) : null}
          <StatusBadge field="incident_status" value={top.status} />
          <span className="text-muted-foreground ml-auto text-xs">{formatKst(top.created_at)}</span>
        </div>

        <Summary incident={top} />

        {canExecute ? (
          <>
            <ul className="flex flex-col gap-1 border-t pt-3 text-xs">
              {top.recommendations.map((rec) => (
                <li key={rec.runbook_id} className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{RUNBOOK_LABELS[rec.runbook_id] ?? rec.runbook_id}</span>
                  {/* 복구 런북은 계약에 `target_arn`이 없어 null이다 — 없는 대상을 지어내지 않는다. */}
                  <span className="text-muted-foreground font-mono">
                    {rec.target_arn === null ? '대상 미지정' : arnShort(rec.target_arn)}
                  </span>
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap items-center gap-3">
              <Button
                type="button"
                disabled={locked}
                onClick={() =>
                  setRequest({
                    // 멱등 키는 **모달을 열 때 1회** 만든다(§4.6). 클릭마다 만들면 중복 클릭이
                    // 서로 다른 키가 되어 멱등성이 무력화된다.
                    idempotencyKey: newIdempotencyKey(),
                    variant: 'ACTION',
                    candidates: top.recommendations.map(
                      (r): ActionCandidate => ({
                        runbookId: r.runbook_id,
                        targetArn: r.target_arn,
                        displayParameters: r.display_parameters,
                        targetAsset: assets.find((a) => a.arn === r.target_arn) ?? null,
                      }),
                    ),
                  })
                }
              >
                {approveLabel}
              </Button>
              {locked ? (
                <span className="text-muted-foreground text-xs">
                  진행 중인 실행이 있어 새 실행을 받지 않습니다.
                </span>
              ) : null}
            </div>
          </>
        ) : (
          <p className="text-muted-foreground border-t pt-3 text-xs">
            추천된 조치가 아직 없습니다 — 분석이 끝나면 이 자리에 실행 버튼이 생깁니다.
          </p>
        )}

        <NextUp items={queue.slice(1)} />
      </div>
    );
  })();

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle className="text-sm">AI 조치 제안</CardTitle>
        <CardDescription className="text-xs">
          위험도 1순위 1건의 판단 근거와 추천 런북 — 실행하면 인시던트 상세에서 진행 상태를 봅니다
        </CardDescription>
      </CardHeader>
      <CardContent>{body}</CardContent>

      {top !== null ? (
        <ActionExecuteDialog
          incident={top}
          request={request}
          onClose={() => setRequest(null)}
          onExecuted={(outcome) => {
            // ACT-002는 INC-002가 그린다 — 실행 id를 실어 그 패널이 열린 채로 진입한다.
            router.push(
              `${href(top.incident_id)}?execution=${encodeURIComponent(outcome.execution.execution_id)}`,
            );
          }}
          // 409 PROPOSAL_NOT_EXECUTABLE — 제안이 이미 실행됐거나 무효해졌다. 대시보드를 다시 읽는다.
          onProposalStale={() => {
            setRequest(null);
            router.refresh();
          }}
        />
      ) : null}
    </Card>
  );
}
