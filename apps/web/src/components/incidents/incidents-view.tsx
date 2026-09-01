'use client';

// 인시던트 목록 본체 — 프리셋·상태 필터·카드 그리드를 담습니다(화면설계서 v1.6 §4.4).
// INC-001 보안과 INC-004 자산이 이 컴포넌트를 공유한다 — 다른 건 `category`와 선제차단 프리셋뿐이다.

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useMemo, useRef, useState } from 'react';

import {
  ActionExecuteDialog,
  type ActionRequest,
} from '@/components/incidents/action-execute-dialog';
import { EmptyState } from '@/components/empty-state';
import { ErrorState } from '@/components/error-state';
import { FilterSelect } from '@/components/filter-select';
import { IncidentCard } from '@/components/incidents/incident-card';
import { Badge } from '@/components/ui/badge';
import { getIncident, newIdempotencyKey } from '@/lib/api/client';
import { INCIDENT_STATUS_LABELS, RISK_LEVEL_LABELS } from '@/lib/enum-labels';
import {
  ALL,
  byPreset,
  clampOption,
  PRESET_SLUG,
  riskOptionsOf,
  statusOptionsOf,
  visibleIncidents,
  type IncidentPreset,
} from '@/lib/incident-filter';
import { cn } from '@/lib/utils';
import type { IncidentListItem, IncidentResponse } from '@/types/api';

/**
 * 프리셋은 **필터만 다르고 정렬은 같다**(§4.4). 링크로 두는 이유는 `승인 대기`·`히스토리`가
 * 서버 필터(`?status=`)로 부르는 프리셋이기 때문이다 — 클라이언트에서만 걸러내면 계약이 정한
 * 서버 필터를 쓰지 않는 화면이 된다. `선제차단`은 `response_mode`에 서버 필터가 없어
 * 클라이언트가 거르지만(§4.4), 링크 형태를 갈라 두면 어느 것이 주소로 남는지 알 수 없어진다.
 */
function Preset({
  href,
  active,
  children,
}: {
  href: string;
  active: boolean;
  children: React.ReactNode;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors',
        active ? 'bg-muted text-foreground font-medium' : 'text-muted-foreground hover:text-foreground',
      )}
    >
      {children}
    </Link>
  );
}

