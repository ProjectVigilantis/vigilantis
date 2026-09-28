// AST-001 「절감 예상(AI 추정)」 — 다운사이징 후보별 월 절감 예상액 막대.
//
// **실제 청구액이 아니다(#347).** 원천은 인시던트 상세의 `ai_savings_estimate`이고, 단가는
// 모델이 추정한 값(`pricing_source = MODEL_KNOWLEDGE`), 금액은 서버가 (현재 단가 − 목표 단가)
// × 730시간으로 계산한 예상값이다. AWS Cost Explorer 연동이 아니므로 카드 설명에 그 사실을
// 고정 문구로 적는다 — 금액만 큰 글자로 남으면 청구서로 읽힌다.
//
// 막대 길이는 **1위 금액 기준 비율**이다. 합계 대비로 그리면 후보가 늘수록 모든 막대가
// 짧아져 "1위가 얼마나 큰가"가 안 보인다.

import { EmptyState } from '@/components/empty-state';
import { formatUsd, type SavingsSummary } from '@/lib/savings';
import { cn } from '@/lib/utils';

export function SavingsCard({
  summary,
  /** 상세 조회에 실패한 인시던트 수. 0이면 표시하지 않는다 — 실패를 0원으로 덮지 않기 위해서다. */
  failed,
  onSelect,
}: {
  summary: SavingsSummary;
  failed: number;
  /** 막대를 누르면 그 자산 상세로. 자산 화면이 자기 Drawer를 연다. */
  onSelect: (arn: string) => void;
}) {
  const { rows, total, unestimated } = summary;
  const top = rows[0]?.amount ?? 0;

  if (rows.length === 0) {
    return (
      <EmptyState
        message="절감 예상이 아직 없습니다."
        description={
          unestimated > 0 || failed > 0
            ? `추정 실패 ${unestimated}건 · 조회 실패 ${failed}건 — 분석이 끝나면 이 자리에 금액이 생깁니다.`
            : '다운사이징 후보가 분석되면 이 자리에 월 절감 예상액이 생깁니다.'
        }
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="flex items-baseline justify-between text-sm">
        <span className="text-muted-foreground text-xs">후보 {rows.length}건 합계</span>
        <span className="font-mono text-lg font-medium tabular-nums">{formatUsd(total)}</span>
      </p>

      <ul className="flex flex-col gap-3">
        {rows.map((row) => (
          <li key={row.arn}>
            <button
              type="button"
              onClick={() => onSelect(row.arn)}
              title={row.explanation}
              className="hover:bg-accent/40 focus-visible:ring-ring/50 flex w-full cursor-pointer flex-col gap-1 rounded-md px-1 py-1 text-left focus-visible:ring-2 focus-visible:outline-none"
            >
              <span className="flex items-center justify-between gap-2 text-xs">
                <span className="truncate">{row.label}</span>
                <span className="font-mono tabular-nums">{formatUsd(row.amount)}</span>
              </span>
              <span className="bg-muted block h-1.5 overflow-hidden rounded-full">
                <span
                  className="bg-emerald-400/80 block h-full rounded-full"
                  style={{ width: `${top === 0 ? 0 : (row.amount / top) * 100}%` }}
                />
              </span>
              <span className="text-muted-foreground font-mono text-[11px]">
                {row.currentType} → {row.targetType}
              </span>
            </button>
          </li>
        ))}
      </ul>

      {/* 금액이 안 나온 건은 0원이 아니라 **모름**이다 — 합계가 "이만큼이 전부"로 읽히지 않게 적는다. */}
      {unestimated > 0 || failed > 0 ? (
        <p className={cn('text-muted-foreground border-t pt-2 text-xs')}>
          금액 미산출 {unestimated}건{failed > 0 ? ` · 상세 조회 실패 ${failed}건` : ''} — 합계에
          포함되지 않았습니다.
        </p>
      ) : null}
    </div>
  );
}
