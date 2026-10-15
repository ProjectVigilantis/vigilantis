'use client';

// 인시던트 목록 본체 — 프리셋·상태 필터·카드 그리드를 담습니다(화면설계서 v1.6 §4.4).
//
// 2026-09-28부터 독립 화면(INC-001 `/incidents` · INC-004 `/asset-incidents`)이 아니라 **자산 관제(AST-001)·
// 보안 관제(SEC-001)의 `인시던트` 탭**이다. 두 탭이 이 컴포넌트를 공유한다 — 다른 건 담긴 `category`와
// 선제차단 프리셋뿐이다. 옛 경로는 그 탭으로 redirect한다(app/incidents/page.tsx).
//
// **프리셋은 클라이언트 상태다.** 종전에는 `승인 대기`·`히스토리`가 서버 필터(`?status=`)를 부르는
// 링크였는데, 이제 이 목록이 사는 화면이 인시던트 전량을 이미 들고 있다(상태 패널·토폴로지 공격 경로·
// 상세 Drawer가 같은 응답을 쓴다) — 프리셋마다 다시 부르면 같은 화면이 같은 목록을 두 번 받는다.
// `byPreset`이 네 프리셋을 전부 클라이언트에서 거르므로 결과는 같고, 대기·선제차단 건수 배지도 항상
// 셀 수 있다. 첫 프리셋만 URL(`?preset=`)에서 받는다 — `승인 대기`로 바로 들어오는 딥링크다.
//
// **화면 위 지표 띠·필터가 이 탭에도 걸린다**(`subjectArns`). 인시던트는 `subject_arn`으로 자산에
// 걸리므로, 자산 화면에서 `EC2` 타일을 누른 채 이 탭을 열면 EC2에 걸린 건만 남는다 — 자원과 그 자원의
// 진단을 한 화면에 둔 이유가 그것이다.

import { useRouter } from 'next/navigation';
import { useMemo, useRef, useState } from 'react';

import { ActionExecuteDialog } from '@/components/incidents/action-execute-dialog';
import { EmptyState } from '@/components/empty-state';
import { ErrorState } from '@/components/error-state';
import { FilterSelect } from '@/components/filter-select';
import { IncidentCard } from '@/components/incidents/incident-card';
import { Badge } from '@/components/ui/badge';
import { proposalRequest, type ActionRequest } from '@/lib/action-request';
import { getIncident, newIdempotencyKey } from '@/lib/api/client';
import { INCIDENT_STATUS_LABELS, RISK_LEVEL_LABELS } from '@/lib/enum-labels';
import {
  ALL,
  byPreset,
  clampOption,
  riskOptionsOf,
  statusOptionsOf,
  visibleIncidents,
  type IncidentPreset,
} from '@/lib/incident-filter';
import { cn } from '@/lib/utils';
import type { AssetItem, IncidentListItem } from '@/types/api';

/** 프리셋 토글 — **필터만 다르고 정렬은 같다**(§4.4). 켠 것을 다시 누르면 기본(`ACTIVE`)으로 돌아온다. */
function Preset({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        'flex cursor-pointer items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors',
        active ? 'bg-muted text-foreground font-medium' : 'text-muted-foreground hover:text-foreground',
      )}
    >
      {children}
    </button>
  );
}

