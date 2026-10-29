'use client';

// MON-001 통합 관제 — 자산 관제(AST-001)와 보안 관제(SEC-001)를 한 화면의 두 축으로 합쳤다(2026-10-29 초안).
//
// 두 화면은 틀이 같았다(지표 띠 → 목록 · 토폴로지 · 인시던트 탭, 부르는 API 3종도 같다). 메뉴를 둘로 두면
// 토폴로지 · 인시던트 탭도 두 벌이 되고, 플랫폼 운영자가 먼저 고를 것이 "어느 고객인가"인데 그 자리가 없었다.
// 그래서 제목 줄에 고객 선택과 축 전환을 두고, 그 아래는 고른 축의 본체(`AssetsView` · `SecurityView`)를 그대로 쓴다.
//
// 축은 URL `?axis=`에도 적는다. 새로고침 · 뒤로 가기 · 링크 공유에서 같은 축으로 돌아와야 해서다. 서버를 다시
// 부르지 않도록 라우터가 아니라 `history.replaceState`로 바꾼다(데이터는 페이지가 두 축 몫을 이미 받았다).
//
// 고객 선택은 아직 잠겨 있다. 자산 · 인시던트 조회가 테넌트 범위를 받는 방법(T-1)이 서버에 없어, 지금 고르게
// 하면 화면이 고객 하나를 보는 척하면서 전체를 보여 준다.

import { useState, type ReactNode } from 'react';

import { AssetsView } from '@/components/assets/assets-view';
import { SecurityView } from '@/components/security/security-view';
import type { MonitoringAxis } from '@/lib/home-path';
import type { IncidentPreset } from '@/lib/incident-filter';
import type { SavingsSummary } from '@/lib/savings';
import { cn } from '@/lib/utils';
import type {
  AssetsResponse,
  IncidentListItem,
  MetricsTimeseriesResponse,
  TenantListItem,
} from '@/types/api';

const AXES: readonly { value: MonitoringAxis; label: string }[] = [
  { value: 'assets', label: '자산' },
  { value: 'security', label: '보안' },
];

function AxisSwitch({ axis, onChange }: { axis: MonitoringAxis; onChange: (next: MonitoringAxis) => void }) {
  return (
    <div role="group" aria-label="관제 축" className="inline-flex rounded-lg border p-0.5">
      {AXES.map(({ value, label }) => (
        <button
          key={value}
          type="button"
          aria-pressed={axis === value}
          onClick={() => onChange(value)}
          className={cn(
            'focus-visible:ring-ring/50 h-8 rounded-md px-4 text-sm font-semibold transition-colors focus-visible:ring-2 focus-visible:outline-none',
            axis === value ? 'bg-primary text-white' : 'text-muted-foreground hover:text-foreground',
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function TenantSelect({ tenants }: { tenants: TenantListItem[] | null }) {
  const total = tenants === null ? null : tenants.length;
  return (
    <label className="flex items-center gap-1.5 text-sm">
      <span className="text-muted-foreground">고객사</span>
      <select
        disabled
        value="ALL"
        aria-describedby="tenant-scope-note"
        className="border-input bg-background rounded-md border px-2 py-1 disabled:opacity-70"
      >
        <option value="ALL">{total === null ? '전체 고객사' : `전체 고객사 (${total})`}</option>
        {(tenants ?? []).map((t) => (
          <option key={t.tenant_id} value={t.tenant_id}>
            {t.name}
          </option>
        ))}
      </select>
      <span id="tenant-scope-note" className="text-muted-foreground text-xs">
        고객사별 보기는 서버 조회 범위(T-1)가 열리면 켭니다
      </span>
    </label>
  );
}

export function MonitoringView({
  initialAxis,
  tenants,
  data,
  incidents,
  incidentsError,
  incidentsByArn,
  metrics,
  savings,
  savingsFailed,
  openArn,
  initialTab,
  initialPreset,
  renderedAt,
}: {
  initialAxis: MonitoringAxis;
  /** 고객 목록. `null`은 조회 실패 — 선택은 어차피 잠겨 있어 이름 없이 `전체 고객`만 적는다. */
  tenants: TenantListItem[] | null;
  data: AssetsResponse;
  incidents: IncidentListItem[] | null;
  incidentsError: unknown;
  incidentsByArn: Record<string, IncidentListItem[]> | null;
  metrics: MetricsTimeseriesResponse | null;
  savings: SavingsSummary;
  savingsFailed: number;
  openArn?: string;
  initialTab?: string;
  initialPreset?: IncidentPreset;
  renderedAt: string;
}) {
  const [axis, setAxis] = useState(initialAxis);

  // 딥링크(`?asset=` · `?tab=` · `?preset=`)는 처음 연 축에만 건다. 축을 바꾸면 그 축의 첫 화면에서 시작한다.
  const linked = (target: MonitoringAxis) =>
    target === initialAxis ? { openArn, initialTab, initialPreset } : {};

  const changeAxis = (next: MonitoringAxis) => {
    setAxis(next);
    const url = new URL(window.location.href);
    url.search = next === 'assets' ? '' : `?axis=${next}`;
    window.history.replaceState(window.history.state, '', url);
  };

  const heading: ReactNode = (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      <h1 className="text-lg font-semibold">관제</h1>
      <AxisSwitch axis={axis} onChange={changeAxis} />
      <TenantSelect tenants={tenants} />
    </div>
  );

  const common = { data, incidents, incidentsError, incidentsByArn, metrics, renderedAt, heading };

  return axis === 'assets' ? (
    <AssetsView key="assets" {...common} {...linked('assets')} savings={savings} savingsFailed={savingsFailed} />
  ) : (
    <SecurityView key="security" {...common} {...linked('security')} />
  );
}
