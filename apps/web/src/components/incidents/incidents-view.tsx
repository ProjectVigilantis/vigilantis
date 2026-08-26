'use client';

// INC-001 인시던트 목록 본체 — 프리셋·필터·카드 그리드를 담습니다(화면설계서 v1.5 §4.4).

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
import { CATEGORY_LABELS, INCIDENT_STATUS_LABELS } from '@/lib/enum-labels';
import {
  ALL,
  categoryOptionsOf,
  clampOption,
  statusOptionsOf,
  visibleIncidents,
} from '@/lib/incident-filter';
import { cn } from '@/lib/utils';
import type { IncidentListItem, IncidentResponse } from '@/types/api';

/**
 * 프리셋은 **필터만 다르고 정렬은 같다**(§4.4). `승인 대기`는 서버 필터(`?status=`)로 부르므로
 * 링크로 둔다 — 클라이언트에서 걸러내면 계약이 정한 서버 필터를 쓰지 않는 화면이 된다.
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
  pendingOnly,
  otherStatusFilter,
  pendingCount,
}: {
  items: IncidentListItem[];
  /** `승인 대기` 프리셋 여부. 서버가 `?status=AWAITING_APPROVAL`로 걸러 준 결과다. */
  pendingOnly: boolean;
  /** 두 프리셋 밖의 유효한 `?status=` 값. 있으면 어느 프리셋도 활성이 아니다. */
  otherStatusFilter: string | null;
  /** 셀 수 없는 응답이면 null — 배지를 감춘다. */
  pendingCount: number | null;
}) {
  const router = useRouter();
  const [category, setCategory] = useState<string>(ALL);
  const [status, setStatus] = useState<string>(ALL);

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

  // 셀렉트 옵션은 응답에 실제로 있는 값만, 순서는 계약 상수 순서다(§lib/incident-filter).
  const statusOptions = useMemo(() => statusOptionsOf(items), [items]);
  const categoryOptions = useMemo(() => categoryOptionsOf(items), [items]);
  // 프리셋 전환에서 살아남은 필터가 지금 목록에 없는 값이면 `전체`로 접는다(§lib/incident-filter).
  // 셀렉트 표시값도 이 값을 써야 화면과 실제 필터가 어긋나지 않는다.
  const effectiveStatus = clampOption(status, statusOptions);
  const effectiveCategory = clampOption(category, categoryOptions);
  const visible = useMemo(
    () => visibleIncidents(items, category, status),
    [items, category, status],
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <nav className="flex items-center gap-1">
          <Preset href="/incidents" active={!pendingOnly && otherStatusFilter === null}>
            {ALL}
          </Preset>
          <Preset href="/incidents?status=AWAITING_APPROVAL" active={pendingOnly}>
            승인 대기
            {/* 대기 건수 배지 — 이 프리셋에 뜨는 건 전부 지금 누를 수 있는 건이다.
                계약이 AWAITING_APPROVAL을 "실행 가능한 제안 ≥ 1 · 진행 중 실행 없음"으로 강제한다.
                셀 수 없는 응답(두 프리셋 밖 필터)에서는 배지를 감춘다 — 0은 거짓말이 된다. */}
            {pendingCount !== null ? <Badge variant="secondary">{pendingCount}</Badge> : null}
          </Preset>
        </nav>

        <div className="flex flex-wrap items-center gap-3">
          <FilterSelect
            label="유형"
            value={effectiveCategory}
            options={[
              { value: ALL, label: ALL },
              ...categoryOptions.map((c) => ({ value: c, label: CATEGORY_LABELS[c]?.label ?? c })),
            ]}
            onChange={setCategory}
          />
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
        </div>
      </div>

      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
        {/* 두 프리셋 어느 쪽도 아닌 필터가 걸려 있으면 그 사실을 말한다 — 프리셋 표시만으로는
            지금 무엇이 걸러진 목록인지 알 수 없다(PR #171 리뷰). */}
        {otherStatusFilter !== null ? (
          <span>
            서버 필터 <code className="font-mono">status={otherStatusFilter}</code> 적용됨 ·{' '}
            <Link href="/incidents" className="underline underline-offset-4">
              전체 보기
            </Link>
          </span>
        ) : null}
        <span aria-live="polite">
          {visible.length === items.length
            ? `${items.length}건`
            : `${visible.length} / ${items.length}건`}
        </span>
      </div>

      {/* 카드 1건의 조회 실패다 — **인라인으로 강제한다.** 이 자리가 받을 수 있는 두 코드가
          `INCIDENT_NOT_FOUND`·`INTERNAL_ERROR`로 **둘 다 `page`** 라(error-state.tsx:24·38),
          code별 기본에 맡기면 목록 위에 전체 오류 화면이 뜨고 `목록으로` 버튼이 목록에 붙는다.
          `page` 매핑은 화면 전체가 그 인시던트인 `/incidents/[id]`를 위한 것이다(PR #180 리뷰). */}
      {openError !== null ? <ErrorState error={openError} variant="inline" /> : null}

      {visible.length === 0 ? (
        <EmptyState
          message={pendingOnly ? '승인을 기다리는 인시던트가 없습니다.' : '조건에 맞는 인시던트가 없습니다.'}
          description={pendingOnly ? undefined : '필터를 바꾸면 다른 인시던트를 볼 수 있습니다.'}
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
