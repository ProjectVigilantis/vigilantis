// 구독 현황(T-16 · #528)의 표시 사전 · 서버 반영 대기 판정 · 결과 문구. 원천은 /me/subscription 응답과
// 고객사 관리 응답의 `subscription` · `subscription_source` · `entitlement_ends_at` 칸이다.
//
// 결제 · 청구는 없다 — 유료 권리의 원천은 서버 안의 모의 Marketplace 계약(`agreement`)이고 `subscription`은 그
// 투영이다. 날짜 · 시각은 서버 값만 보이고 브라우저 시각으로 예정일을 만들지 않는다. 열 수 있는 쓰기는 서버의
// `available_actions`이고, 버튼을 숨기는 것은 화면 편의다 — 최종 판정은 쓰기 경로가 한다(403 · 409).

import type { EnumLabel } from './enum-labels.ts';
import { NO_VALUE } from './enum-labels.ts';
import { isStaleTenantRequest } from './tenants.ts';
import {
  SUBSCRIPTION_PURCHASE_PLANS,
  type CustomerSubscriptionResponse,
  type ErrorCode,
  type MockAgreementStatus,
  type SubscriptionAction,
  type SubscriptionPurchasePlan,
  type SubscriptionSource,
  type TenantListItem,
  type TenantSubscription,
} from '../types/api.ts';

export const FREE_PLAN = 'Free';
/** 요금이 등록되지 않았을 때. 0원으로 그리지 않는다(#528). */
export const AMOUNT_UNSET = '미정';

/** 요금제 정적 안내. 요금 · 혜택 · SLA는 확정 전이라 확정된 상품 조건처럼 적지 않는다(#528). */
export const PLAN_GUIDE: { plan: typeof FREE_PLAN | SubscriptionPurchasePlan; description: string }[] = [
  { plan: FREE_PLAN, description: '앱 기본 요금제 · 요금 미정' },
  { plan: 'Business', description: '월 단위 모의 계약 · 요금 미정' },
  { plan: 'Enterprise', description: '월 단위 모의 계약 · 요금 미정' },
];

/** 화면 · 확인창이 함께 쓰는 모의 표시. 실제 AWS 계약 · 링크 · ARN을 흉내 내지 않는다. */
export const MOCK_MARKETPLACE_NOTICE = '모의 Marketplace 계약 · 실제 AWS 구매/청구 없음';

/**
 * 서버 계약 처리의 한계 — 처리 잡이 꺼져 있거나 모의 공급자가 대기를 유지하도록 설정될 수 있다(config.py
 * SUBSCRIPTION_SCHEDULER_ENABLED · SUBSCRIPTION_MOCK_ACTIVATION_OUTCOME). 언제 처리될지 약속하지 않는다.
 */
export const PROCESSING_NOT_GUARANTEED = '서버 처리는 설정에 따라 보류되거나 늦어질 수 있어 반영 시점이 정해져 있지 않습니다.';

/** 확인창에 싣는 계약 조건 — 활성화 전이라 날짜 없이 규칙만 말한다(schemas/subscriptions.py 계약 기간 달력). */
export const RENEWAL_RULE =
  '월 단위 계약입니다. 활성화된 날(KST)의 날짜를 기념일로 매월 그날 00시(KST)가 기간 경계이고, 자동 갱신을 끄지 않으면 같은 요금제로 이어집니다. 그 날짜가 없는 달은 말일이 경계이고, 다음 달에는 원래 날짜로 돌아갑니다. 갱신은 서버가 처리한 뒤에 반영됩니다.';
export const PURCHASE_PENDING_RULE = `구매 요청은 활성화 대기로 접수될 뿐입니다. 서버의 모의 공급자가 처리해야 활성화되거나 실패로 바뀌고, 그 전에는 유료 권리가 없으며 지금 요금제가 그대로입니다. ${PROCESSING_NOT_GUARANTEED}`;

export const SOURCE_LABELS: Record<SubscriptionSource, EnumLabel> = {
  MANUAL: { label: '수동 등록', tone: 'gray' },
  MOCK_MARKETPLACE: { label: '모의 Marketplace 계약', tone: 'blue' },
  APP_FREE: { label: '앱 기본 Free', tone: 'neutral' },
};
const SOURCE_UNREGISTERED: EnumLabel = { label: '미등록', tone: 'gray' };

export function sourceLabel(source: SubscriptionSource | null): EnumLabel {
  return source === null ? SOURCE_UNREGISTERED : SOURCE_LABELS[source];
}

export const AGREEMENT_STATUS_LABELS: Record<MockAgreementStatus, EnumLabel> = {
  PENDING: { label: '활성화 대기', tone: 'yellow', spinner: true },
  ACTIVE: { label: '활성', tone: 'green' },
  FAILED: { label: '활성화 실패', tone: 'red' },
  EXPIRED: { label: '만료', tone: 'gray' },
};

