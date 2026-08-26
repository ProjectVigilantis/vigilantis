'use client';

// ACT-002 실행 상태 인라인 — 화면설계서 v1.5 §4.7. INC-002 상세 하단에 붙는 패널입니다.

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useRef } from 'react';

import { CopyButton } from '@/components/copy-button';
import { ExecutionStatusBadge } from '@/components/status-badge';
import { Card } from '@/components/ui/card';
import { isTerminalStatus } from '@/lib/execution-status';
import { formatKst } from '@/lib/utils';
import type { ExecuteActionResponse, ExecutionStatus, IsoDateTime } from '@/types/api';

/** 진행 기록 한 줄 — `EXECUTION_UPDATED` 수신 시각과 status 전이다(§4.7). */
export interface ExecutionTransition {
  at: IsoDateTime;
  /** 최초 접수는 이전 상태가 없다. */
  from: ExecutionStatus | null;
  to: ExecutionStatus;
}

/**
 * §4.7 최종 상태 표시 표. `FAILED`(AWS 변경 없음)와 `ROLLBACK_FAILED`(변경된 채 복구 실패)는
 * 대응이 달라 같은 "실패"로 합치지 않는다.
 */
const STATUS_MESSAGES: Record<ExecutionStatus, string> = {
  IN_PROGRESS: '조치를 실행하고 있습니다.',
  SUCCESS: '조치가 완료됐습니다.',
  FAILED: '실행하지 못했습니다. AWS 변경은 없으며 재시도하거나 다른 제안을 고를 수 있습니다.',
  ROLLBACK_INITIATED: '복구를 시작했습니다.',
  ROLLED_BACK: '이전 상태로 복구했습니다.',
  ROLLBACK_FAILED: '복구에 실패했습니다. 수동 확인이 필요합니다.',
};

export function ExecutionStatusPanel({
  execution,
  transitions,
  replayed,
  subjectHref,
}: {
  execution: ExecuteActionResponse;
  transitions: ExecutionTransition[];
  /** 200(같은 키 재요청)으로 열린 패널. */
  replayed: boolean;
  /** AST-001 링크. 스펙 조정 진행은 EC2 `state` 전이로만 관측되므로 단계를 지어내지 않고 넘긴다(§4.7). */
  subjectHref: string | null;
}) {
  const router = useRouter();
  const refreshedFor = useRef<string | null>(null);

  // 최종 상태를 받으면 상세를 재조회해 화면 값을 확정한다 — WS는 알림이고 상태의 기준이 아니다(§4.8).
  // router.refresh()는 useState를 보존하므로 이 패널은 유지된다(Next 16 useRouter 레퍼런스).
  useEffect(() => {
    if (!isTerminalStatus(execution.status)) return;
    const key = `${execution.execution_id}:${execution.status}`;
    if (refreshedFor.current === key) return;
    refreshedFor.current = key;
    router.refresh();
  }, [execution.execution_id, execution.status, router]);

  const critical = execution.status === 'ROLLBACK_FAILED';

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-muted-foreground text-xs font-medium">실행 상태</h2>
      <Card className={`gap-3 p-4 ${critical ? 'border-destructive' : ''}`}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <ExecutionStatusBadge value={execution.status} />
          <span className="flex items-center gap-1">
            <code className="text-muted-foreground font-mono text-xs">
              {execution.execution_id}
            </code>
            <CopyButton value={execution.execution_id} label="실행 ID 복사" />
          </span>
        </div>

        <p className={`text-sm ${critical ? 'text-destructive font-medium' : ''}`}>
          {critical ? '⚠ ' : ''}
          {STATUS_MESSAGES[execution.status]}
          {critical ? ' (CRITICAL)' : ''}
        </p>

        {/* 롤백이 가드레일에서 거절되면 자동 재시도가 없다 — 재시도 버튼을 만들지 않는다.
            만들면 SSOT 롤백 공통 정책 ④에 없는 경로를 사용자가 실행하게 된다(§4.7). */}

        {replayed ? (
          <p className="text-muted-foreground text-xs">이미 접수된 요청입니다.</p>
        ) : null}

        <div className="flex flex-col gap-1">
          <h3 className="text-muted-foreground text-xs font-medium">진행 기록</h3>
          {/* 실행 중 관제자가 볼 수 있는 유일한 정보다(§4.7 ○ FE 판단).
              시각은 고정 폭(tabular-nums)으로 잡아 줄이 쌓여도 세로로 맞춘다. */}
          <ol className="flex flex-col gap-0.5 text-sm">
            {transitions.map((t, i) => (
              <li key={i} className="flex gap-3">
                <time className="text-muted-foreground font-mono text-xs tabular-nums">
                  {formatKst(t.at)}
                </time>
                <span>
                  {t.from === null ? '실행 접수: ' : `실행 상태: ${t.from} → `}
                  {t.to}
                </span>
              </li>
            ))}
          </ol>
          {transitions.length === 1 ? (
            // WS 미연동 구간이라 접수 이후 전이가 들어오지 않는다 — 빈 목록으로 오해되지 않게 밝힌다.
            <p className="text-muted-foreground text-xs">
              이후 상태 전이는 실시간 연결(WS) 연동 후 이 목록에 쌓입니다.
            </p>
          ) : null}
        </div>

        {subjectHref ? (
          <p className="text-xs">
            <Link href={subjectHref} className="underline underline-offset-4">
              자산 보기 →
            </Link>{' '}
            {/* 계약에 실행 단계(phase)가 없어 진행률을 지어낼 수 없다 — 실제 자원 상태는 AST-001로
                넘긴다(§4.7). 예: 스펙 조정은 EC2 state 전이로만 관측된다. */}
            <span className="text-muted-foreground">
              실제 자원 상태는 자산 관제에서 확인합니다.
            </span>
          </p>
        ) : null}
      </Card>
    </section>
  );
}
