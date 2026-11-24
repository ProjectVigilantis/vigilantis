// 공개 안내 문서(app/guides/*)의 도식. AWS 화면 캡처를 싣지 않는 대신, 누가 · 어느 화면에서 · 무엇을 하고 ·
// 무엇을 보면 끝났는지를 그림처럼 보이게 한다. 캡처를 흉내 내지 않도록 모든 도식에 「도식」 표시를 붙이고,
// 화면 모형(GuideSketch)은 보조 기술에 그림 하나(role="img")로 읽히며 같은 뜻을 번호 설명이 글로 다시 싣는다.
//
// 글 폭은 문서 골격(LegalDocument)의 본문 열이 정한다. 사이드바 유무와 상관없이 열 폭으로 단을 나누도록
// 화면 폭이 아니라 컨테이너 쿼리(@container)를 쓴다.

import { ArrowDown, Check, ChevronDown, CircleCheck, ExternalLink, ListChecks, RotateCcw } from 'lucide-react';

import { SERVICE_NAME } from '@/lib/legal';

/** 단계를 하는 쪽. 색만으로 가르지 않게 이름표를 늘 함께 보인다. */
export type GuideActor = 'customer' | 'operator';

const ACTOR: Record<GuideActor, { label: string; className: string }> = {
  customer: { label: '고객사 AWS 담당자', className: 'border-primary/40 bg-primary/10 text-foreground' },
  operator: { label: `${SERVICE_NAME} 운영팀`, className: 'border-status-good/50 bg-status-good/10 text-foreground' },
};

const SKETCH_NOTE = '도식 · 실제 AWS 화면이 아닙니다';

