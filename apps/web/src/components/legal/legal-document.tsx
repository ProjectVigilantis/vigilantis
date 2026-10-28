// 처리방침 · 약관(T-15) 공용 골격: 랜딩과 같은 남색 머리띠, 제목 · 시행일, 목차, 조항 본문, 바닥글.
// 로그인 전 화면이라 셸 · 실시간 연결 없이 그린다(lib/return-to.ts `PUBLIC_PATHS`).

import { Shield } from 'lucide-react';
import Link from 'next/link';

import { formatLegalDate, LEGAL_EFFECTIVE_DATE, OPERATOR_NAME, SERVICE_NAME } from '@/lib/legal';
import { LANDING_PATH, PRIVACY_PATH, TERMS_PATH } from '@/lib/return-to';

export type LegalSection = { id: string; title: string; body: React.ReactNode };

export function LegalDocument({
  title,
  intro,
  sections,
}: {
  title: string;
  intro: React.ReactNode;
  sections: LegalSection[];
}) {
  return (
    <div className="bg-background flex flex-1 flex-col">
      <header className="dark bg-shell text-foreground">
        <div className="mx-auto flex h-16 w-full max-w-3xl items-center justify-between px-4 sm:px-6">
          <Link href={LANDING_PATH} className="flex items-center gap-2.5">
            <span className="bg-primary flex size-8 items-center justify-center rounded-md">
              <Shield aria-hidden className="size-4.5 text-white" />
            </span>
            <span className="text-lg font-bold tracking-tight">{SERVICE_NAME}</span>
          </Link>
          <Link
            href="/login"
            className="bg-primary inline-flex h-10 items-center rounded-md px-4 text-sm font-semibold text-white hover:bg-primary/90"
          >
            로그인
          </Link>
        </div>
      </header>

      <article className="mx-auto flex w-full max-w-3xl flex-col gap-10 px-4 py-12 sm:px-6">
        <div className="flex flex-col gap-3">
          <h1 className="text-3xl font-bold tracking-tight">{title}</h1>
          <p className="text-muted-foreground text-sm">시행일 {formatLegalDate(LEGAL_EFFECTIVE_DATE)}</p>
          <div className="text-foreground-secondary text-[15px] leading-7">{intro}</div>
        </div>

        <nav aria-label="목차" className="bg-card rounded-xl border p-6">
          <h2 className="mb-3 text-sm font-semibold">목차</h2>
          {/* 2열은 위에서 아래로 채운다. 격자로 두면 1 · 2가 한 줄에 놓여 왼쪽 열만 읽을 때 번호가 건너뛴다. */}
          <ol className="gap-x-6 text-sm sm:columns-2">
            {sections.map((s) => (
              <li key={s.id} className="mb-1.5 break-inside-avoid">
                <a href={`#${s.id}`} className="text-primary hover:underline">
                  {s.title}
                </a>
              </li>
            ))}
          </ol>
        </nav>

        {sections.map((s) => (
          <section key={s.id} id={s.id} aria-labelledby={`${s.id}-title`} className="flex scroll-mt-6 flex-col gap-3">
            <h2 id={`${s.id}-title`} className="text-lg font-bold">
              {s.title}
            </h2>
            <div className="text-foreground-secondary flex flex-col gap-3 text-[15px] leading-7">{s.body}</div>
          </section>
        ))}
      </article>

      <LegalFooter />
    </div>
  );
}

/** 랜딩 · 로그인 · 문서 화면이 함께 쓰는 바닥 링크. 처리방침은 이름을 굵게 두어 약관과 구별되게 한다(관례). */
export function LegalLinks({ className }: { className?: string }) {
  return (
    <span className={className ?? 'flex gap-5'}>
      <Link href={PRIVACY_PATH} className="font-semibold hover:underline">
        개인정보처리방침
      </Link>
      <Link href={TERMS_PATH} className="hover:underline">
        이용약관
      </Link>
    </span>
  );
}

function LegalFooter() {
  return (
    <footer className="border-t">
      <div className="text-muted-foreground mx-auto flex w-full max-w-3xl flex-wrap items-center justify-between gap-4 px-4 py-8 text-sm sm:px-6">
        <span>
          © {SERVICE_NAME} · {OPERATOR_NAME}
        </span>
        <LegalLinks />
      </div>
    </footer>
  );
}

/** 조항 안 목록. `numbered`면 1. 2. 3. 순서 목록이다. */
export function LegalList({ items, numbered = false }: { items: React.ReactNode[]; numbered?: boolean }) {
  const Tag = numbered ? 'ol' : 'ul';
  return (
    <Tag className={`flex flex-col gap-1.5 pl-5 ${numbered ? 'list-decimal' : 'list-disc'}`}>
      {items.map((item, i) => (
        <li key={i}>{item}</li>
      ))}
    </Tag>
  );
}

/** 조항 안 표. 좁은 화면에서는 상자 안에서만 가로로 넘친다(DESIGN.md §레이아웃과 간격). */
export function LegalTable({ head, rows }: { head: string[]; rows: React.ReactNode[][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full min-w-[520px] text-left text-sm">
        <thead className="bg-muted text-foreground-secondary">
          <tr>
            {head.map((h) => (
              <th key={h} scope="col" className="px-4 py-2.5 font-semibold">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t align-top">
              {row.map((cell, j) => (
                <td key={j} className="px-4 py-2.5 leading-6">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
