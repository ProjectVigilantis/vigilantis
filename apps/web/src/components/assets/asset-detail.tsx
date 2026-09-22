'use client';

// AST-002 자산 상세 패널 — 화면설계서 v1.5 §4.3. 별도 API가 없어 AST-001이 받은 items[] 단건만 씁니다.

import Link from 'next/link';

import { CopyButton } from '@/components/copy-button';
import { HealthArea, VerdictArea } from '@/components/assets/asset-card';
import { CpuSparkline, NetworkSparkline } from '@/components/charts/sparkline';
import { Row } from '@/components/detail-row';
import { EnumBadge, StatusBadge } from '@/components/status-badge';
import { Separator } from '@/components/ui/separator';
import {
  ASSET_TYPE_LABELS,
  NO_VALUE,
  SPEC_KEY_LABELS,
  assetStateEntry,
  incidentTitle,
} from '@/lib/enum-labels';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { IDLE_CPU_AVG } from '@/lib/dashboard';
import { cpuPointsFor, networkRowsFor } from '@/lib/metrics-chart';
import { specValueView } from '@/lib/spec-value';
import { formatKst } from '@/lib/utils';
import type {
  AssetItem,
  IncidentListItem,
  MetricsTimeseriesResponse,
  OpenPortRule,
} from '@/types/api';

/** `from_port`·`to_port`가 null이면 포트 지정 없이 전부 열린 규칙이다(계약: nullable). */
function portRuleText(rule: OpenPortRule): string {
  const range =
    rule.from_port === null || rule.to_port === null
      ? '전체 포트'
      : rule.from_port === rule.to_port
        ? String(rule.from_port)
        : `${rule.from_port}-${rule.to_port}`;
  return `${rule.protocol} ${range}${rule.ipv6 ? ' (IPv6)' : ''}`;
}

function isPortRule(value: unknown): value is OpenPortRule {
  return typeof value === 'object' && value !== null && 'protocol' in value;
}

/**
 * spec 값 렌더. 계약의 spec 필드 타입이 유형마다 달라(문자열·수치·불리언·배열·Key→Value)
 * **값 모양으로** 가른다 — 그 분기는 `lib/spec-value`가 정하고 여기서는 모양별 표기만 정한다.
 * `[]`는 `null`과 의미가 다르지만(3.3) 둘 다 화면에는 값이 없으므로 같은 `—`로 적는다.
 */
function SpecValue({ value, threat }: { value: unknown; threat: boolean }) {
  const view = specValueView(value);
  if (view.kind === 'EMPTY') return <span className="text-muted-foreground">{NO_VALUE}</span>;
  if (view.kind === 'BOOLEAN') return <>{view.value ? '예' : '아니오'}</>;
  if (view.kind === 'LIST') {
    return (
      <span className="flex flex-wrap justify-end gap-1">
        {view.items.map((item, i) =>
          isPortRule(item) ? (
            <EnumBadge
              key={i}
              entry={{ label: portRuleText(item), tone: threat ? 'red' : 'gray' }}
            />
          ) : (
            <span key={i} className="font-mono text-xs break-all">
              {String(item)}
            </span>
          ),
        )}
      </span>
    );
  }
  // 태그(`tags`)처럼 Key→Value 로 오는 값. 키는 AWS가 준 철자 그대로라 mono 로 두고,
  // 사람이 읽을 값과 색으로 가른다 — ACT-001 승인 모달의 `display_parameters` 줄과 같은 모양이다.
  if (view.kind === 'MAP') {
    return (
      <span className="flex flex-col items-end gap-0.5">
        {view.entries.map(([key, text]) => (
          <span key={key} className="flex flex-wrap justify-end gap-1.5 text-xs">
            <span className="text-muted-foreground font-mono break-all">{key}</span>
            <span className="break-all">{text}</span>
          </span>
        ))}
      </span>
    );
  }
  return <span className={view.numeric ? 'tabular-nums' : undefined}>{view.text}</span>;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-muted-foreground text-xs font-medium">{title}</h3>
      {children}
    </section>
  );
}