export function IncidentsView({
  items,
  assets,
  initialPreset = 'ACTIVE',
  showPreemptive,
  subjectArns = null,
}: {
  /** 이 탭이 담는 카테고리의 인시던트 전량 — 프리셋은 여기서 클라이언트가 거른다. */
  items: IncidentListItem[];
  /** 화면이 이미 받은 자산 전량 — 승인 모달의 `조치 대상` 블록 조인에 쓴다(#183). 다시 부르지 않는다. */
  assets: readonly AssetItem[];
  /** URL `?preset=`에서 온 첫 프리셋. 이후 전환은 이 컴포넌트의 상태다. `전체` 칸은 없다(§4.4). */
  initialPreset?: IncidentPreset;
  /** FINOPS에는 `response_mode`가 없어 선제차단 프리셋을 두지 않는다. */
  showPreemptive: boolean;
  /** 화면 위 띠·필터가 고른 자산 집합. `null`이면 거르지 않는다(파일 머리말). */
  subjectArns?: ReadonlySet<string> | null;
}) {
  const router = useRouter();
  const [preset, setPreset] = useState<IncidentPreset>(initialPreset);
  const [status, setStatus] = useState<string>(ALL);
  const [risk, setRisk] = useState<string>(ALL);
  /** 켠 프리셋을 다시 누르면 끈다 — `전체` 칸이 없으므로 기본(진행 중 전량)이 곧 끈 상태다. */
  const toggle = (p: IncidentPreset) => setPreset((prev) => (prev === p ? 'ACTIVE' : p));

  // 화면 위 필터가 먼저 건다 — 프리셋·셀렉트·건수 배지 전부 이 집합 안에서 센다.
  const scoped = useMemo(
    () => (subjectArns === null ? items : items.filter((i) => subjectArns.has(i.subject_arn))),
    [items, subjectArns],
  );

  /**
   * ACT-001 모달은 **목록 전체에 하나**다. 카드마다 두면 인스턴스가 목록 수만큼 생기고
   * 멱등 키도 그만큼 만들어진다 — §4.6은 모달 인스턴스당 키 1개를 전제한다.
   */
  const [request, setRequest] = useState<ActionRequest | null>(null);
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
      // 자산은 승인 모달의 `조치 대상` 블록 조인에만 쓴다(#183 A안) — 이 탭이 사는 화면이 이미
      // 받아 둔 목록(`assets`)을 그대로 쓰므로 여기서는 상세 하나만 부른다.
      const incident = await getIncident(incidentId);
      if (latestOpen.current !== token) return; // 이전 선택의 응답 — 버린다
      // 조회 사이에 상태가 바뀌었으면 모달을 열지 않는다 — 실행 잠금은 §4.5가 정한 규칙이고,
      // 후보가 비어 있으면 고를 것이 없는 모달이 뜬다.
      if (incident.status === 'ACTION_IN_PROGRESS' || incident.recommendations.length === 0) {
        router.push(`/incidents/${encodeURIComponent(incidentId)}`);
        return;
      }
      // 멱등 키는 모달을 여는 이 시점에 1회 생성해 인스턴스 수명 동안 고정한다(§4.6).
      setRequest(proposalRequest(incident, assets, newIdempotencyKey()));
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
  const inPreset = useMemo(() => byPreset(scoped, preset), [scoped, preset]);
  const statusOptions = useMemo(() => statusOptionsOf(inPreset), [inPreset]);
  // 위험도 셀렉트는 값이 있을 때만 그린다 — FINOPS는 계약이 두 위험도를 null로 강제해 늘 빈다.
  const riskOptions = useMemo(() => riskOptionsOf(inPreset), [inPreset]);
  // 프리셋 전환에서 살아남은 필터가 지금 목록에 없는 값이면 `전체`로 접는다(§lib/incident-filter).
  // 셀렉트 표시값도 이 값을 써야 화면과 실제 필터가 어긋나지 않는다.
  const effectiveStatus = clampOption(status, statusOptions);
  const effectiveRisk = clampOption(risk, riskOptions);
  const visible = useMemo(
    () => visibleIncidents(scoped, preset, status, risk),
    [scoped, preset, status, risk],
  );
  const pendingOnly = preset === 'PENDING';
  // 건수 배지 — 전량을 들고 있으므로 늘 셀 수 있다(종전 서버 필터 응답의 "셀 수 없음"이 사라졌다).
  const pendingCount = useMemo(() => byPreset(scoped, 'PENDING').length, [scoped]);
  const preemptiveCount = useMemo(() => byPreset(scoped, 'PREEMPTIVE').length, [scoped]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        {/* 왼쪽 = 무엇을 볼지(프리셋), 오른쪽 = 어떻게 거를지(상태). 두 축이 한 줄에 섞이면
            어느 것이 목록을 갈아끼우고 어느 것이 그 안을 좁히는지 구분되지 않는다(§4.4).
            `전체` 칸은 없다 — 켠 프리셋을 다시 눌러 끄면 기본(진행 중 전량)으로 돌아온다. */}
        <nav className="flex items-center gap-1" aria-label="프리셋">
          <Preset active={pendingOnly} onClick={() => toggle('PENDING')}>
            승인 대기
            {/* 대기 건수 배지 — 이 프리셋에 뜨는 건 전부 지금 누를 수 있는 건이다.
                계약이 AWAITING_APPROVAL을 "실행 가능한 제안 ≥ 1 · 진행 중 실행 없음"으로 강제한다. */}
            <Badge variant="secondary">{pendingCount}</Badge>
          </Preset>
          {/* 선제차단 = 승인 없이 이미 격리된 건. `승인 대기`와 성격이 반대라(누를 일이 아니라
              정당성을 판단할 일) 상태 필터에 묻지 않고 앞에 세운다(§4.4). FINOPS에는 없다. */}
          {showPreemptive ? (
            <Preset active={preset === 'PREEMPTIVE'} onClick={() => toggle('PREEMPTIVE')}>
              선제차단
              <Badge variant="secondary">{preemptiveCount}</Badge>
            </Preset>
          ) : null}
          <Preset active={preset === 'HISTORY'} onClick={() => toggle('HISTORY')}>
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
        {/* 위 띠·필터가 걸려 있으면 그 사실을 말한다 — 숨긴 줄 모르면 "인시던트가 없다"로 읽힌다. */}
        {subjectArns !== null ? (
          <span>위 자산 필터에 걸린 자산의 건만 — 전체 {items.length}건</span>
        ) : null}
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
      {request !== null ? (
        <ActionExecuteDialog
          request={request}
          onClose={() => setRequest(null)}
          onExecuted={(outcome) => {
            const id = encodeURIComponent(request.incidentId);
            router.push(`/incidents/${id}?execution=${encodeURIComponent(outcome.execution.execution_id)}`);
          }}
          // 409 PROPOSAL_NOT_EXECUTABLE — 제안이 이미 실행됐거나 무효해졌다. 목록을 다시 읽는다.
          onProposalStale={() => {
            setRequest(null);
            router.refresh();
          }}
        />
      ) : null}
    </div>
  );
}