export const RENEWAL_OFF: EnumLabel = { label: '자동 갱신 꺼짐', tone: 'yellow' };

/** 서비스 상태와 계약은 따로다 — 고객사 계정의 구독 화면 · 고객사 관리 화면이 같은 말을 쓴다(subscription_workflow.py 머리말). */
export const STOP_KEEPS_CONTRACT =
  '이용 중단은 구독 계약을 해지하지 않습니다. 계약의 갱신 · 만료는 중단 중에도 그대로 진행됩니다.';
export const RESUME_KEEPS_SUBSCRIPTION = '재개는 구독을 바꾸지 않고, 그때의 구독 권리를 그대로 따릅니다.';

/** 현재 요금제의 종류. NONE = 구독 또는 요금제가 등록되지 않음, OTHER = 수동 등록된 그 밖의 이름. */
export type PlanKind = 'NONE' | 'FREE' | 'PAID' | 'OTHER';

export function planKind(subscription: TenantSubscription | null): PlanKind {
  const plan = subscription?.plan ?? null;
  if (plan === null) return 'NONE';
  if (plan === FREE_PLAN) return 'FREE';
  return (SUBSCRIPTION_PURCHASE_PLANS as readonly string[]).includes(plan) ? 'PAID' : 'OTHER';
}

export function planLabel(subscription: TenantSubscription | null): string {
  if (subscription === null) return '미등록';
  return subscription.plan ?? '요금제 미등록';
}

/** 요금. 금액 · 통화 어느 하나라도 없으면 미정이다. 금액은 통화의 최소 단위라 소수 자릿수로 나눈다. */
export function formatAmount(amountMinor: number | null, currency: string | null): string {
  if (amountMinor === null || currency === null) return AMOUNT_UNSET;
  const format = new Intl.NumberFormat('ko-KR', { style: 'currency', currency });
  const digits = format.resolvedOptions().maximumFractionDigits ?? 0;
  return format.format(amountMinor / 10 ** digits);
}

export function periodText(subscription: TenantSubscription | null): string {
  const start = subscription?.period_start ?? null;
  const end = subscription?.period_end ?? null;
  if (start === null && end === null) return NO_VALUE;
  return `${start ?? NO_VALUE} ~ ${end ?? NO_VALUE}`;
}

export function anchorDayText(day: number | null): string | null {
  return day === null ? null : `매월 ${day}일(그 날짜가 없는 달은 말일)`;
}

/** 원천별 설명 한 줄(고객사 관리 · 고객사 계정의 구독 화면 공용). 등록 안 됨은 구독 칸이 이미 말한다. */
export function sourceNote(source: SubscriptionSource | null, subscription: TenantSubscription | null): string | null {
  if (source === 'MANUAL') return '수동 등록된 기록입니다. 자동 갱신 · Free 전환을 하지 않습니다.';
  if (source === 'APP_FREE') return '앱 정책의 기본 Free입니다. AWS에서 구매한 요금제가 아닙니다.';
  if (source === 'MOCK_MARKETPLACE') {
    return subscription?.cancel_at_period_end
      ? '자동 갱신이 꺼져 있습니다. 권리 끝 시각까지 이용하고, 그 뒤 서버가 만료를 처리하면 앱 기본 Free로 돌아갑니다.'
      : '모의 계약이 자동 갱신 중입니다. 권리 끝 시각 뒤 서버가 갱신을 처리하면 같은 요금제의 다음 기간이 반영됩니다.';
  }
  return null;
}

/** 저장된 투영이 서버 처리를 아직 따라잡지 못했을 때의 안내 — 지난 권리를 지금 권리처럼 말하지 않는다. */
export const LAPSED_NOTE = `권리 끝 시각이 지났습니다. 서버가 갱신 또는 만료를 처리해 반영할 때까지 위 값은 직전 기간의 기록입니다. ${PROCESSING_NOT_GUARANTEED}`;

/** 모의 계약 투영의 권리 끝 시각이 기준 시각(서버 응답을 받은 시각)에 이미 지났는가. */
export function entitlementLapsed(endsAt: string | null, nowMs: number): boolean {
  return endsAt !== null && Date.parse(endsAt) <= nowMs;
}

