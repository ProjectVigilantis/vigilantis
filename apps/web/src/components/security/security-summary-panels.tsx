'use client';

// SEC-001 보안 요약 — 인터넷 개방 보안 그룹(+노출 포트) · 보안 인시던트 현황 · 외부 위협 출발지.
//
// 「인터넷 개방 보안 그룹」은 종전에 메인 대시보드(DSH-001) 오른쪽 레일의 카드였다(2026-09-28 이전).
// 대시보드는 요약(지표 띠)만 남기고, 어느 SG의 어느 규칙이 몇 대에 걸리는지처럼 **들여다보는 것**은
// 이 화면으로 왔다 — 자산 화면(AST-001)의 헬스 스코어가 그렇듯 띠 바로 아래 첫 줄이 제자리다.
// 셈은 `lib/dashboard`의 `exposureRows`·`lib/security` 그대로다.
//
// 항목·행에 클릭이 있어 클라이언트 컴포넌트다. 개방 SG 목록과 노출 포트는 **한 박스**다(2026-09-30) —
// "어느 SG가 열렸나"와 "무엇이 열렸나"는 같은 노출을 두 방향에서 센 것이라 따로 두면 같은 말을 두 번 한다.
// 보안 인시던트 현황은 박스 없이 목록만 내보낸다 — 화면이 전체 위협 추이와 한 박스에 묶는다.

import Link from 'next/link';

import { Panel } from '@/components/panel';
import { Bar, Muted } from '@/components/panel-parts';
import { StatusBadge } from '@/components/status-badge';
import { exposureRows } from '@/lib/dashboard';
import { INCIDENT_STATUS_LABELS } from '@/lib/enum-labels';
import { exposedPorts, secopsStatusCounts } from '@/lib/security';
import type { ThreatPath } from '@/lib/threat-path';
import type { AssetItem, IncidentListItem, IncidentStatus } from '@/types/api';

/** 개방 SG 목록 — 누르면 그 보안 그룹의 상세(AST-002 Drawer)를 연다. */
export function OpenSgPanel({
  items,
  onSelect,
  className,
}: {
  items: readonly AssetItem[];
  onSelect: (asset: AssetItem) => void;
  className?: string;
}) {
  const exposure = exposureRows(items);
  return (
    <Panel
      className={className}
      title="인터넷 개방 보안 그룹"
      description="전체 대역(0.0.0.0/0 · ::/0)에 열린 인바운드 — 누르면 자산 상세를 엽니다"
    >
      {exposure.length === 0 ? (
        <Muted>인터넷에 열린 보안 그룹이 없습니다.</Muted>
      ) : (
        <div className="flex flex-col gap-4">
          <ul className="flex flex-col gap-2">
            {exposure.map(({ sg, rules, affectedEc2 }) => (
              <li key={sg.arn}>
                <button
                  type="button"
                  onClick={() => onSelect(sg)}
                  className="hover:bg-accent/60 focus-visible:ring-ring/50 flex w-full cursor-pointer flex-col gap-1 rounded-md border p-3 text-left transition-colors focus-visible:ring-2 focus-visible:outline-none"
                >
                  <span className="flex w-full flex-wrap items-center justify-between gap-2">
                    <span className="font-mono text-xs">{sg.name ?? sg.resource_id}</span>
                    {/* verdict가 없으면 판정 대기·실패다 — 사유를 evaluation_status로 적는다. */}
                    {sg.verdict !== null ? (
                      <StatusBadge field="verdict" value={sg.verdict} />
                    ) : (
                      <StatusBadge field="evaluation_status" value={sg.evaluation_status} />
                    )}
                  </span>
                  <span className="text-muted-foreground font-mono text-xs">{rules.join(', ')}</span>
                  <span className="text-muted-foreground text-xs">영향 EC2 {affectedEc2}대</span>
                </button>
              </li>
            ))}
          </ul>
          <ExposedPortList items={items} />
        </div>
      )}
    </Panel>
  );
}

/** 막대 색 — 배지 톤(`INCIDENT_STATUS_LABELS`)과 같은 뜻으로 맞춘다: 기다림은 주황, 끝은 초록, 막힘은 빨강. */
const STATUS_BAR: Record<IncidentStatus, string> = {
  ANALYZING: 'bg-muted-foreground/50',
  AWAITING_APPROVAL: 'bg-amber-400',
  ACTION_IN_PROGRESS: 'bg-amber-400',
  AWAITING_CLOSURE: 'bg-emerald-500',
  RESOLVED: 'bg-muted-foreground/30',
  FAILED: 'bg-danger',
};

/**
 * 보안 인시던트 상태 분포 — 판정 현황(AST-001)과 같은 모양의 막대 목록. 박스는 화면이 준다
 * (전체 위협 추이와 한 박스 — security-view.tsx).
 */