export function IncidentsView({
  items,
  preset,
  basePath,
  showPreemptive,
  pendingCount,
  preemptiveCount,
}: {
  items: IncidentListItem[];
  /** 현재 프리셋. `ACTIVE`가 기본이며 `전체` 칸은 없다(§4.4). */
  preset: IncidentPreset;
  /** 프리셋 링크가 붙을 경로 — 보안 `/incidents`, 자산 `/asset-incidents`. */
  basePath: string;
  /** FINOPS에는 `response_mode`가 없어 선제차단 프리셋을 두지 않는다. */
  showPreemptive: boolean;
  /** 셀 수 없는 응답이면 null — 배지를 감춘다. */
  pendingCount: number | null;
  preemptiveCount: number | null;
}) {
  const router = useRouter();
  const [status, setStatus] = useState<string>(ALL);
  const [risk, setRisk] = useState<string>(ALL);

  /**
   * ACT-001 모달은 **목록 전체에 하나**다. 카드마다 두면 인스턴스가 목록 수만큼 생기고
   * 멱등 키도 그만큼 만들어진다 — §4.6은 모달 인스턴스당 키 1개를 전제한다.
   */
  const [modal, setModal] = useState<{ incident: IncidentResponse; request: ActionRequest } | null>(
    null,
  );
  /** 상세를 조회 중인 카드. 누른 카드만 잠근다. */
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [openError, setOpenError] = useState<unknown>(null);
  /**
   * 마지막으로 누른 요청의 표식. A 조회가 끝나기 전에 B를 누르면 **늦게 도착한 A의 응답이
   * B를 덮어써 A의 실행 창이 열릴 수 있다** — 잘못된 대상의 조치를 승인하게 되는 경로다
   * (PR #180 리뷰). 표식이 어긋난 응답은 버린다.
   */
  const latestOpen = useRef(0);

  /**
   * 목록 계약에 `recommendations`가 없어(`api.ts:286`) 버튼을 누른 시점에 상세를 부른다.
   * §4.4도 "추천·복구 버튼 유무 배지는 건별 상세 조회로 보강"으로 이 경로를 전제한다.
   */
  async function openExecute(incidentId: string) {
    const token = ++latestOpen.current;
    setOpeningId(incidentId);
    setOpenError(null);
    try {
      const incident = await getIncident(incidentId);
      if (latestOpen.current !== token) return; // 이전 선택의 응답 — 버린다
      // 조회 사이에 상태가 바뀌었으면 모달을 열지 않는다 — 실행 잠금은 §4.5가 정한 규칙이고,
      // 후보가 비어 있으면 고를 것이 없는 모달이 뜬다.
      if (incident.status === 'ACTION_IN_PROGRESS' || incident.recommendations.length === 0) {
        router.push(`/incidents/${encodeURIComponent(incidentId)}`);
        return;
      }
      setModal({
        incident,
        request: {
          // 모달을 여는 이 시점에 1회 생성해 인스턴스 수명 동안 고정한다(§4.6).
          idempotencyKey: newIdempotencyKey(),
          variant: 'ACTION',
          candidates: incident.recommendations.map((r) => ({
            runbookId: r.runbook_id,
            targetArn: r.target_arn,
            displayParameters: r.display_parameters,
          })),
        },
      });
    } catch (error) {
      // 실패한 채로 열면 후보 없는 모달이 된다 — 열지 않고 §4.9 규칙대로 오류만 그린다.
      if (latestOpen.current === token) setOpenError(error);
    } finally {
      // 이전 선택의 finally가 새 선택의 진행 표시를 끄지 않게 한다.
      if (latestOpen.current === token) setOpeningId(null);
    }
  }

  // 셀렉트 옵션은 **프리셋이 거른 뒤의** 목록에 실제로 있는 값만, 순서는 계약 상수 순서다.
  // 프리셋 전 목록으로 세면 지금 보이지 않는 상태가 옵션에 남는다(§lib/incident-filter).
  const inPreset = useMemo(() => byPreset(items, preset), [items, preset]);
  const statusOptions = useMemo(() => statusOptionsOf(inPreset), [inPreset]);
  // 위험도 셀렉트는 값이 있을 때만 그린다 — FINOPS는 계약이 두 위험도를 null로 강제해 늘 빈다.
  const riskOptions = useMemo(() => riskOptionsOf(inPreset), [inPreset]);
  // 프리셋 전환에서 살아남은 필터가 지금 목록에 없는 값이면 `전체`로 접는다(§lib/incident-filter).
  // 셀렉트 표시값도 이 값을 써야 화면과 실제 필터가 어긋나지 않는다.
  const effectiveStatus = clampOption(status, statusOptions);
  const effectiveRisk = clampOption(risk, riskOptions);
  const visible = useMemo(
    () => visibleIncidents(items, preset, status, risk),
    [items, preset, status, risk],
  );
  const pendingOnly = preset === 'PENDING';
  const presetHref = (p: IncidentPreset) =>
    PRESET_SLUG[p] === null ? basePath : `${basePath}?preset=${PRESET_SLUG[p]}`;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        {/* 왼쪽 = 무엇을 볼지(프리셋), 오른쪽 = 어떻게 거를지(상태). 두 축이 한 줄에 섞이면
            어느 것이 목록을 갈아끼우고 어느 것이 그 안을 좁히는지 구분되지 않는다(§4.4).
            `전체` 칸은 없다 — 켠 프리셋을 다시 눌러 끄면 기본(진행 중 전량)으로 돌아온다. */}
        <nav className="flex items-center gap-1" aria-label="프리셋">
          <Preset href={presetHref(pendingOnly ? 'ACTIVE' : 'PENDING')} active={pendingOnly}>
            승인 대기
            {/* 대기 건수 배지 — 이 프리셋에 뜨는 건 전부 지금 누를 수 있는 건이다.
                계약이 AWAITING_APPROVAL을 "실행 가능한 제안 ≥ 1 · 진행 중 실행 없음"으로 강제한다.
                셀 수 없는 응답(서버가 status로 걸러 준 것)에서는 배지를 감춘다 — 0은 거짓말이 된다. */}
            {pendingCount !== null ? <Badge variant="secondary">{pendingCount}</Badge> : null}
          </Preset>
          {/* 선제차단 = 승인 없이 이미 격리된 건. `승인 대기`와 성격이 반대라(누를 일이 아니라
              정당성을 판단할 일) 상태 필터에 묻지 않고 앞에 세운다(§4.4). FINOPS에는 없다. */}
          {showPreemptive ? (
            <Preset
              href={presetHref(preset === 'PREEMPTIVE' ? 'ACTIVE' : 'PREEMPTIVE')}
              active={preset === 'PREEMPTIVE'}
            >
              선제차단
              {preemptiveCount !== null ? (
                <Badge variant="secondary">{preemptiveCount}</Badge>
              ) : null}
            </Preset>
          ) : null}
          <Preset
            href={presetHref(preset === 'HISTORY' ? 'ACTIVE' : 'HISTORY')}
            active={preset === 'HISTORY'}
          >
            히스토리
          </Preset>
        </nav>

        <div className="flex flex-wrap items-center gap-3">
          {/* 위험도는 `initial_risk_level` — 정렬 축과 같은 불변 키다(§4.4).
              옵션이 없으면(자산 인시던트) 셀렉트 자체를 감춘다. */}
          {riskOptions.length > 0 ? (
            <FilterSelect
              label="위험도"
              value={effectiveRisk}
              options={[
                { value: ALL, label: ALL },
                ...riskOptions.map((r) => ({
                  value: r,
                  label: RISK_LEVEL_LABELS[r]?.label ?? r,
                })),
              ]}
              onChange={setRisk}
            />
          ) : null}
          {/* 위험도와 같은 규칙 — 고를 값이 없으면 셀렉트를 그리지 않는다.
              `승인 대기` 프리셋처럼 목록이 한 상태로만 채워지면 옵션이 `전체` 하나만 남아,
              누를 수는 있지만 아무것도 바뀌지 않는 셀렉트가 된다. */}
          {statusOptions.length > 0 ? (
            <FilterSelect
              label="상태"
              value={effectiveStatus}
              options={[
                { value: ALL, label: ALL },
                ...statusOptions.map((s) => ({
                  value: s,
                  label: INCIDENT_STATUS_LABELS[s]?.label ?? s,
                })),
              ]}
              onChange={setStatus}
            />
          ) : null}
        </div>
      </div>

      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
        {/* 프리셋이 이미 거른 뒤의 모수로 센다 — 전체 응답 수와 비교하면 "3 / 12건"처럼
            지금 화면과 무관한 분모가 붙는다. */}
        <span aria-live="polite">
          {visible.length === inPreset.length
            ? `${inPreset.length}건`
            : `${visible.length} / ${inPreset.length}건`}
        </span>
        {preset === 'HISTORY' ? (
          // 종료된 건은 실행 버튼이 없다 — 계약이 RESOLVED면 recommendations를 비우기 때문이다.
          // 그 강제가 "종료해도 제안을 폐기하지 않는다"는 v1.6 결정과 충돌한다(9장 #32).
          <span>종료된 건 · 실행 버튼 없음 (이름은 종료 전과 같다)</span>
        ) : null}
      </div>

      {/* 카드 1건의 조회 실패다 — **인라인으로 강제한다.** 이 자리가 받을 수 있는 두 코드가
          `INCIDENT_NOT_FOUND`·`INTERNAL_ERROR`로 **둘 다 `page`** 라(error-state.tsx:24·38),
          code별 기본에 맡기면 목록 위에 전체 오류 화면이 뜨고 `목록으로` 버튼이 목록에 붙는다.
          `page` 매핑은 화면 전체가 그 인시던트인 `/incidents/[id]`를 위한 것이다(PR #180 리뷰). */}
      {openError !== null ? <ErrorState error={openError} variant="inline" /> : null}

      {visible.length === 0 ? (
        <EmptyState
          message={
            preset === 'PENDING'
              ? '승인을 기다리는 인시던트가 없습니다.'
              : preset === 'PREEMPTIVE'
                ? '선제 차단된 인시던트가 없습니다.'
                : preset === 'HISTORY'
                  ? '종료된 인시던트가 없습니다.'
                  : '처리할 인시던트가 없습니다.'
          }
          description={
            // "지금 할 일이 없다"도 관제 정보다(§3.1) — 필터 탓으로 돌리지 않는다.
            effectiveStatus === ALL ? undefined : '상태 필터를 바꾸면 다른 인시던트를 볼 수 있습니다.'
          }
        />
      ) : (
        // 페이지네이션은 MVP 계약에 없다 — 응답 전량을 렌더한다(§3.1.1·§4.4 예외).
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {visible.map((incident) => (
            <IncidentCard
              key={incident.incident_id}
              incident={incident}
              showExecute={pendingOnly}
              executePending={openingId === incident.incident_id}
              onExecute={openExecute}
            />
          ))}
        </div>
      )}

      {/* 실행 결과는 INC-002 하단 ACT-002가 그린다 — 목록에는 만들지 않는다(§4.4·§4.7).
          판단 근거가 없는 자리에 실행 상태만 띄우면 근거 없이 후속 판단을 하게 된다.
          설계서 §2.2가 대시보드 경로에 정해 둔 "시작한 화면에서 INC-002로 이동"과 같다. */}
      {modal !== null ? (
        <ActionExecuteDialog
          incident={modal.incident}
          request={modal.request}
          onClose={() => setModal(null)}
          onExecuted={(outcome) => {
            const id = encodeURIComponent(modal.incident.incident_id);
            router.push(`/incidents/${id}?execution=${encodeURIComponent(outcome.execution.execution_id)}`);
          }}
          // 409 PROPOSAL_NOT_EXECUTABLE — 제안이 이미 실행됐거나 무효해졌다. 목록을 다시 읽는다.
          onProposalStale={() => {
            setModal(null);
            router.refresh();
          }}
        />
      ) : null}
    </div>
  );
}
