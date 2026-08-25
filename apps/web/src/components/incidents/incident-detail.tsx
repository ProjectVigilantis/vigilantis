// INC-002 인시던트 상세 본문 — 화면설계서 v1.5 §4.5. A 변형(FINOPS) 기준으로 세운 공통 골격입니다.

import Link from 'next/link';

import { CopyButton } from '@/components/copy-button';
import { Row } from '@/components/detail-row';
import { TimeoutCountdown } from '@/components/incidents/timeout-countdown';
import { EmptyState } from '@/components/empty-state';
import { ExecutionStatusBadge, StatusBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { RUNBOOK_LABELS, incidentTitle } from '@/lib/enum-labels';
import { formatKst } from '@/lib/utils';
import type { AssetItem, IncidentResponse, IsoDateTime } from '@/types/api';

/**
 * 계약 합의(2026-08-14): `AGENT_WAIT` 전환 이벤트의 `occurred_at` + 60초.
 * `'use client'` 모듈에 두면 서버 컴포넌트가 값 대신 클라이언트 참조를 받아 계산이 NaN이 된다.
 */
const AGENT_WAIT_TIMEOUT_MS = 60_000;

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
      <TimeoutNotice incident={incident} agentWaitAt={agentWaitAt} />
    </Section>
  );
}

/**
 * B-Medium 타임아웃 고지(§4.5) — 승인 전까지 조치는 수행되지 않지만, 1분 미응답이면 서버가
 * `TIMEOUT_ISOLATION_1M`으로 자동 격리한다는 사실을 **대기 중에 미리** 알린다.
 *
 * Low는 같은 `AGENT_WAIT`이지만 타임아웃 자동 격리가 없어 이 블록을 뺀다(§4.5 · SSOT §3단계 위험 대응).
 * ○ FE 판단: 등급은 정밀 평가가 나왔으면 그 값을, 아니면 초기 판정을 쓴다 — 어느 필드가 타임아웃을
 * 가르는지는 계약에 없다(OPEN: BE 확인 필요).
 */
function TimeoutNotice({
  incident,
  agentWaitAt,
}: {
  incident: IncidentResponse;
  agentWaitAt: IsoDateTime | null;
}) {
  const level = incident.reviewed_risk_level ?? incident.initial_risk_level;
  if (
    incident.response_mode !== 'AGENT_WAIT' ||
    level === 'LOW' ||
    incident.status !== 'AWAITING_APPROVAL'
  ) {
    return null;
  }

  return (
    <p className="border-danger/30 bg-danger/5 flex flex-wrap items-center gap-1 rounded-md border p-3 text-sm">
      <span>승인 전까지 조치는 수행되지 않습니다.</span>
      {agentWaitAt !== null ? (
        // 기준은 `AGENT_WAIT` 전환을 알린 INCIDENT_UPDATED의 occurred_at이다 —
        // **수신 시각을 쓰지 않는다**(합의 2026-08-14). 수신 기준이면 재접속마다 시간이 늘어난다.
        <TimeoutCountdown deadline={Date.parse(agentWaitAt) + AGENT_WAIT_TIMEOUT_MS} />
      ) : (
        // 전환 이벤트를 받지 못한 인시던트(재접속 등)는 카운트다운 없이 고정 안내문을 쓴다(§4.5).
        <span className="text-danger font-medium">
          1분 안에 응답하지 않으면 서버가 자동으로 격리합니다.
        </span>
      )}
    </p>
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
 * 비가역인 것이 아니라 "백업 기반 롤백이 붙어 있느냐"만 나타낸다. 실행 요청 구성·멱등 키는
 * ACT-001 소관이라 여기서는 자리만 둔다.
 */
function ExecutionsArea({ incident }: { incident: IncidentResponse }) {
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
                  <Button key={runbookId} type="button" variant="outline" size="sm" disabled>
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

export function IncidentDetail({
  incident,
  subject,
  agentWaitAt = null,
}: {
  incident: IncidentResponse;
  /** `subject_arn` → `GET /assets`의 `arn` 조인 결과(§4.5). 조회 실패·미수집이면 null이다. */
  subject: AssetItem | null;
  /**
   * `AGENT_WAIT` 전환을 알린 `INCIDENT_UPDATED`의 `occurred_at`. 카운트다운의 유일한 기준이다.
   * WS 미연동 구간이라 지금은 항상 null이며, 그때는 고정 안내문으로 대체한다(§4.5).
   */
  agentWaitAt?: IsoDateTime | null;
}) {
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

      <RiskArea incident={incident} agentWaitAt={agentWaitAt} />

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

      <ExecutionsArea incident={incident} />

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

      {/* 버튼 노출 규칙은 §4.5 표가 기준이다. 여기서는 자리만 두고, 요청 구성·멱등 키·파괴적 조치
          경고는 ACT-001(#140)에서 붙인다 — 지금 누르면 아무 일도 일어나지 않아야 한다. */}
      {incident.recommendations.length > 0 ? (
        <div className="flex items-center gap-3">
          <Button type="button" disabled>
            이 조치 실행
          </Button>
          <span className="text-muted-foreground text-xs">실행은 ACT-001 연결 후 활성화됩니다.</span>
        </div>
      ) : null}
    </div>
  );
}
