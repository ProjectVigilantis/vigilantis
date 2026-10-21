// 고객 목록 그래프 3종. 전부 GET /tenants 응답 하나로 그린다(시계열은 계약에 없다).
// 형태 · 색 규칙은 DESIGN.md §데이터 시각화를 따른다: 크기는 --data 한 계열, 상태는 상태 색 + 글자.
// 마우스를 올리면 보이는 설명은 `title`로 단다.

import Link from 'next/link';

import { ChartCard } from '@/components/chart-card';
import { statusFill as fillOf } from '@/lib/status-fill';
import {
  TENANT_STATUS_LABELS,
  VERIFICATION_LABELS,
  VERIFICATION_STALE_HOURS,
  type TenantSummary,
  type VerificationAge,
} from '@/lib/tenants';
import { cn, formatKst } from '@/lib/utils';
import type { TenantListItem, TenantStatus } from '@/types/api';

const STATUS_ORDER: readonly TenantStatus[] = ['ACTIVE', 'PENDING_CONNECTION', 'SUSPENDED'];

/** 고객 상태 구성: 한 줄 누적 막대와 건수 범례. */
export function StatusComposition({ summary }: { summary: TenantSummary }) {
  const parts = STATUS_ORDER.map((status) => ({
    status,
    count: summary.byStatus[status],
    label: TENANT_STATUS_LABELS[status].label,
    fill: fillOf(TENANT_STATUS_LABELS[status].tone).fill,
  }));
  const shown = parts.filter((p) => p.count > 0);
  const described = parts.map((p) => `${p.label} ${p.count}곳`).join(', ');

  return (
    <ChartCard title="고객 상태 구성" description={`전체 ${summary.total}곳`}>
      {summary.total === 0 ? (
        <div className="bg-heat-none h-7 rounded-[4px]" />
      ) : (
        <div role="img" aria-label={described} className="flex h-7 gap-0.5">
          {shown.map((p, i) => (
            <div
              key={p.status}
              title={`${p.label} ${p.count}곳`}
              className={cn(p.fill, i === 0 && 'rounded-l-[4px]', i === shown.length - 1 && 'rounded-r-[4px]')}
              style={{ width: `${(p.count / summary.total) * 100}%` }}
            />
          ))}
        </div>
      )}
      <ul className="flex flex-col gap-2.5">
        {parts.map((p) => (
          <li key={p.status} className="flex items-center justify-between gap-2 text-sm">
            <span className="inline-flex items-center gap-2">
              <span aria-hidden className={cn('size-2.5 rounded-[3px]', p.fill)} />
              {p.label}
            </span>
            <span className="font-bold tabular-nums">{p.count}</span>
          </li>
        ))}
      </ul>
    </ChartCard>
  );
}

/** 가로축 끝(시간). 기준선이 가운데 오도록 기준의 두 배로 둔다. */
const AGE_AXIS_HOURS = VERIFICATION_STALE_HOURS * 2;

function formatHours(hours: number): string {
  return hours < 1 ? `${Math.round(hours * 60)}분` : `${hours.toFixed(1)}시간`;
}

/** 마지막 연결 확인 후 지난 시간: 고객마다 점 하나, 기준선보다 오른쪽이면 낡은 확인이다. */
export function VerificationAgeChart({
  ages,
  unchecked,
  className,
}: {
  ages: readonly VerificationAge[];
  /** 확인 기록이 없어 점이 없는 고객 수. */
  unchecked: number;
  className?: string;
}) {
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((r) => r * AGE_AXIS_HOURS);

  return (
    <ChartCard
      className={className}
      title="마지막 연결 확인 후 지난 시간"
      description={`점선(${VERIFICATION_STALE_HOURS}시간)보다 오른쪽이면 확인이 오래된 고객입니다.`}
    >
      {ages.length === 0 ? (
        <p className="text-muted-foreground text-sm">연결 확인 기록이 있는 고객이 없습니다.</p>
      ) : (
        <div className="flex flex-col gap-2.5">
          {ages.map((a) => {
            const entry = VERIFICATION_LABELS[a.result];
            const style = fillOf(entry.tone);
            const over = a.hours > AGE_AXIS_HOURS;
            const left = (Math.min(a.hours, AGE_AXIS_HOURS) / AGE_AXIS_HOURS) * 100;
            return (
              <div key={a.tenantId} className="grid grid-cols-[96px_minmax(0,1fr)_112px] items-center gap-3">
                <Link
                  href={`/tenants/${encodeURIComponent(a.tenantId)}`}
                  className="truncate text-[13px] font-semibold hover:underline"
                >
                  {a.name}
                </Link>
                <div className="relative h-6">
                  <div className="bg-gridline absolute inset-x-0 top-[11px] h-0.5" />
                  <div className="border-axis absolute inset-y-0 left-1/2 border-l border-dashed" />
                  <div
                    title={`${a.name} · ${formatKst(a.verifiedAt)} 확인 · ${formatHours(a.hours)} 지남${over ? `(${AGE_AXIS_HOURS}시간 넘음)` : ''} · ${entry.label}`}
                    className="absolute top-0 flex size-6 -translate-x-1/2 items-center justify-center"
                    style={{ left: `${left}%` }}
                  >
                    <span className={cn('ring-card size-3 rounded-full ring-2', style.fill)} />
                  </div>
                </div>
                <span className={cn('text-xs font-semibold', style.text)}>{entry.label}</span>
              </div>
            );
          })}
          <div className="text-muted-foreground grid grid-cols-[96px_minmax(0,1fr)_112px] gap-3 text-[11px]">
            <span />
            <div className="flex justify-between">
              {ticks.map((h) => (
                <span key={h}>{h === 0 ? '지금' : `${h}시간`}</span>
              ))}
            </div>
            <span />
          </div>
        </div>
      )}
      {unchecked > 0 ? (
        <p className="text-muted-foreground text-xs">아직 연결 확인 전인 고객 {unchecked}곳은 점이 없습니다.</p>
      ) : null}
    </ChartCard>
  );
}

/** 고객별 열린 인시던트: 관제 중인 고객만, 많은 순. */
export function OpenIncidentBars({ tenants }: { tenants: readonly TenantListItem[] }) {
  const rows = tenants
    .filter((t) => t.status === 'ACTIVE')
    .sort((a, b) => b.open_incident_count - a.open_incident_count);
  const max = Math.max(1, ...rows.map((t) => t.open_incident_count));

  return (
    <ChartCard title="고객별 열린 인시던트" description="관제 중인 고객만">
      {rows.length === 0 ? (
        <p className="text-muted-foreground text-sm">관제 중인 고객이 없습니다.</p>
      ) : (
        <div className="flex flex-col gap-3">
          {rows.map((t) => (
            <div
              key={t.tenant_id}
              title={`${t.name} · 열린 인시던트 ${t.open_incident_count}건`}
              className="grid grid-cols-[88px_minmax(0,1fr)_28px] items-center gap-2.5"
            >
              <span className="truncate text-[13px]">{t.name}</span>
              <div className="bg-data-track h-[18px] rounded-[4px]">
                <div
                  className="bg-data h-[18px] rounded-r-[4px]"
                  style={{ width: `${(t.open_incident_count / max) * 100}%` }}
                />
              </div>
              <span className="text-right text-[13px] font-bold tabular-nums">{t.open_incident_count}</span>
            </div>
          ))}
        </div>
      )}
    </ChartCard>
  );
}