function ActorChip({ actor }: { actor: GuideActor }) {
  const a = ACTOR[actor];
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs ${a.className}`}>
      {a.label}
    </span>
  );
}

function Caption({ title, note = '도식' }: { title: React.ReactNode; note?: string }) {
  return (
    <figcaption className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <span className="text-foreground text-sm font-bold">{title}</span>
      <span className="text-muted-foreground text-xs">{note}</span>
    </figcaption>
  );
}

function Mark({ n }: { n: number }) {
  return (
    <span
      aria-hidden
      className="bg-primary text-primary-foreground inline-flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold tabular-nums"
    >
      {n}
    </span>
  );
}

export interface GuideStep {
  title: React.ReactNode;
  actor: GuideActor;
  /** 어느 화면 · 어느 자리에서 하는지. */
  where?: React.ReactNode;
  body?: React.ReactNode;
  /** 이 단계가 끝났다고 볼 수 있는 표시. */
  signal?: React.ReactNode;
}

/** 위에서 아래로 이어지는 단계 흐름. `loop`가 있으면 끝에 되돌아가는 화살표와 그 조건을 보인다. */
export function GuideFlow({ title, steps, loop }: { title: React.ReactNode; steps: GuideStep[]; loop?: React.ReactNode }) {
  return (
    <figure className="bg-card flex flex-col gap-4 rounded-lg border p-4 sm:p-5">
      <Caption title={title} />
      <ol className="flex flex-col">
        {steps.map((s, i) => (
          <li key={i} className="relative flex gap-3 pb-5 last:pb-0">
            {i < steps.length - 1 ? (
              <span aria-hidden className="bg-border absolute top-8 bottom-0 left-[15px] w-0.5" />
            ) : null}
            <span
              aria-hidden
              className="bg-accent text-accent-foreground ring-card relative flex size-8 shrink-0 items-center justify-center rounded-full text-sm font-bold tabular-nums ring-4"
            >
              {i + 1}
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-1.5 pt-0.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <ActorChip actor={s.actor} />
                {s.where ? (
                  <span className="text-muted-foreground bg-secondary rounded px-1.5 py-0.5 text-xs">{s.where}</span>
                ) : null}
              </div>
              <p className="text-foreground leading-6">{s.title}</p>
              {s.body ? <div className="text-[14px] leading-6">{s.body}</div> : null}
              {s.signal ? (
                <p className="flex gap-1.5 text-[13px] leading-5">
                  <CircleCheck aria-hidden className="text-status-good mt-0.5 size-4 shrink-0" />
                  <span>
                    완료 신호: {s.signal}
                  </span>
                </p>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
      {loop ? (
        <p className="bg-secondary flex gap-2 rounded-md p-3 text-[13px] leading-5">
          <RotateCcw aria-hidden className="text-primary mt-0.5 size-4 shrink-0" />
          <span>{loop}</span>
        </p>
      ) : null}
    </figure>
  );
}

export interface GuideBranch {
  label: React.ReactNode;
  /** 이 갈래에 해당하는 때. */
  when: React.ReactNode;
  actor: GuideActor;
  steps: React.ReactNode[];
  /** 이 갈래의 끝에서 보이는 것. */
  result: React.ReactNode;
}

/** 하나의 물음에서 갈라지는 두세 갈래. 좁은 열에서는 위아래로, 넓은 열에서는 나란히 놓인다. */
export function GuideBranches({
  title,
  question,
  branches,
  note,
}: {
  title: React.ReactNode;
  question: React.ReactNode;
  branches: GuideBranch[];
  note?: React.ReactNode;
}) {
  const cols = branches.length >= 3 ? '@2xl:grid-cols-3' : '@lg:grid-cols-2';
  return (
    <figure className="@container bg-card flex flex-col gap-4 rounded-lg border p-4 sm:p-5">
      <Caption title={title} />
      <div className="flex flex-col items-center gap-2">
        <p className="border-primary/40 bg-primary/5 text-foreground w-full max-w-xl rounded-lg border-2 border-dashed px-4 py-2.5 text-center text-sm">
          {question}
        </p>
        <ArrowDown aria-hidden className="text-muted-foreground size-5" />
      </div>
      <ul className={`grid gap-3 ${cols}`}>
        {branches.map((b, i) => (
          <li key={i} className="flex flex-col gap-2.5 rounded-lg border p-4">
            <div className="flex flex-col gap-1.5">
              <ActorChip actor={b.actor} />
              <p className="text-foreground leading-6 font-bold">{b.label}</p>
              <p className="text-muted-foreground text-[13px] leading-5">{b.when}</p>
            </div>
            <ol className="flex list-decimal flex-col gap-1 pl-5 text-[14px] leading-6">
              {b.steps.map((step, j) => (
                <li key={j}>{step}</li>
              ))}
            </ol>
            <p className="bg-secondary mt-auto flex gap-1.5 rounded-md p-2.5 text-[13px] leading-5">
              <CircleCheck aria-hidden className="text-status-good mt-0.5 size-4 shrink-0" />
              <span>
                결과: {b.result}
              </span>
            </p>
          </li>
        ))}
      </ul>
      {note ? <p className="text-muted-foreground text-[13px] leading-5">{note}</p> : null}
    </figure>
  );
}

export interface SketchItem {
  label: string;
  value?: React.ReactNode;
  /** 화면 모형의 번호. 아래 번호 설명(callouts)의 순서와 같다. */
  mark?: number;
  kind?: 'field' | 'menu' | 'check' | 'button';
}

function SketchPiece({ item }: { item: SketchItem }) {
  const kind = item.kind ?? 'field';
  const ring = item.mark ? 'ring-primary ring-2' : '';
  if (kind === 'button') {
    return (
      <span className="flex items-center gap-1.5 self-end">
        {item.mark ? <Mark n={item.mark} /> : null}
        <span className={`bg-primary text-primary-foreground rounded-md px-3 py-1.5 text-xs font-semibold ${ring} ring-offset-2`}>
          {item.label}
        </span>
      </span>
    );
  }
  if (kind === 'menu') {
    return (
      <span className="flex items-center gap-1.5">
        {item.mark ? <Mark n={item.mark} /> : null}
        <span className={`bg-background flex items-center gap-1 rounded-md border px-2 py-1 text-xs ${ring}`}>
          <span className="text-muted-foreground">{item.label}</span>
          {item.value ? <span className="text-foreground">{item.value}</span> : null}
          <ChevronDown aria-hidden className="size-3.5" />
        </span>
      </span>
    );
  }
  if (kind === 'check') {
    return (
      <span className={`bg-background flex items-start gap-2 rounded-md border p-2.5 text-xs ${ring}`}>
        {item.mark ? <Mark n={item.mark} /> : null}
        <span className="bg-primary text-primary-foreground mt-px flex size-4 shrink-0 items-center justify-center rounded-sm">
          <Check aria-hidden className="size-3" />
        </span>
        <span className="text-foreground leading-5">
          {item.label}
          {item.value ? <span className="text-muted-foreground block">{item.value}</span> : null}
        </span>
      </span>
    );
  }
  return (
    <span className={`bg-background flex items-start gap-2 rounded-md border p-2.5 text-xs ${ring}`}>
      {item.mark ? <Mark n={item.mark} /> : null}
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="text-muted-foreground">{item.label}</span>
        {item.value ? <span className="text-foreground font-mono break-all">{item.value}</span> : null}
      </span>
    </span>
  );
}

/**
 * 화면 모형. 자리의 배치만 단순하게 그리고 실제 화면처럼 꾸미지 않는다. 보조 기술에는 `summary` 한 문장으로 읽히고,
 * 번호 자리의 할 일은 아래 `callouts`가 글로 싣는다.
 */
export function GuideSketch({
  title,
  summary,
  screen,
  topbar,
  items,
  callouts,
}: {
  title: React.ReactNode;
  summary: string;
  /** 모형 안의 화면 이름. */
  screen: string;
  topbar?: SketchItem[];
  items: SketchItem[];
  callouts: React.ReactNode[];
}) {
  return (
    <figure className="bg-card flex flex-col gap-4 rounded-lg border p-4 sm:p-5">
      <Caption title={title} note={SKETCH_NOTE} />
      <div role="img" aria-label={summary} className="bg-muted/40 overflow-hidden rounded-lg border border-dashed">
        {topbar ? (
          <div className="bg-secondary flex flex-wrap items-center justify-end gap-2 border-b border-dashed px-3 py-2">
            <span className="text-muted-foreground mr-auto text-xs">상단 메뉴 막대</span>
            {topbar.map((item) => (
              <SketchPiece key={item.label} item={{ kind: 'menu', ...item }} />
            ))}
          </div>
        ) : null}
        <div className="flex flex-col gap-2.5 p-3">
          <span className="text-foreground text-sm font-bold">{screen}</span>
          {items.map((item) => (
            <SketchPiece key={item.label} item={item} />
          ))}
        </div>
      </div>
      <ol className="flex flex-col gap-2 text-[14px] leading-6">
        {callouts.map((c, i) => (
          <li key={i} className="flex gap-2">
            <span className="mt-0.5">
              <Mark n={i + 1} />
            </span>
            <span>{c}</span>
          </li>
        ))}
      </ol>
    </figure>
  );
}

/** 시작 전에 갖출 것. 완료 신호는 각 단계 · 갈래가 싣는다. */
export function GuidePreconditions({ items }: { items: React.ReactNode[] }) {
  return (
    <section aria-label="시작 전 확인" className="bg-card flex flex-col gap-2 rounded-lg border p-4">
      <p className="text-foreground flex items-center gap-1.5 text-sm font-bold">
        <ListChecks aria-hidden className="text-primary size-4" />
        시작 전 확인
      </p>
      <ul className="flex list-disc flex-col gap-1 pl-5 text-[14px] leading-6">
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

/** 이 절이 기대는 AWS 공식 문서. 새 창으로 연다. */
export function GuideSources({ links }: { links: { label: string; href: string }[] }) {
  return (
    <div className="flex flex-col gap-1.5 text-[13px] leading-5">
      <p className="text-muted-foreground font-semibold">AWS 공식 문서</p>
      <ul className="flex flex-col gap-1">
        {links.map((l) => (
          <li key={l.href}>
            <a href={l.href} target="_blank" rel="noreferrer" className="text-link inline-flex items-center gap-1 hover:underline">
              {l.label}
              <ExternalLink aria-hidden className="size-3.5" />
              <span className="sr-only">(새 창)</span>
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 절 안의 소제목. 절 제목(h2) 아래 단계 묶음을 나눈다. */
export function GuideHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-foreground mt-2 text-base font-bold">
      {children}
    </h3>
  );
}