/** 고객사 관리 목록의 구독 칸 아랫줄 — 원천과, 있으면 자동 갱신 여부 · 마지막 이용일(KST). */
export function subscriptionListNote(
  item: Pick<TenantListItem, 'subscription' | 'subscription_source'>,
): string | null {
  const { subscription, subscription_source: source } = item;
  if (subscription === null || source === null) return null;
  const parts = [SOURCE_LABELS[source].label];
  if (source === 'MOCK_MARKETPLACE') parts.push(subscription.cancel_at_period_end ? RENEWAL_OFF.label : '자동 갱신');
  if (subscription.period_end !== null) parts.push(`${subscription.period_end}까지`);
  return parts.join(' · ');
}

/* ─────────────────────────── 고객사 계정의 구독 화면(/me/subscription) ─────────────────────────── */

/** 서버가 이 사용자에게 지금 연 쓰기인가(`available_actions`). */
export function can(data: CustomerSubscriptionResponse, action: SubscriptionAction): boolean {
  return data.available_actions.includes(action);
}

/**
 * 계약의 현재 상태가 투영에 아직 반영되지 않았는가 — 모의 공급자의 활성화 · 갱신 · 만료와 투영은 같은 처리 주기
 * 안에서도 따로 저장된다. 활성 계약인데 투영이 그 계약 · 기간 · 갱신 여부와 다르거나, 만료됐는데 투영이 아직 계약이다.
 */
export function projectionLag(data: CustomerSubscriptionResponse): boolean {
  const agreement = data.agreement;
  if (agreement === null) return false;
  if (agreement.status === 'ACTIVE') {
    return (
      data.source !== 'MOCK_MARKETPLACE' ||
      data.entitlement_ends_at === null ||
      agreement.term_end === null ||
      Date.parse(data.entitlement_ends_at) !== Date.parse(agreement.term_end) ||
      data.subscription?.cancel_at_period_end !== !agreement.auto_renew
    );
  }
  if (agreement.status === 'EXPIRED') return data.source === 'MOCK_MARKETPLACE';
  return false;
}

/** 서버 처리를 기다리는 중인가 — 활성화 대기 · 투영 지연 · 지난 권리 끝 시각. 이때 화면이 다시 읽는다. */
export function awaitingServer(data: CustomerSubscriptionResponse, nowMs: number): boolean {
  if (data.agreement?.status === 'PENDING') return true;
  if (data.source === 'MOCK_MARKETPLACE' && entitlementLapsed(data.entitlement_ends_at, nowMs)) return true;
  return projectionLag(data);
}

/**
 * 지금 뒤에 오는 가장 이른 서버 경계(ms) — 모의 계약 투영의 권리 끝 시각과 활성 계약의 기간 끝 시각. 그 시각이
 * 지나면 `awaitingServer`의 판정이 바뀌므로 열어 둔 화면이 그때 다시 판정한다. 없으면 null. 날짜를 만들지 않는다.
 */
export function nextEntitlementBoundary(data: CustomerSubscriptionResponse, nowMs: number): number | null {
  const candidates = [
    data.source === 'MOCK_MARKETPLACE' ? data.entitlement_ends_at : null,
    data.agreement?.status === 'ACTIVE' ? data.agreement.term_end : null,
  ]
    .filter((at): at is string => at !== null)
    .map((at) => Date.parse(at))
    .filter((at) => at > nowMs);
  return candidates.length === 0 ? null : Math.min(...candidates);
}

/** 모의 계약 상태의 설명. 시각은 화면이 계약 행에서 KST로 보인다. */
export function agreementNote(data: CustomerSubscriptionResponse, nowMs: number): string {
  const agreement = data.agreement;
  if (agreement === null) return '모의 Marketplace 계약이 없습니다.';
  switch (agreement.status) {
    case 'PENDING':
      return `활성화 대기 중입니다. 서버의 모의 공급자가 처리해야 활성화되거나 실패로 바뀌며, 그 전에는 유료 권리가 없습니다. ${PROCESSING_NOT_GUARANTEED}`;
    case 'ACTIVE':
      if (entitlementLapsed(agreement.term_end, nowMs)) {
        return `기간 끝 시각이 지났습니다. 갱신 또는 만료는 서버가 처리한 뒤에 반영됩니다. ${PROCESSING_NOT_GUARANTEED}`;
      }
      if (projectionLag(data)) {
        return `계약의 현재 상태가 아직 현재 구독에 반영되지 않았습니다. 서버가 처리하면 반영됩니다. ${PROCESSING_NOT_GUARANTEED}`;
      }
      return agreement.auto_renew
        ? '기간 끝 시각 뒤 서버가 처리하면 같은 요금제로 한 달 갱신됩니다.'
        : '자동 갱신이 꺼져 있습니다. 기간 끝 시각까지 권리가 유지되고, 그 뒤 서버가 만료를 처리하면 앱 기본 Free로 돌아갑니다.';
    case 'FAILED':
      return `모의 공급자가 활성화하지 못했습니다. 유료 권리는 생기지 않았고 청구도 없습니다.${
        can(data, 'PURCHASE') ? ' 다시 구매를 요청할 수 있습니다.' : ''
      }`;
    case 'EXPIRED':
      return projectionLag(data)
        ? `계약이 만료됐지만 현재 구독에 아직 반영되지 않았습니다. 서버가 처리하면 앱 기본 Free로 바뀝니다. ${PROCESSING_NOT_GUARANTEED}`
        : '자동 갱신을 끈 계약이 기간 끝 시각에 만료됐습니다.';
  }
}

