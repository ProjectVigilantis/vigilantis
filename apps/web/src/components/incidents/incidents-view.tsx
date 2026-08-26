'use client';

// INC-001 인시던트 목록 본체 — 프리셋·필터·카드 그리드를 담습니다(화면설계서 v1.5 §4.4).

import Link from 'next/link';
import { useMemo, useState } from 'react';

import { EmptyState } from '@/components/empty-state';
import { FilterSelect } from '@/components/filter-select';
import { IncidentCard } from '@/components/incidents/incident-card';
import { Badge } from '@/components/ui/badge';
import { CATEGORY_LABELS, INCIDENT_STATUS_LABELS } from '@/lib/enum-labels';
import { sortByRisk } from '@/lib/incident-sort';
import { cn } from '@/lib/utils';
import type { IncidentCategory, IncidentListItem, IncidentStatus } from '@/types/api';

const ALL = '전체';

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
  pendingCount,
}: {
  items: IncidentListItem[];
  /** `승인 대기` 프리셋 여부. 서버가 `?status=AWAITING_APPROVAL`로 걸러 준 결과다. */
  pendingOnly: boolean;
  pendingCount: number;
}) {
  const [category, setCategory] = useState<string>(ALL);
  const [status, setStatus] = useState<string>(ALL);

  const visible = useMemo(() => {
    const filtered = items.filter(
      (i) =>
        (category === ALL || i.category === (category as IncidentCategory)) &&
        (status === ALL || i.status === (status as IncidentStatus)),
    );
    // 정렬은 전역 셀렉터 한 곳이다 — DSH-001 AI 조치 카드가 같은 함수를 쓴다(§4.4).
    return sortByRisk(filtered);
  }, [items, category, status]);

  // 셀렉트 옵션은 응답에 실제로 있는 값만 — 계약 전체 enum을 늘어놓으면 0건 옵션이 섞인다.
  const statusOptions = useMemo(
    () => [...new Set(items.map((i) => i.status))],
    [items],
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <nav className="flex items-center gap-1">
          <Preset href="/incidents" active={!pendingOnly}>
            {ALL}
          </Preset>
          <Preset href="/incidents?status=AWAITING_APPROVAL" active={pendingOnly}>
            승인 대기
            {/* 대기 건수 배지 — 이 프리셋에 뜨는 건 전부 지금 누를 수 있는 건이다.
                계약이 AWAITING_APPROVAL을 "실행 가능한 제안 ≥ 1 · 진행 중 실행 없음"으로 강제한다. */}
            <Badge variant="secondary">{pendingCount}</Badge>
          </Preset>
        </nav>

        <div className="flex flex-wrap items-center gap-3">
          <FilterSelect
            label="유형"
            value={category}
            options={[
              { value: ALL, label: ALL },
              ...(Object.keys(CATEGORY_LABELS) as IncidentCategory[]).map((c) => ({
                value: c,
                label: CATEGORY_LABELS[c]?.label ?? c,
              })),
            ]}
            onChange={setCategory}
          />
          <FilterSelect
            label="상태"
            value={status}
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

      <div className="text-muted-foreground text-xs" aria-live="polite">
        {visible.length === items.length ? `${items.length}건` : `${visible.length} / ${items.length}건`}
      </div>

      {visible.length === 0 ? (
        <EmptyState
          message={pendingOnly ? '승인을 기다리는 인시던트가 없습니다.' : '조건에 맞는 인시던트가 없습니다.'}
          description={pendingOnly ? undefined : '필터를 바꾸면 다른 인시던트를 볼 수 있습니다.'}
        />
      ) : (
        // 페이지네이션은 MVP 계약에 없다 — 응답 전량을 렌더한다(§3.1.1·§4.4 예외).
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {visible.map((incident) => (
            <IncidentCard key={incident.incident_id} incident={incident} showExecute={pendingOnly} />
          ))}
        </div>
      )}
    </div>
  );
}
