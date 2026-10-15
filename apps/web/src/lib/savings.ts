// 절감 예상 집계 — AST-001 「절감 예상(AI 추정)」 카드가 쓰는 파생. 렌더와 분리해 `node --test`로
// 검증한다. 같은 디렉터리 상대 경로만 쓴다 — `node --test`는 `@/` 별칭을 해석하지 못한다.
//
// **이 값은 AWS Cost Explorer가 아니다.** 원천은 인시던트 상세의 `ai_savings_estimate`이고,
// 단가는 모델 추정(`pricing_source = MODEL_KNOWLEDGE`), 금액은 서버가 (현재 단가 − 목표 단가) ×
// 730시간으로 계산한 **예상값**이다(#347). 화면은 금액 옆에 그 사실을 반드시 함께 적는다.
//
// Cost Explorer를 쓰지 않는 이유는 환경 제약이다 — LocalStack Community에 `ce`·`pricing`이
// 없어(실측: `ce` = Pro 전용, `pricing` GetProducts 미지원) 로컬·CI 어디서도 검증할 수 없고,
// 실 계정에서도 요청당 과금·24시간 지연·일 단위 입자라 시연 창에서는 빈 곡선이 된다.

import type { AiSavingsEstimate, AssetItem, IncidentResponse } from '@/types/api';

export interface SavingsRow {
  /** 조치 대상 자산의 ARN — 자산 목록과 같은 축이라 카드에서 자산으로 넘어갈 수 있다. */
  arn: string;
  /** 자산 이름(없으면 resource_id, 그것도 못 찾으면 ARN 꼬리). */
  label: string;
  incidentId: string;
  /** USD/월. 계약은 문자열로 싣고 여기서 숫자로 바꾼다 — 막대 길이를 재려면 수치가 필요하다. */
  amount: number;
  currentType: string;
  targetType: string;
  /** 서버가 쓴 근거 문장(`explanation_source = SERVER_TEMPLATE`). 툴팁에 그대로 쓴다. */
  explanation: string;
}

export interface SavingsSummary {
  /** 금액이 나온 것만, 큰 것부터. 막대 길이는 `rows[0].amount` 기준으로 잰다. */
  rows: SavingsRow[];
  /** USD/월 합계. */
  total: number;
  /**
   * 추정이 실려 왔지만 금액이 없는 건수(`UNAVAILABLE`·`INVALID`). **0으로 합치지 않는다** —
   * 합계를 "이만큼이 전부"로 읽게 두면 안 되므로 카드가 이 수를 함께 적는다.
   */
  unestimated: number;
}

/**
 * 인시던트 상세 묶음에서 절감 예상을 모은다. `details`에는 조회에 성공한 상세만 넣는다
 * (실패는 호출부가 걸러 낸다 — 여기서 실패와 0건을 섞으면 카드가 둘을 구분하지 못한다).
 *
 * 같은 자산에 추정이 둘 이상 오면 **큰 쪽만 남긴다.** 인시던트가 여러 번 열린 자산에서
 * 같은 다운사이징이 두 번 세어지면 합계가 부풀기 때문이다.
 */
export function savingsSummary(
  details: readonly IncidentResponse[],
  assets: readonly AssetItem[],
): SavingsSummary {
  const nameByArn = new Map(assets.map((a) => [a.arn, a.name ?? a.resource_id]));
  const best = new Map<string, SavingsRow>();
  let unestimated = 0;

  for (const detail of details) {
    for (const rec of detail.recommendations) {
      const estimate = rec.ai_savings_estimate;
      if (estimate === null) continue;
      if (estimate.status !== 'ESTIMATED' || estimate.amount === null || estimate.basis === null) {
        unestimated += 1;
        continue;
      }
      const amount = Number(estimate.amount);
      if (!Number.isFinite(amount)) {
        unestimated += 1;
        continue;
      }
      const row: SavingsRow = {
        arn: rec.target_arn,
        label: nameByArn.get(rec.target_arn) ?? rec.target_arn.split('/').at(-1) ?? rec.target_arn,
        incidentId: detail.incident_id,
        amount,
        currentType: estimate.basis.current_instance_type,
        targetType: estimate.basis.target_instance_type,
        explanation: estimate.basis.explanation,
      };
      const seen = best.get(row.arn);
      if (seen === undefined || seen.amount < row.amount) best.set(row.arn, row);
    }
  }

  const rows = [...best.values()].sort((a, b) => b.amount - a.amount);
  // 달러를 그대로 더하면 부동소수 오차가 남는다(실측: 75.92 + 120 = 195.92000000000002).
  // 계약이 소수 둘째 자리까지만 싣는 값이므로 **센트로 바꿔 정수로 더하고** 되돌린다.
  const cents = rows.reduce((sum, r) => sum + Math.round(r.amount * 100), 0);
  return { rows, total: cents / 100, unestimated };
}

export interface SavingsBreakdown {
  /** 현재 스펙의 월 비용(USD) — 현재 단가 × 가정 시간. 막대 전체 길이다. */
  current: number;
  /** 조정 후 남는 월 비용(USD) — `current − saving`. */
  remaining: number;
  /** 월 절감 예상(USD) — 서버가 계산한 `amount` 그대로. 화면의 다른 금액 표기와 한 값이다. */
  saving: number;
  /** `saving / current`(0–1). 현재 비용이 0이면 0. */
  ratio: number;
}

/**
 * INC-002 절감 예상 막대의 분해 — "현재 월 비용 = 조정 후 비용 + 절감"으로 쪼갠다.
 *
 * 절감은 서버 `amount`를 그대로 쓰고 조정 후 비용을 **뺄셈으로** 얻는다. 목표 단가 × 시간을
 * 따로 곱하면 반올림 경로가 서버와 달라 막대 옆 절감액이 금액 표기와 1센트씩 어긋날 수 있다.
 * 금액이 없거나(`ESTIMATED` 아님) 수치가 깨졌거나 절감이 현재 비용을 넘으면(계약 위반) null —
 * 그리지 않는다. 틀린 막대보다 막대가 없는 편이 낫다.
 */
export function savingsBreakdown(estimate: AiSavingsEstimate): SavingsBreakdown | null {
  if (estimate.status !== 'ESTIMATED' || estimate.amount === null || estimate.basis === null) {
    return null;
  }
  const rate = Number(estimate.basis.current_hourly_rate);
  const hours = estimate.basis.assumptions.hours;
  const saving = Number(estimate.amount);
  if (![rate, hours, saving].every(Number.isFinite)) return null;

  // 센트 정수로 계산한다 — savingsSummary와 같은 이유(부동소수 오차).
  const currentCents = Math.round(rate * hours * 100);
  const savingCents = Math.round(saving * 100);
  if (savingCents < 0 || savingCents > currentCents) return null;

  return {
    current: currentCents / 100,
    remaining: (currentCents - savingCents) / 100,
    saving: savingCents / 100,
    ratio: currentCents === 0 ? 0 : savingCents / currentCents,
  };
}

/** `$12.34` 꼴. 통화는 계약이 USD 고정이라 기호를 상수로 둔다. */
export function formatUsd(amount: number): string {
  return `$${amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