export function AssetDetail({
  asset,
  incidents,
  metrics,
  open,
  onOpenChange,
}: {
  /**
   * 카드에서 고른 항목을 그대로 받는다 — 신규 페치 없음(§4.3).
   * 열림 여부는 `open`이 따로 쥔다. 닫을 때 이 값을 비우면 닫는 애니메이션 동안 패널이
   * 빈 채로 미끄러져 나가므로, 호출부는 다음 선택 전까지 마지막 자산을 그대로 둔다.
   */
  asset: AssetItem | null;
  /** 목록 API의 `subject_arn` 역조인 결과(§3.3). `null`은 조회 실패 — 0건과 구분한다. */
  incidents: IncidentListItem[] | null;
  /**
   * CloudWatch 시계열 3축(`GET /api/v1/metrics/timeseries`). **이 패널도 신규 페치를 하지
   * 않는다**(§4.3) — 화면이 이미 받아 둔 응답에서 이 자산의 줄만 골라 그린다.
   * `null`이면 조회 실패이고, 그때는 스파크라인 자리에 그 사실을 적는다.
   */
  metrics: MetricsTimeseriesResponse | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const state = asset ? assetStateEntry(asset) : null;
  // 메트릭은 EC2 전용이다 — 계약상 SG·EBS 에는 이 곡선이 없다(있는 척하면 빈 차트가 결측처럼 읽힌다).
  const showCharts = asset !== null && asset.asset_type === 'EC2';
  const cpuPoints = showCharts && metrics ? cpuPointsFor(metrics.cpu, asset.arn) : null;
  const networkRows = showCharts && metrics ? networkRowsFor(metrics.network, asset.arn) : null;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-md">
        {asset ? (
          <>
            <SheetHeader className="gap-1">
              <SheetTitle className="pr-8 break-all">{asset.name ?? asset.resource_id}</SheetTitle>
              <SheetDescription>
                {ASSET_TYPE_LABELS[asset.asset_type]?.label ?? asset.asset_type} · {asset.region} ·{' '}
                {asset.account_id}
              </SheetDescription>
            </SheetHeader>

            <div className="flex flex-col gap-1.5 px-4 pb-4">
              {/* state가 null이면 행 자체를 숨긴다(§4.3) — 빈 값을 남기면 상태를 못 받은 것처럼 읽힌다. */}
              {state ? (
                <Row label="상태">
                  <EnumBadge entry={state} />
                </Row>
              ) : null}
              <Row label="헬스">
                <HealthArea score={asset.health_score} />
              </Row>
              {Object.entries(asset.spec).map(([key, value]) => (
                <Row key={key} label={SPEC_KEY_LABELS[key] ?? key}>
                  {/* SG의 open_to_world가 비어 있지 않으면 위협 노출로 강조한다(§4.3 예외). */}
                  <SpecValue
                    value={value}
                    threat={asset.asset_type === 'SG' && key === 'open_to_world'}
                  />
                </Row>
              ))}
              <Row label="ARN">
                <span className="flex items-center justify-end gap-1">
                  <span className="font-mono text-xs break-all">{asset.arn}</span>
                  <CopyButton value={asset.arn} label="ARN 복사" />
                </span>
              </Row>
              <Row label="수집">{formatKst(asset.collected_at)}</Row>
            </div>

            {/* CloudWatch 추이 — EC2 에만 있는 값이라 그 유형에서만 그린다.
                판정(위) 바로 아래에 두는 이유: "저활성"이라는 판정을 그 근거인 곡선과 한
                화면에서 대조해야 다운사이징 승인을 판단할 수 있다. */}
            {showCharts ? (
              <>
                <Separator />
                <div className="flex flex-col gap-4 p-4">
                  <Section title={`CPU 추이 (저활성 임계 ${metrics?.cpu.idle_cpu_avg_threshold ?? IDLE_CPU_AVG}% 파선)`}>
                    {metrics === null ? (
                      <p className="text-muted-foreground text-xs">
                        추이를 불러오지 못했습니다. 새로고침하면 다시 조회합니다.
                      </p>
                    ) : (
                      <CpuSparkline points={cpuPoints} threshold={metrics.cpu.idle_cpu_avg_threshold} />
                    )}
                  </Section>
                  <Section title="네트워크 추이 (수신·송신, 초당)">
                    {metrics === null ? (
                      <p className="text-muted-foreground text-xs">
                        추이를 불러오지 못했습니다. 새로고침하면 다시 조회합니다.
                      </p>
                    ) : (
                      <NetworkSparkline rows={networkRows} />
                    )}
                  </Section>
                </div>
              </>
            ) : null}

            {/* NOT_APPLICABLE(NACL·ASG·LT·TG)은 Rule 판정 블록 전체를 숨긴다(§4.3 예외).
                판정 사유 코드는 계약에 필드가 없어 verdict 배지만 표시한다(9장 #6). */}
            {asset.evaluation_status === 'NOT_APPLICABLE' ? null : (
              <>
                <Separator />
                <div className="p-4">
                  <Section title="Rule 판정">
                    <div>
                      <VerdictArea asset={asset} />
                    </div>
                  </Section>
                </div>
              </>
            )}

            <Separator />
            <div className="p-4">
              <Section title="연결된 인시던트">
                {incidents === null ? (
                  // 조회 실패를 "없습니다"로 적으면 관제자가 0건으로 읽는다 — 확인 못 한 상태로 남긴다.
                  <p className="text-muted-foreground text-sm">
                    인시던트를 불러오지 못했습니다. 새로고침하면 다시 조회합니다.
                  </p>
                ) : incidents.length === 0 ? (
                  <p className="text-muted-foreground text-sm">연결된 인시던트가 없습니다.</p>
                ) : (
                  // 행 클릭 → INC-002(§4.3 액션).
                  incidents.map((incident) => (
                    <Link
                      key={incident.incident_id}
                      href={`/incidents/${encodeURIComponent(incident.incident_id)}`}
                      className="hover:border-ring focus-visible:ring-ring/50 flex flex-col gap-1.5 rounded-md border p-3 transition-colors focus-visible:ring-2 focus-visible:outline-none"
                    >
                      <div className="flex flex-wrap items-center gap-1">
                        <StatusBadge field="category" value={incident.category} />
                        <StatusBadge field="incident_status" value={incident.status} />
                      </div>
                      <p className="text-sm">{incidentTitle(incident)}</p>
                    </Link>
                  ))
                )}
              </Section>
            </div>

            {/* 실행 버튼은 두지 않는다 — 실행은 항상 인시던트 문맥에서 시작한다(계약이 incident_id를 요구, §4.3). */}
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
