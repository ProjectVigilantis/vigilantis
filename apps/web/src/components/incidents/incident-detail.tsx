'use client';

// INC-002 인시던트 상세 본문 — 화면설계서 v1.5 §4.5. A 변형(FINOPS) 기준으로 세운 공통 골격입니다.
//
// ACT-001 모달의 상태(선택한 런북·멱등 키)를 **수행된 조치(중간)와 제안 조치(하단) 두 곳이 함께**
// 열고, 그 결과를 ACT-002 패널(맨 아래)이 받는다. 세 지점이 한 상태를 공유해야 해서 이 파일 전체를
// 클라이언트 경계로 둔다 — Context를 새로 세우는 것보다 이쪽이 작다.

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';

import { CopyButton } from '@/components/copy-button';
import { Row } from '@/components/detail-row';
import {
  ActionExecuteDialog,
  type ActionCandidate,
  type ActionRequest,
  type ExecuteOutcome,
} from '@/components/incidents/action-execute-dialog';
import {
  ExecutionStatusPanel,
  type ExecutionTransition,
} from '@/components/incidents/execution-status-panel';
import { EmptyState } from '@/components/empty-state';
import { ExecutionStatusBadge, StatusBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { useRealtime } from '@/components/realtime-provider';
import { agentWaitTimes, appendTransition, latchAgentWaitAt } from '@/lib/realtime-events';
import { newIdempotencyKey } from '@/lib/api/client';
import { isTerminalStatus } from '@/lib/execution-status';
import { RUNBOOK_LABELS, incidentTitle } from '@/lib/enum-labels';
import { formatKst } from '@/lib/utils';
import type { AssetItem, IncidentResponse, IsoDateTime, RunbookId } from '@/types/api';

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-muted-foreground text-xs font-medium">{title}</h2>
      {children}
    </section>
  );
}

/**
 * 판단 근거는 계약상 분석 완료 시 정확히 3줄, 분석 중·실패면 빈 배열이다.
 * 빈 배열을 그냥 비워두면 "근거가 없는 인시던트"로 읽히므로 status 기준 안내로 대체한다(§3.3).
 */