export function SecurityIncidentStatusList({
  incidents,
}: {
  /** 인시던트 목록. `null`은 조회 실패 — 0건과 구분해 그린다. */
  incidents: IncidentListItem[] | null;
}) {
  const counts = secopsStatusCounts(incidents);
  if (counts === null) return <Muted>인시던트를 불러오지 못했습니다.</Muted>;
  const total = counts.reduce((n, c) => n + c.count, 0);
  return (
    <div className="flex flex-col gap-3">
      <p className="text-muted-foreground flex justify-between text-xs">
        <span>보안 인시던트 현황</span>
        <span className="tabular-nums">{total}건</span>
      </p>
      <ul className="flex flex-col gap-2.5 text-xs">
        {counts.map(({ status, count }) => (
          <li key={status} className="flex flex-col gap-1">
            <span className="flex items-center justify-between">
              <span className="text-muted-foreground">{INCIDENT_STATUS_LABELS[status]?.label ?? status}</span>
              <span className="font-mono tabular-nums">{count}</span>
            </span>
            <Bar ratio={total === 0 ? 0 : count / total} className={STATUS_BAR[status]} />
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * 외부 위협 출발지 — 인시던트 목록 계약의 `threat_context`에서 파생한 경로(`lib/threat-path`)를 줄로
 * 적는다. 토폴로지 탭이 같은 경로를 그래프 위에 덧그리는데, 목록으로도 있어야 출발지가 몇 개인지
 * 세어 읽을 수 있다. **관측된 IP와 허용 대역을 한 말로 덮지 않는다**(threat-path.ts 머리말).
 */
export function ThreatSourcePanel({
  paths,
  items,
  className,
}: {
  paths: readonly ThreatPath[];
  /** 대상 ARN → 이름을 찾기 위한 자산 전량. */
  items: readonly AssetItem[];
  className?: string;
}) {
  const byArn = new Map(items.map((a) => [a.arn, a]));
  return (
    <Panel
      className={className}
      title="외부 위협 출발지"
      description="인시던트에 기록된 출발지 — 관측된 공격자 IP와 규칙이 허용한 대역을 가릅니다"
    >
      {paths.length === 0 ? (
        <Muted>외부에서 들어온 경로가 없습니다.</Muted>
      ) : (
        <ul className="flex flex-col text-xs">
          {paths.map((path) => {
            const target = byArn.get(path.targetArn);
            return (
              <li
                key={path.incidentId}
                className="flex flex-wrap items-center gap-2 border-b py-2 last:border-b-0"
              >
                <span className="font-mono">{path.source}</span>
                <span className="text-muted-foreground">{path.observed ? '관측 출발지' : '허용 대역'}</span>
                <span className="text-muted-foreground" aria-hidden>
                  ▶
                </span>
                <Link
                  href={`/incidents/${encodeURIComponent(path.incidentId)}`}
                  className="font-mono hover:underline"
                >
                  {/* 자산 목록에 없는 대상(소멸 자산 등)은 ARN 전체 대신 꼬리만 — 전체는 title로 남긴다. */}
                  <span title={path.targetArn}>
                    {target?.name ?? target?.resource_id ?? path.targetArn.split(/[/:]/).at(-1)}
                  </span>
                </Link>
                <span className="ml-auto">
                  <StatusBadge field="incident_status" value={path.status} />
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

/**
 * 노출 포트 — 인터넷 개방 규칙을 **포트 기준**으로 센 막대(`lib/security` `exposedPorts`). 개방 SG 목록
 * 아래에 붙는다 — 목록이 "어느 SG가 열렸나"라면 이쪽은 "무엇이 열렸나"다. 한 계열이라 범례는 없고,
 * 막대 길이는 가장 많은 포트 기준이다. 값은 글자로도 적는다 — 막대만으로 읽게 두지 않는다.
 */
function ExposedPortList({ items }: { items: readonly AssetItem[] }) {
  const ports = exposedPorts(items);
  const top = ports[0]?.sgCount ?? 0;
  return (
    <div className="flex flex-col gap-2 border-t pt-3">
      <p className="text-muted-foreground text-xs">노출 포트</p>
      <ul className="flex flex-col gap-3 text-xs">
        {ports.map((p) => (
          <li key={p.label} className="flex flex-col gap-1">
            <span className="flex items-baseline justify-between gap-2">
              <span>
                <span className="font-mono">{p.label}</span>
                {p.service !== null ? <span className="text-muted-foreground ml-1.5">{p.service}</span> : null}
              </span>
              <span className="text-muted-foreground tabular-nums">
                SG <span className="text-foreground font-mono">{p.sgCount}</span> · EC2{' '}
                <span className="text-foreground font-mono">{p.affectedEc2}</span>
              </span>
            </span>
            <Bar ratio={top === 0 ? 0 : p.sgCount / top} className="bg-amber-400" />
          </li>
        ))}
      </ul>
    </div>
  );
}
