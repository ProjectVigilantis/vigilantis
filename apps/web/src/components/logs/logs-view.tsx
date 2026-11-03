'use client';

// 로그 화면 본문(app/logs). 기간(하루 · 일주일 · 한 달)마다 감사 기록과 위협 이벤트를 읽어 인시던트 발생과 합친다
// (lib/activity-log.ts). 원천 하나가 실패해도 나머지는 보이고, 빠진 원천은 목록 위에 한 줄로 적는다.

import { useEffect, useState } from 'react';
import Link from 'next/link';

import { RangeToggle } from '@/components/range-toggle';
import { buildLog, LOG_KIND_LABELS, logKindCounts, type LogEntry, type LogKind } from '@/lib/activity-log';
import { ApiError, getAuditEvents, getThreatEvents } from '@/lib/api/client';
import { notYetServed } from '@/lib/support';
import { THREAT_RANGES, type ThreatEventItem, type ThreatRangeDays } from '@/lib/threat-events';
import { cn } from '@/lib/utils';
import { AUDIT_EVENTS_MAX_LIMIT, type AuditEventItem, type IncidentListItem } from '@/types/api';

/** 한 기간에 감사 기록을 최대 몇 쪽까지 읽나(쪽당 100건). 넘치면 그렇다고 적는다. */
const AUDIT_MAX_PAGES = 5;
const PAGE_ROWS = 200;

type Source<T> = { kind: 'ready'; items: T[]; truncated: boolean } | { kind: 'missing' } | { kind: 'failed' };
type Load = { audit: Source<AuditEventItem>; threats: Source<ThreatEventItem> };

const KIND_DOT: Record<LogKind, string> = {
  ACTION: 'bg-primary',
  THREAT: 'bg-status-critical',
  INCIDENT: 'bg-status-serious',
};

const stamp = (iso: string) =>
  new Date(iso).toLocaleString('ko-KR', {
    timeZone: 'Asia/Seoul',
    hourCycle: 'h23',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });

function failure(cause: unknown): { kind: 'missing' } | { kind: 'failed' } {
  return cause instanceof ApiError && notYetServed(cause.httpStatus) ? { kind: 'missing' } : { kind: 'failed' };
}

async function loadAudit(since: string): Promise<Source<AuditEventItem>> {
  try {
    const items: AuditEventItem[] = [];
    let cursor: string | undefined;
    for (let page = 0; page < AUDIT_MAX_PAGES; page += 1) {
      const res = await getAuditEvents({ since, cursor, limit: AUDIT_EVENTS_MAX_LIMIT });
      items.push(...res.items);
      if (res.next_cursor === null) return { kind: 'ready', items, truncated: false };
      cursor = res.next_cursor;
    }
    return { kind: 'ready', items, truncated: true };
  } catch (cause) {
    return failure(cause);
  }
}

async function loadThreats(days: ThreatRangeDays): Promise<Source<ThreatEventItem>> {
  try {
    const res = await getThreatEvents(days);
    return { kind: 'ready', items: res.items, truncated: res.total > res.items.length };
  } catch (cause) {
    return failure(cause);
  }
}

