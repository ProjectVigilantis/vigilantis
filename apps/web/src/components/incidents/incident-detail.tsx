// INC-002 인시던트 상세 본문 — 화면설계서 v1.5 §4.5. A 변형(FINOPS) 기준으로 세운 공통 골격입니다.

import Link from 'next/link';

import { CopyButton } from '@/components/copy-button';
import { EmptyState } from '@/components/empty-state';
import { StatusBadge } from '@/components/status-badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { RUNBOOK_LABELS, incidentTitle } from '@/lib/enum-labels';
import { formatKst } from '@/lib/utils';
import type { AssetItem, IncidentResponse } from '@/types/api';

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

export function IncidentDetail({
  incident,
  subject,
}: {
  incident: IncidentResponse;
  /** `subject_arn` → `GET /assets`의 `arn` 조인 결과(§4.5). 조회 실패·미수집이면 null이다. */
  subject: AssetItem | null;
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

      {/* FINOPS는 위험도 2필드·response_mode가 전부 null이라 위험도 영역을 렌더하지 않는다(§3.3).
          SECOPS 위험도 블록과 B-Medium 카운트다운은 #139에서 붙인다. */}

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