/** 버튼이 없는 이유. 판정은 `available_actions`이고 여기서는 다음 할 일만 말한다. 보일 것이 없으면 null. */
export function manageNote(data: CustomerSubscriptionResponse): string | null {
  if (!data.can_manage) return '구독 변경은 고객사 중앙계정만 요청할 수 있습니다.';
  if (can(data, 'PURCHASE') || data.agreement?.status === 'PENDING') return null;
  if (data.service_status === 'SUSPENDED') {
    return '이용 중단 중에는 구매를 요청할 수 없습니다. 재개는 중앙 관리계정 또는 해당 고객사가 배정된 관리자 직원계정에 요청하세요.';
  }
  if (data.service_status === 'PENDING_CONNECTION') return '고객 계정 연결이 확인되어 관제가 시작되면 구매를 요청할 수 있습니다.';
  if (data.source === null) return '구독 정보가 등록되지 않았습니다. 등록은 중앙 관리계정 또는 해당 고객사가 배정된 관리자 직원계정에 요청하세요.';
  if (data.source === 'MANUAL' && planKind(data.subscription) !== 'FREE') {
    return '수동 등록된 구독은 이 화면에서 바꿀 수 없습니다. 중앙 관리계정 또는 해당 고객사가 배정된 관리자 직원계정에 문의하세요.';
  }
  if (planKind(data.subscription) === 'FREE' && !data.mock_marketplace_enabled) {
    return '지금은 모의 Marketplace 구매 요청을 받지 않습니다. 중앙 관리계정 또는 해당 고객사가 배정된 관리자 직원계정에 문의하세요.';
  }
  return null;
}

/**
 * 요청이 그 사이 무효가 됐는지 — 상태가 바뀌었거나(409 · 404) 쓰기 권한이 없어졌다(403). 이때는 확인창을 닫고
 * 다시 읽어 지금 가능한 동작을 보인다. 그 밖의 실패는 입력을 잃지 않게 확인창을 남기고 다시 시도하게 한다.
 */
export function isStaleSubscriptionRequest(code: ErrorCode | null): boolean {
  return isStaleTenantRequest(code) || code === 'AUTH_FORBIDDEN';
}

export type SubscriptionChange = 'purchase' | 'cancel' | 'stop';

/** 서버 처리를 기다리는 동안 화면이 다시 읽는 간격(앞 응답이 온 뒤부터). 서버 처리 주기(기본 60초)보다 짧게 둔다 — 처리를 앞당기지는 않는다. */
export const SUBSCRIPTION_POLL_MS = 15_000;

/**
 * 성공 알림. 서버 응답의 날짜만 쓴다 — 구매 요청은 접수일 뿐 구매 완료가 아니고, 갱신 끄기는 해지가 아니며,
 * 이용 중단은 계약 해지가 아니다.
 */
export function changeNotice(kind: SubscriptionChange, data: CustomerSubscriptionResponse): string {
  const sub = data.subscription;
  if (kind === 'purchase') {
    return `${data.agreement?.plan ?? '유료 요금제'} 구매 요청을 접수했습니다(활성화 대기). 활성화되기 전까지는 지금 요금제(${planLabel(sub)})가 그대로입니다.`;
  }
  if (kind === 'cancel') {
    const until = sub?.period_end ?? NO_VALUE;
    const expiry = data.expiry_date === null ? '' : ` ${data.expiry_date} 00시(KST) 뒤 서버가 만료를 처리하면 앱 기본 Free로 돌아갑니다.`;
    return `자동 갱신을 껐습니다. 즉시 해지가 아니며 ${until}까지 지금 권리가 유지됩니다.${expiry}`;
  }
  return '서비스 이용을 중단했습니다. 구독 계약은 해지되지 않았고 갱신 · 만료는 계속 진행됩니다.';
}

/** 알림이 가리키는 상태. 다시 읽은 응답에서 이 값이 바뀌면 지난 알림을 내린다. */
export function noticeSignature(data: CustomerSubscriptionResponse): string {
  const a = data.agreement;
  return [a?.agreement_id, a?.status, a?.auto_renew, data.service_status, data.source].join('|');
}