export function LogsView({
  assetNames,
  incidents,
  staffRoles,
}: {
  assetNames: Record<string, string>;
  /** 고객사 직원 이메일 → 권한 이름. */
  staffRoles: Record<string, string>;
  /** 인시던트 목록. 조회 실패면 null(발생 줄이 빠진다). */
  incidents: IncidentListItem[] | null;
}) {
  const [days, setDays] = useState<ThreatRangeDays>(7);
  const [filter, setFilter] = useState<LogKind | 'ALL'>('ALL');
  // 직원별로 거른다. '' = 전체.
  const [person, setPerson] = useState('');
  const [shown, setShown] = useState(PAGE_ROWS);
  // 기간마다 읽은 것을 기억한다. 돌아갈 때 다시 부르지 않는다. 시작 시각도 함께 둬 인시던트를 같은 창으로 자른다.
  const [loads, setLoads] = useState<Partial<Record<ThreatRangeDays, Load & { sinceMs: number }>>>({});
  const load = loads[days];

  useEffect(() => {
    if (loads[days] !== undefined) return;
    let alive = true;
    const sinceMs = Date.now() - days * 24 * 3600_000;
    Promise.all([loadAudit(new Date(sinceMs).toISOString()), loadThreats(days)]).then(([audit, threats]) => {
      if (alive) setLoads((m) => ({ ...m, [days]: { audit, threats, sinceMs } }));
    });
    return () => {
      alive = false;
    };
  }, [days, loads]);

  const entries =
    load === undefined
      ? null
      : buildLog({
          audit: load.audit.kind === 'ready' ? load.audit.items : [],
          threats: load.threats.kind === 'ready' ? load.threats.items : [],
          incidents: incidents ?? [],
          assetNames,
          staffRoles,
          sinceMs: load.sinceMs,
        });
  const counts = entries === null ? null : logKindCounts(entries);
  // 이 기간에 무언가를 한 사람들. 계정 목록에 있는 직원은 기록이 없어도 고를 수 있게 함께 싣는다.
  const people = [...new Set([...(entries ?? []).flatMap((e) => (e.person === null ? [] : [e.person])), ...Object.keys(staffRoles)])].sort();
  const visible = (entries ?? []).filter((e) => (filter === 'ALL' || e.kind === filter) && (person === '' || e.person === person));

  const notes: string[] = [];
  if (load !== undefined) {
    if (load.audit.kind === 'missing') notes.push('조치 기록 API가 서버에 아직 없습니다.');
    if (load.audit.kind === 'failed') notes.push('조치 기록을 불러오지 못했습니다.');
    if (load.audit.kind === 'ready' && load.audit.truncated) notes.push(`조치 기록은 최근 ${load.audit.items.length}건만 실었습니다.`);
    if (load.threats.kind === 'missing') notes.push('위협 이벤트 API가 서버에 아직 없습니다.');
    if (load.threats.kind === 'failed') notes.push('위협 이벤트를 불러오지 못했습니다.');
    if (load.threats.kind === 'ready' && load.threats.truncated) notes.push(`위협 이벤트는 최근 ${load.threats.items.length}건만 실었습니다.`);
  }
  if (incidents === null) notes.push('인시던트 목록을 불러오지 못해 발생 기록이 빠졌습니다.');

  const filters: { value: LogKind | 'ALL'; label: string; count: number | null }[] = [
    { value: 'ALL', label: '전체', count: entries?.length ?? null },
    ...(Object.keys(LOG_KIND_LABELS) as LogKind[]).map((k) => ({ value: k, label: LOG_KIND_LABELS[k], count: counts?.[k] ?? null })),
  ];

  return (
    <section aria-label="로그" className="bg-card ring-foreground/10 flex flex-col gap-4 rounded-xl p-4 ring-1 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="group" aria-label="구분" className="flex flex-wrap gap-1.5">
          {filters.map((f) => (
            <button
              key={f.value}
              type="button"
              aria-pressed={filter === f.value}
              onClick={() => {
                setFilter(f.value);
                setShown(PAGE_ROWS);
              }}
              className={cn(
                'focus-visible:ring-ring inline-flex h-8 items-center gap-1.5 rounded-full border px-3 text-[13px] font-semibold focus-visible:ring-2 focus-visible:outline-none',
                filter === f.value ? 'bg-primary border-primary text-white' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {f.value !== 'ALL' ? <span aria-hidden className={cn('size-2 rounded-full', KIND_DOT[f.value])} /> : null}
              {f.label}
              {f.count !== null ? <span className="tabular-nums">{f.count}</span> : null}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
        <label className="text-muted-foreground inline-flex items-center gap-2 text-[13px]">
          누가
          <select
            value={person}
            onChange={(e) => {
              setPerson(e.target.value);
              setShown(PAGE_ROWS);
            }}
            className="bg-card text-foreground focus-visible:ring-ring h-8 rounded-md border px-2 text-[13px] focus-visible:ring-2 focus-visible:outline-none"
          >
            <option value="">전체</option>
            {people.map((p) => (
              <option key={p} value={p}>
                {staffRoles[p] === undefined ? p : `${p} (${staffRoles[p]})`}
              </option>
            ))}
          </select>
        </label>
        <RangeToggle
          options={THREAT_RANGES.map((r) => ({ value: r.days, label: r.label }))}
          value={days}
          onChange={(d) => {
            setDays(d);
            setShown(PAGE_ROWS);
          }}
        />
        </div>
      </div>

      {notes.length > 0 ? (
        <ul className="text-muted-foreground flex flex-col gap-0.5 text-xs">
          {notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      ) : null}

      {entries === null ? (
        <div aria-busy className="bg-muted/50 h-64 animate-pulse rounded-md" />
      ) : visible.length === 0 ? (
        <p className="text-foreground-secondary py-2 text-[15px]">이 기간에 남은 기록이 없습니다.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-[13px]">
            <caption className="sr-only">로그(최근 것부터)</caption>
            <thead className="text-muted-foreground border-b text-left text-xs">
              <tr>
                <th className="py-2 pr-4 font-semibold">시각(KST)</th>
                <th className="py-2 pr-4 font-semibold">구분</th>
                <th className="py-2 pr-4 font-semibold">내용</th>
                <th className="py-2 pr-4 font-semibold">누가</th>
                <th className="py-2 font-semibold">대상</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {visible.slice(0, shown).map((e) => (
                <LogRow key={e.id} entry={e} />
              ))}
            </tbody>
          </table>
          {visible.length > shown ? (
            <button
              type="button"
              onClick={() => setShown((n) => n + PAGE_ROWS)}
              className="text-primary mt-3 text-sm font-semibold hover:underline"
            >
              {visible.length - shown}건 더 보기
            </button>
          ) : null}
        </div>
      )}
    </section>
  );
}

function LogRow({ entry }: { entry: LogEntry }) {
  return (
    <tr className="align-top">
      <td className="py-2 pr-4 font-mono text-xs whitespace-nowrap tabular-nums">{stamp(entry.at)}</td>
      <td className="py-2 pr-4 whitespace-nowrap">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden className={cn('size-2 rounded-full', KIND_DOT[entry.kind])} />
          {LOG_KIND_LABELS[entry.kind]}
        </span>
      </td>
      <td className="py-2 pr-4">
        <span className="font-semibold">{entry.title}</span>
        {entry.detail ? <span className="text-muted-foreground block text-xs">{entry.detail}</span> : null}
      </td>
      <td className="py-2 pr-4 break-all">
        {entry.actor}
        {entry.actorNote ? <span className="text-muted-foreground block text-xs">{entry.actorNote}</span> : null}
      </td>
      <td className="py-2 break-all">
        {entry.href === null ? (
          entry.target
        ) : (
          <Link href={entry.href} className="hover:underline">
            {entry.target}
          </Link>
        )}
      </td>
    </tr>
  );
}