function SummaryLines({ incident }: { incident: IncidentResponse }) {
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

/**
 * 위험도 영역 — SECOPS(B 변형) 전용이다(§4.5).
 * FINOPS는 계약이 세 필드를 전부 null로 강제하므로 **영역 자체를 렌더하지 않는다**(§3.3).
 * 빈 배지를 남기면 "판정을 못 받은 위협"으로 읽힌다.
 */
function RiskArea({
  incident,
  agentWaitAt,
}: {
  incident: IncidentResponse;
  agentWaitAt: IsoDateTime | null;
}) {
  if (incident.initial_risk_level === null) return null;

  return (
    <Section title="위험도">
      <div className="flex flex-col gap-1.5">
        {/* ⏳ 판정 사유·정책 버전·판정 시각은 계약에 필드가 없다(9장 #30) — 등급만 표시한다. */}
        <Row label="초기 판정">
          <StatusBadge field="risk_level" value={incident.initial_risk_level} />
        </Row>
        <Row label="정밀 평가">
          {incident.reviewed_risk_level !== null ? (
            <StatusBadge field="risk_level" value={incident.reviewed_risk_level} />
          ) : (
            // null은 "위험도 없음"이 아니라 아직 안 나온 것이다 — status로 갈라 적는다(§3.3).
            <span className="text-muted-foreground">
              {incident.status === 'ANALYZING' ? '정밀평가 진행 중' : '정밀평가 없음'}
            </span>
          )}
        </Row>
        {incident.response_mode !== null ? (
          <Row label="대응">
            <StatusBadge field="response_mode" value={incident.response_mode} />
          </Row>
        ) : null}
      </div>
      <AgentWaitNotice incident={incident} agentWaitAt={agentWaitAt} />
    </Section>
  );
}

/**
 * B-Medium 타임아웃 창 — **고지를 그리는 조건이자 기준 시각을 래치하는 조건**이다.
 * 두 곳이 따로 판정하면 화면에는 시각이 떠 있는데 기준은 안 잡히는 식으로 어긋난다.
 */
function inAgentWaitWindow(incident: IncidentResponse): boolean {
  return (
    incident.response_mode === 'AGENT_WAIT' &&
    incident.initial_risk_level !== 'LOW' &&
    incident.status === 'AWAITING_APPROVAL'
  );
}

/**
 * B-Medium 타임아웃 고지(§4.5) — 승인 전까지 조치는 수행되지 않지만, 1분 미응답이면 서버가
 * `TIMEOUT_ISOLATION_1M`으로 자동 격리한다는 사실을 **대기 중에 미리** 알린다.
 *
 * **초 단위 카운트다운 대신 절대 시각 2종을 쓴다**(2026-08-27 확정 · SSOT §확정 결정 로그).
 * 1분을 실시간으로 지켜보게 하는 화면이 1차 시연 범위 밖이고(`docs/E2E_DEMO_SCENARIOS.md`),
 * 초를 세면 클라이언트 시계 오차가 그대로 화면 값이 된다.
 *
 * 기준 시각의 **원천은 서버로 두는 것이 확정 방향**이나 응답 필드 신설이 발동 엔진
 * (`TIMEOUT_ISOLATION_1M` 스케줄러 잡 · Risk Evaluator)과 같은 묶음으로 미뤄졌다. 그때까지는
 * `INCIDENT_UPDATED`의 `occurred_at`에서 파생한 **잠정 표기**다 — 그 이벤트를 못 본 진입
 * (재접속·목록에서 나중에 열기)은 시각 없이 고정 안내문으로 떨어진다.
 *
 * Low는 같은 `AGENT_WAIT`이지만 타임아웃 자동 격리가 없어 이 블록을 뺀다(§4.5 · SSOT §3단계 위험 대응).
 *
 * 기준 등급은 **`initial_risk_level`만** 본다(2026-08-25 확정, SSOT §확정 결정 로그).
 * `reviewed_risk_level`(AI 정밀 평가)은 초기 판정을 덮어쓰지 않는 관제자 참고값이라 자동 행동을
 * 가르지 않는다 — `response_mode`도 초기 판정에서만 파생된다(`packages/schemas/events.py`
 * `_EXPECTED_MODE_BY_RISK`). 정밀 평가가 자동 행동을 바꾸려면 상태 전이 계약이 먼저 필요하다
 * (Risk Evaluator, SSOT §미해결 6번).
 */
function AgentWaitNotice({
  incident,
  agentWaitAt,
}: {
  incident: IncidentResponse;
  agentWaitAt: IsoDateTime | null;
}) {
  if (!inAgentWaitWindow(incident)) return null;

  // 기준은 `AGENT_WAIT` 전환을 알린 INCIDENT_UPDATED의 occurred_at이다 — **수신 시각을 쓰지
  // 않는다**(합의 2026-08-14). 수신 기준이면 재접속마다 시간이 늘어난다. 파싱 불가한 값은
  // null로 떨어져 아래 고정 안내문이 받는다(PR #187 리뷰).
  const times = agentWaitTimes(agentWaitAt);

  return (
    <div className="border-danger/30 bg-danger/5 flex flex-col gap-1.5 rounded-md border p-3 text-sm">
      <p>승인 전까지 조치는 수행되지 않습니다.</p>
      {times !== null ? (
        // formatKst는 timeZone을 고정하므로 서버·클라이언트가 같은 문자열을 만든다.
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
          <dt className="text-muted-foreground text-xs">제안 생성</dt>
          <dd className="font-mono text-xs tabular-nums">{formatKst(times.startedAt)}</dd>
          <dt className="text-muted-foreground text-xs">실행 예정</dt>
          <dd className="text-danger font-mono text-xs font-medium tabular-nums">
            {formatKst(times.deadlineAt)}
          </dd>
        </dl>
      ) : (
        // 전환 이벤트를 못 받았거나 값이 계약 밖이면 시각 없이 고정 안내문을 쓴다(§4.5).
        <p className="text-danger font-medium">1분 안에 응답하지 않으면 서버가 자동으로 격리합니다.</p>
      )}
    </div>
  );
}

/**
 * 수행된 조치(§4.5 B 변형) — `executions[]`를 항목별로 그린다.
 * `executions = []`이면 **영역 자체를 렌더하지 않는다**(§3.3).
 *
 * 상태는 `ExecutionStatusBadge`로 그린다 — Incident status와 의미가 달라 배지를 분리했고(§3.2),
 * `FAILED`(AWS 변경 없음)와 `ROLLBACK_FAILED`(변경된 채 복구 실패·CRITICAL)를 합치지 않는다.
 *
 * 복구 버튼은 **항목별 `available_recovery_runbook_ids`로만** 판정한다(§4.5) — 이 목록이 비어 있다고
 * 비가역인 것이 아니라 "백업 기반 롤백이 붙어 있느냐"만 나타낸다. 요청 구성·멱등 키는 ACT-001
 * 모달(§4.6) 소관이라 여기서는 모달을 열기만 한다.
 */
function ExecutionsArea({
  incident,
  locked,
  onRecover,
}: {
  incident: IncidentResponse;
  /** `ACTION_IN_PROGRESS`면 같은 Incident의 실행 버튼을 전부 비활성화한다(§4.5). */
  locked: boolean;
  onRecover: (runbookId: RunbookId, originExecutionId: string) => void;
}) {
  if (incident.executions.length === 0) return null;

  return (
    <Section title="수행된 조치">
      <ul className="flex flex-col gap-2">
        {incident.executions.map((execution) => (
          <li key={execution.execution_id}>
            <Card className="gap-2 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">{RUNBOOK_LABELS[execution.runbook_id]}</span>
                <ExecutionStatusBadge value={execution.status} />
              </div>
              <p className="text-muted-foreground text-xs">
                갱신 {formatKst(execution.updated_at)}
              </p>
              <div className="flex flex-wrap items-center gap-2">
                {/* 차단을 그대로 두는 선택도 조치다 — SECOPS 격리 실행에만 둔다(§4.5 버튼 표). */}
                {incident.category === 'SECOPS' ? (
                  <Button type="button" variant="outline" size="sm" disabled>
                    차단 유지
                  </Button>
                ) : null}
                {execution.available_recovery_runbook_ids.map((runbookId) => (
                  <Button
                    key={runbookId}
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={locked}
                    onClick={() => onRecover(runbookId, execution.execution_id)}
                  >
                    {RUNBOOK_LABELS[runbookId]}
                  </Button>
                ))}
              </div>
            </Card>
          </li>
        ))}
      </ul>
    </Section>
  );
}

/**
 * 제안 조치 버튼(§4.5 버튼 노출 규칙 표) — **계약이 정한 조건 외에는 어떤 실행 버튼도 만들지 않는다.**
 * 전부 확정 필드로 판정한다(§3.2.3 유도 규칙).
 *
 * | 조건 | 버튼 |
 * | --- | --- |
 * | `recommendations ≥ 1` · FINOPS | `이 조치 실행` |
 * | `recommendations ≥ 1` · SECOPS | `승인하고 차단` |
 * | 〃 + `response_mode = AGENT_WAIT` | `승인하고 차단` `차단 안 함` — 실행 전 상태 |
 * | `recommendations = []` | 없음(조회 전용) |
 *
 * `status = ANALYZING`은 계약이 `recommendations`를 빈 배열로 강제하므로 자연히 버튼이 사라진다.
 * `ACTION_IN_PROGRESS`면 같은 Incident의 실행 버튼을 전부 비활성화한다.
 *
 * 누르면 ACT-001 모달을 연다(§4.6) — 요청 3필드 구성·멱등 키·파괴적 조치 경고가 거기 있고,
 * 그것 없이 실행을 열면 계약을 어긴 요청이 나간다.
 */
function ProposalActions({
  incident,
  locked,
  onExecute,
}: {
  incident: IncidentResponse;
  locked: boolean;
  onExecute: (candidates: ActionCandidate[]) => void;
}) {
  if (incident.recommendations.length === 0) return null;

  const isSecOps = incident.category === 'SECOPS';
  const approveLabel = isSecOps ? '승인하고 차단' : '이 조치 실행';
  // 반려(`차단 안 함`)는 아직 실행되지 않은 AGENT_WAIT 상태에서만 의미가 있다(§4.5 B-Medium).
  const canReject = isSecOps && incident.response_mode === 'AGENT_WAIT';

  return (
    <div className="flex flex-wrap items-center gap-3">
      <Button
        type="button"
        disabled={locked}
        onClick={() =>
          onExecute(
            incident.recommendations.map((r) => ({
              runbookId: r.runbook_id,
              targetArn: r.target_arn,
              displayParameters: r.display_parameters,
            })),
          )
        }
      >
        {approveLabel}
      </Button>
      {/* 반려는 API를 부르지 않는 화면 조작이라 ACT-001 소관이 아니다 — INC-002 B(#155) 자리 그대로. */}
      {canReject ? (
        <Button type="button" variant="outline" disabled>
          차단 안 함
        </Button>
      ) : null}
      {locked ? (
        <span className="text-muted-foreground text-xs">
          진행 중인 실행이 있어 새 실행을 받지 않습니다.
        </span>
      ) : null}
    </div>
  );
}

export function IncidentDetail({
  incident,
  subject,
  openExecutionId = null,
}: {
  incident: IncidentResponse;
  /** `subject_arn` → `GET /assets`의 `arn` 조인 결과(§4.5). 조회 실패·미수집이면 null이다. */
  subject: AssetItem | null;
  /**
   * `?execution=<id>` 딥링크(§4.4 목록에서 실행한 경우). 자체 URL이 없는 ACT-002를
   * 부모 화면이 열어 준다 — `?asset=`(AST-002, #138)과 같은 방식이다.
   * 값은 이미 받아 온 `executions[]`에서 찾으므로 재조회하지 않는다.
   */
  openExecutionId?: string | null;
}) {
  const router = useRouter();
  // 모달 인스턴스 = 이 객체 하나. 열 때마다 새로 만들어 **멱등 키를 인스턴스에 고정**한다(§4.6).
  const [request, setRequest] = useState<ActionRequest | null>(null);
  // 딥링크로 들어온 실행(#179). props에서만 파생하므로 서버·클라이언트가 같은 값을 만든다(하이드레이션 안전).
  const deepLinked =
    openExecutionId === null
      ? undefined
      : incident.executions.find((e) => e.execution_id === openExecutionId);
  const [outcome, setOutcome] = useState<ExecuteOutcome | null>(() =>
    deepLinked === undefined
      ? null
      : {
          // 딥링크로 들어온 것은 재요청 응답이 아니다 — `이미 접수된 요청입니다`를 띄우지 않는다.
          replayed: false,
          execution: {
            execution_id: deepLinked.execution_id,
            status: deepLinked.status,
            updated_at: deepLinked.updated_at,
          },
        },
  );
  const { connection, subscribeExecution, subscribeIncident } = useRealtime();
  /**
   * ACT-002 진행 기록 — `EXECUTION_UPDATED` 수신마다 한 줄 쌓는다(§4.7). WS에 사용자용 메시지
   * 필드가 없으므로(추가하지 않는다) **수신 시각과 status 전이만** 담는다.
   *
   * 딥링크 진입도 접수 행 하나로 시작한다 — 비우면 패널이 진행 기록을 빈 목록으로 그린다.
   */
  const [transitions, setTransitions] = useState<ExecutionTransition[]>(() =>
    deepLinked === undefined
      ? []
      : [{ at: deepLinked.updated_at, from: null, to: deepLinked.status }],
  );
  /**
   * B-Medium 대기 기준 시각 — `AGENT_WAIT` 전환을 알린 `INCIDENT_UPDATED`의
   * `occurred_at`이다(계약 합의 2026-08-14 · #155). **수신 시각을 쓰지 않는다.**
   *
   * 창에 **들어가는 순간 한 번만** 래치한다. 창 안에서 오는 후속 `INCIDENT_UPDATED`(정밀 평가
   * 도착 등)마다 다시 물리면 60초가 리셋돼 **서버 자동 격리보다 화면이 시간을 더 남았다고
   * 말한다**(PR #181 리뷰). 이벤트를 못 본 진입(재접속·목록에서 나중에 열기)은 null로 남고
   * 고정 안내문 fallback이 대신한다 — replay가 없는 계약의 결과다(§4.5 · ws.py).
   */
  const [waitBase, setWaitBase] = useState<IsoDateTime | null>(null);
  const lastIncidentEventRef = useRef<IsoDateTime | null>(null);

  useEffect(
    () =>
      subscribeIncident((action) => {
        if (action.incidentId !== incident.incident_id) return;
        lastIncidentEventRef.current = action.occurredAt;
      }),
    [subscribeIncident, incident.incident_id],
  );

  // Provider가 이벤트마다 재조회하므로 `incident`가 뒤이어 바뀐다 — 창 진입은 그때 판정된다.
  const inWindow = inAgentWaitWindow(incident);
  useEffect(() => {
    setWaitBase((prev) => latchAgentWaitAt(prev, inWindow, lastIncidentEventRef.current));
  }, [inWindow]);

  /**
   * §4.5 실행 잠금. `incident.status`는 서버 컴포넌트 prop이라 202 직후에는 아직 갱신되지 않는데,
   * 그 공백에 다른 런북을 누르면 **새 모달 = 새 멱등 키**라 계약의 Idempotency가 걸러 주지 못하고
   * 같은 대상에 두 번째 실제 실행이 나간다(PR #169 리뷰). 그래서 응답을 받은 즉시 로컬로도 잠근다.
   *
   * 잠금은 **비최종 상태 동안만**이다 — `FAILED`는 AWS 변경이 없어 재시도·다른 제안이 가능하고(§4.7),
   * 그때는 재조회된 서버 상태가 판단한다.
   */
  // 이 화면이 연 실행의 전이만 받는다 — 다른 인시던트·다른 실행의 이벤트는 무시한다.
  const watchedId = outcome?.execution.execution_id ?? null;
  useEffect(() => {
    if (watchedId === null) return;
    return subscribeExecution((action) => {
      if (action.executionId !== watchedId) return;
      setTransitions((prev) => appendTransition(prev, { at: action.updatedAt, status: action.status }));
      setOutcome((prev) =>
        prev === null
          ? prev
          : {
              ...prev,
              execution: {
                ...prev.execution,
                status: action.status,
                updated_at: action.updatedAt,
              },
            },
      );
    });
  }, [subscribeExecution, watchedId]);

  /**
   * 끊긴 동안 놓친 `EXECUTION_UPDATED` 복구. 재연결 재조회(§4.8 3)로 들어온 서버 실행 상태를
   * **렌더에서 파생한다** — 없으면 실행이 끝났는데도 패널이 비최종에 멈추고 아래 `locked`가
   * 안 풀려 **다음 조치를 아예 못 누른다**(PR #181 리뷰).
   *
   * state를 고치지 않고 파생하는 이유는 effect 안 setState가 연쇄 렌더이기 때문이다.
   * **최종 상태만** 받아서 재조회가 WS보다 늦게 도착해도 화면이 뒤로 돌지 않는다.
   */
  const served = watchedId === null
    ? undefined
    : incident.executions.find((e) => e.execution_id === watchedId);
  const missed =
    outcome !== null &&
    !isTerminalStatus(outcome.execution.status) &&
    served !== undefined &&
    isTerminalStatus(served.status)
      ? served
      : null;
  const shownOutcome =
    outcome === null || missed === null
      ? outcome
      : {
          ...outcome,
          execution: { ...outcome.execution, status: missed.status, updated_at: missed.updated_at },
        };
  // 같은 status면 appendTransition이 줄을 늘리지 않는다 — WS로 이미 받은 전이는 중복되지 않는다.
  const shownTransitions =
    missed === null
      ? transitions
      : appendTransition(transitions, { at: missed.updated_at, status: missed.status });

  const locked =
    incident.status === 'ACTION_IN_PROGRESS' ||
    (shownOutcome !== null && !isTerminalStatus(shownOutcome.execution.status));
  const subjectHref = subject ? `/assets?asset=${encodeURIComponent(subject.arn)}` : null;

  function openAction(candidates: ActionCandidate[]) {
    setRequest({ idempotencyKey: newIdempotencyKey(), candidates, variant: 'ACTION' });
  }

  function openRecovery(runbookId: RunbookId, originExecutionId: string) {
    setRequest({
      idempotencyKey: newIdempotencyKey(),
      // 복구 런북은 `available_recovery_runbook_ids`의 ID뿐이다 — 계약에 target·파라미터가 없다.
      candidates: [{ runbookId, targetArn: null, displayParameters: null }],
      variant: 'RECOVERY',
      originExecutionId,
    });
  }

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-1">
          <StatusBadge field="category" value={incident.category} />
          <StatusBadge field="incident_status" value={incident.status} />
        </div>
        <h1 className="text-lg font-semibold">{incidentTitle(incident)}</h1>
        <p className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <code className="font-mono">{incident.incident_id}</code>
          <span>생성 {formatKst(incident.created_at)}</span>
          <span>갱신 {formatKst(incident.updated_at)}</span>
        </p>
      </header>

      <RiskArea incident={incident} agentWaitAt={waitBase} />

      <Separator />

      <Section title="대상 자산">
        <Card className="gap-1.5 p-4">
          <div className="flex flex-wrap items-center gap-2">
            {subject ? <StatusBadge field="asset_type" value={subject.asset_type} /> : null}
            {/* AST-002는 Drawer라 자체 URL이 없다 — 목록 화면에 ARN을 넘겨 그 항목을 연 채로 진입한다(§4.5 액션).
                수집 목록에 없는 자산이면 열 상세가 없으므로 링크를 걸지 않는다. */}
            {subject ? (
              <Link
                href={`/assets?asset=${encodeURIComponent(subject.arn)}`}
                className="hover:text-primary font-medium underline-offset-4 hover:underline"
              >
                {subject.name ?? subject.resource_id}
              </Link>
            ) : (
              <span className="text-muted-foreground font-medium">수집 목록에 없는 자산</span>
            )}
          </div>
          <span className="flex items-center gap-1">
            <code className="text-muted-foreground font-mono text-xs break-all">
              {incident.subject_arn}
            </code>
            <CopyButton value={incident.subject_arn} label="ARN 복사" />
          </span>
        </Card>
      </Section>

      <Section title="판단 근거">
        <SummaryLines incident={incident} />
      </Section>

      <Section title="근거 데이터">
        {incident.evidence_ids.length === 0 ? (
          <EmptyState message="표시할 근거 데이터가 없습니다." />
        ) : (
          // 계약은 ID만 준다 — 내용 필드는 없다(합의 2026-08-14). 감사 대조용이라 복사가 본 기능이다.
          <ul className="flex flex-col gap-1">
            {incident.evidence_ids.map((id) => (
              <li key={id} className="flex items-center gap-1 text-sm">
                <code className="font-mono text-xs">{id}</code>
                <CopyButton value={id} />
              </li>
            ))}
          </ul>
        )}
      </Section>

      <ExecutionsArea incident={incident} locked={locked} onRecover={openRecovery} />

      <Section title="제안 조치">
        {incident.recommendations.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            {incident.status === 'ANALYZING' ? '분석 중' : '실행할 제안이 없습니다.'}
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {incident.recommendations.map((rec) => (
              <li key={rec.runbook_id}>
                <Card className="gap-2 p-4">
                  <p className="font-medium">{RUNBOOK_LABELS[rec.runbook_id]}</p>
                  {/* display_parameters는 자유 형식 Record다 — FE가 key 표시명을 지어내지 않고 원문을 쓴다. */}
                  <dl className="flex flex-col gap-1 text-sm">
                    {Object.entries(rec.display_parameters).map(([key, value]) => (
                      <div key={key} className="flex items-start justify-between gap-3">
                        <dt className="text-muted-foreground font-mono text-xs">{key}</dt>
                        <dd className="text-right">{value}</dd>
                      </div>
                    ))}
                  </dl>
                  <span className="text-muted-foreground font-mono text-xs break-all">
                    {rec.target_arn}
                  </span>
                </Card>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <ProposalActions incident={incident} locked={locked} onExecute={openAction} />

      {/* ACT-002 — 실행 흐름은 여기서 끝난다. 위쪽 판단 근거·근거 데이터·제안 조치는 그대로 남는다(§4.7). */}
      {shownOutcome ? (
        <ExecutionStatusPanel
          execution={shownOutcome.execution}
          replayed={shownOutcome.replayed}
          subjectHref={subjectHref}
          live={connection === 'open'}
          // 접수 행 + WS 전이가 모두 이 state에 들어 있다. outcome에서 파생하면 같은 리스너가
          // outcome을 덮어쓰면서 접수 시각·상태까지 최신값으로 뭉갠다(PR #181 리뷰).
          transitions={shownTransitions}
        />
      ) : null}

      <ActionExecuteDialog
        incident={incident}
        request={request}
        onClose={() => setRequest(null)}
        onExecuted={(next) => {
          setOutcome(next);
          // 진행 기록을 새 실행의 접수 행으로 초기화한다 — 안 지우면 FAILED 후 재실행에서
          // 옛 실행의 줄이 새 패널에 그대로 쌓인다(§4.7이 허용하는 경로다).
          setTransitions([
            { at: next.execution.updated_at, from: null, to: next.execution.status },
          ]);
          // 서버 상태(ACTION_IN_PROGRESS)를 따라오게 한다. 위 locked가 그 사이를 덮는다.
          router.refresh();
        }}
        // 409 PROPOSAL_NOT_EXECUTABLE — 제안이 이미 실행됐거나 무효해졌다. 상세를 다시 읽는다(§4.6).
        onProposalStale={() => router.refresh()}
      />
    </div>
  );
}
