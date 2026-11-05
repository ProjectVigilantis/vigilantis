import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { CustomerSubscriptionResponse, MockAgreement, TenantSubscription } from '../types/api.ts';
import {
  AMOUNT_UNSET,
  agreementNote,
  awaitingServer,
  changeNotice,
  formatAmount,
  isStaleSubscriptionRequest,
  manageNote,
  noticeSignature,
  periodText,
  planKind,
  planLabel,
  sourceLabel,
  sourceNote,
  subscriptionListNote,
} from './subscriptions.ts';

function sub(over: Partial<TenantSubscription> = {}): TenantSubscription {
  return {
    plan: 'Free',
    period_start: null,
    period_end: null,
    cancel_at_period_end: false,
    billing_anchor_day: null,
    amount_minor: null,
    currency: null,
    ...over,
  };
}

/** 앱 기본 Free · 계약 없음 · 고객사 중앙계정 · 구매 가능. 다른 상태는 서버 계약(schemas/api/subscriptions.py)대로 덮어쓴다. */
function res(over: Partial<CustomerSubscriptionResponse> = {}): CustomerSubscriptionResponse {
  return {
    tenant_name: '학원',
    service_status: 'ACTIVE',
    subscription: sub(),
    source: 'APP_FREE',
    entitlement_ends_at: null,
    agreement: null,
    next_renewal_date: null,
    expiry_date: null,
    mock_marketplace_enabled: true,
    can_manage: true,
    available_actions: ['PURCHASE', 'STOP'],
    ...over,
  };
}

// 2026-10-31(KST) 활성화 · 기념일 31 · 첫 기간 끝 2026-11-30 00시(KST) = 2026-11-29T15:00Z.
const TERM_END = '2026-11-29T15:00:00Z';
const BEFORE_END = Date.parse('2026-11-10T00:00:00Z');
const AFTER_END = Date.parse('2026-11-29T15:00:30Z');

function agreement(over: Partial<MockAgreement> = {}): MockAgreement {
  return {
    agreement_id: 'mock-agr-1',
    source: 'MOCK_MARKETPLACE',
    plan: 'Business',
    status: 'ACTIVE',
    auto_renew: true,
    requested_at: '2026-10-30T14:59:00Z',
    activated_at: '2026-10-30T15:00:00Z',
    term_start: '2026-10-30T15:00:00Z',
    term_end: TERM_END,
    renewal_disabled_at: null,
    ended_at: null,
    failure_code: null,
    ...over,
  };
}

const PAID = sub({ plan: 'Business', period_start: '2026-10-31', period_end: '2026-11-29', billing_anchor_day: 31 });

const PENDING = agreement({
  status: 'PENDING', activated_at: null, term_start: null, term_end: null,
});
const FAILED = agreement({
  status: 'FAILED', activated_at: null, term_start: null, term_end: null,
  ended_at: '2026-10-30T15:01:00Z', failure_code: 'MOCK_ACTIVATION_FAILED',
});
const RENEWAL_OFF_AT = '2026-11-05T01:00:00Z';

/** 활성 계약이 투영까지 반영된 응답(자동 갱신 중). */
const ACTIVE_PROJECTED = res({
  subscription: PAID,
  source: 'MOCK_MARKETPLACE',
  entitlement_ends_at: TERM_END,
  agreement: agreement(),
  next_renewal_date: '2026-11-30',
  available_actions: ['CANCEL_RENEWAL', 'STOP'],
});
/** 자동 갱신을 끈 뒤(투영 반영). 권리는 기간 끝까지다. */
const RENEWAL_DISABLED = res({
  subscription: { ...PAID, cancel_at_period_end: true },
  source: 'MOCK_MARKETPLACE',
  entitlement_ends_at: TERM_END,
  agreement: agreement({ auto_renew: false, renewal_disabled_at: RENEWAL_OFF_AT }),
  expiry_date: '2026-11-30',
  available_actions: ['STOP'],
});

test('요금제 종류: 미등록과 Free를 가르고, 수동 등록된 그 밖의 이름은 유료로 보지 않는다', () => {
  assert.equal(planKind(null), 'NONE');
  assert.equal(planKind(sub({ plan: null })), 'NONE');
  assert.equal(planKind(sub()), 'FREE');
  assert.equal(planKind(sub({ plan: 'Business' })), 'PAID');
  assert.equal(planKind(sub({ plan: 'Enterprise' })), 'PAID');
  assert.equal(planKind(sub({ plan: 'Pro' })), 'OTHER');
  assert.equal(planLabel(null), '미등록');
  assert.equal(planLabel(sub({ plan: 'Pro' })), 'Pro');
});

test('요금이 없으면 0원이 아니라 미정이다', () => {
  assert.equal(formatAmount(null, 'KRW'), AMOUNT_UNSET);
  assert.equal(formatAmount(10000, null), AMOUNT_UNSET);
  assert.notEqual(formatAmount(0, 'KRW'), AMOUNT_UNSET);
});

test('금액은 통화의 최소 단위다 — KRW는 소수 자리가 없고 USD는 센트다', () => {
  assert.match(formatAmount(10000, 'KRW'), /10,000/);
  assert.match(formatAmount(1999, 'USD'), /19\.99/);
});

test('기간은 서버 날짜만 쓰고 없는 끝은 비워 둔다', () => {
  assert.equal(periodText(null), '-');
  assert.equal(periodText(PAID), '2026-10-31 ~ 2026-11-29');
  assert.equal(periodText(sub({ period_start: '2026-10-31' })), '2026-10-31 ~ -');
});

test('원천이 없으면 미등록이다 — Free로 그리지 않는다', () => {
  assert.equal(sourceLabel(null).label, '미등록');
  assert.equal(planLabel(null), '미등록');
});

/* ── 서버 처리 대기(다시 읽기) — 활성화 대기 · 투영 지연 · 지난 권리 끝 시각 ── */

test('구매 요청 직후(PENDING)는 지금 요금제가 Free 그대로여도 서버 처리를 기다린다', () => {
  const data = res({ agreement: PENDING, available_actions: ['STOP'] });
  assert.equal(data.subscription?.plan, 'Free');
  assert.equal(awaitingServer(data, BEFORE_END), true);
  assert.match(agreementNote(data, BEFORE_END), /활성화 대기 중.*유료 권리가 없습니다/);
});

test('활성 계약이 투영까지 반영되면 기다리지 않는다', () => {
  assert.equal(awaitingServer(ACTIVE_PROJECTED, BEFORE_END), false);
  assert.equal(awaitingServer(RENEWAL_DISABLED, BEFORE_END), false);
  assert.match(agreementNote(ACTIVE_PROJECTED, BEFORE_END), /한 달 갱신됩니다/);
});

test('활성화됐지만 투영이 아직 앱 기본 Free면 유료 권리라고 하지 않고 반영을 기다린다', () => {
  const data = res({ agreement: agreement(), available_actions: ['CANCEL_RENEWAL', 'STOP'] });
  assert.equal(awaitingServer(data, BEFORE_END), true);
  assert.match(agreementNote(data, BEFORE_END), /아직 현재 구독에 반영되지 않았습니다/);
});

test('자동 갱신을 껐는데 투영이 아직 자동 갱신 중이면 반영을 기다린다', () => {
  const data = res({
    ...ACTIVE_PROJECTED,
    agreement: agreement({ auto_renew: false, renewal_disabled_at: RENEWAL_OFF_AT }),
    available_actions: ['STOP'],
  });
  assert.equal(awaitingServer(data, BEFORE_END), true);
});

test('권리 끝 시각이 지났는데 투영이 그대로면(처리 주기 전) 기다리고, 지난 기간이라고 말한다', () => {
  assert.equal(awaitingServer(ACTIVE_PROJECTED, AFTER_END), true);
  assert.match(agreementNote(ACTIVE_PROJECTED, AFTER_END), /기간 끝 시각이 지났습니다/);
});

test('만료된 계약이 아직 투영에 남아 있으면 기다리고, Free로 바뀌었으면 끝난다', () => {
  const expired = agreement({ status: 'EXPIRED', auto_renew: false, renewal_disabled_at: RENEWAL_OFF_AT, ended_at: TERM_END });
  const lagging = res({ ...RENEWAL_DISABLED, agreement: expired, available_actions: ['STOP'] });
  assert.equal(awaitingServer(lagging, AFTER_END), true);
  assert.match(agreementNote(lagging, AFTER_END), /아직 반영되지 않았습니다/);

  const settled = res({ agreement: expired });
  assert.equal(awaitingServer(settled, AFTER_END), false);
  assert.match(agreementNote(settled, AFTER_END), /만료됐습니다/);
});

test('활성화 실패는 기다리지 않고, 서버가 구매를 다시 열었을 때만 다시 요청하라고 말한다', () => {
  const retry = res({ agreement: FAILED });
  assert.equal(awaitingServer(retry, BEFORE_END), false);
  assert.match(agreementNote(retry, BEFORE_END), /유료 권리는 생기지 않았고.*다시 구매를 요청할 수 있습니다/);
  assert.equal(manageNote(retry), null);

  const closed = res({ agreement: FAILED, mock_marketplace_enabled: false, available_actions: ['STOP'] });
  assert.doesNotMatch(agreementNote(closed, BEFORE_END), /다시 구매/);
  assert.match(manageNote(closed) ?? '', /모의 Marketplace 구매 요청을 받지 않습니다/);
});

/* ── 버튼이 없는 이유 — 판정은 available_actions(서버) ── */

test('구독 변경 권한이 없으면 고객사 중앙계정만 요청할 수 있다고 말한다', () => {
  assert.match(manageNote(res({ can_manage: false, available_actions: [] })) ?? '', /구독 변경은 고객사 중앙계정만 요청할 수 있습니다/);
});

test('이용 중단 중에는 구매를 요청할 수 없는 이유를 말한다', () => {
  const data = res({ ...ACTIVE_PROJECTED, service_status: 'SUSPENDED', available_actions: ['CANCEL_RENEWAL'] });
  assert.match(manageNote(data) ?? '', /구매를 요청할 수 없습니다/);
});

test('연결 대기 · 미등록 · 수동 등록 유료 구독은 이유를 말한다', () => {
  assert.match(
    manageNote(res({ service_status: 'PENDING_CONNECTION', available_actions: [] })) ?? '',
    /연결이 확인되어 관제가 시작되면/,
  );
  assert.match(
    manageNote(res({ subscription: null, source: null, available_actions: ['STOP'] })) ?? '',
    /등록되지 않았습니다.*등록은 중앙 관리계정 또는 해당 고객사가 배정된 관리자 직원계정에 요청하세요/,
  );
  const manual = res({ subscription: sub({ plan: 'Pro', period_end: '2026-12-31' }), source: 'MANUAL', available_actions: ['STOP'] });
  assert.match(manageNote(manual) ?? '', /수동 등록된 구독은 이 화면에서 바꿀 수 없습니다.*중앙 관리계정 또는 해당 고객사가 배정된 관리자 직원계정에 문의하세요/);
  assert.equal(sourceNote('MANUAL', manual.subscription), '수동 등록된 기록입니다. 자동 갱신 · Free 전환을 하지 않습니다.');
});

test('상태 · 권한이 바뀐 실패는 확인창을 닫고 다시 읽는다. 일시 오류는 확인창에 남긴다', () => {
  assert.equal(isStaleSubscriptionRequest('TENANT_STATE_CONFLICT'), true);
  assert.equal(isStaleSubscriptionRequest('TENANT_NOT_FOUND'), true);
  assert.equal(isStaleSubscriptionRequest('AUTH_FORBIDDEN'), true);
  assert.equal(isStaleSubscriptionRequest('INTERNAL_ERROR'), false);
  assert.equal(isStaleSubscriptionRequest(null), false);
});

/* ── 성공 알림 — 구매 요청은 완료가 아니고, 갱신 끄기 · 이용 중단은 해지가 아니다 ── */

test('구매 요청 알림은 구매 완료가 아니라 활성화 대기 접수라고 말한다', () => {
  const text = changeNotice('purchase', res({ agreement: PENDING, available_actions: ['STOP'] }));
  assert.match(text, /Business 구매 요청을 접수했습니다\(활성화 대기\)/);
  assert.doesNotMatch(text, /구매 완료|결제 완료|구매했습니다/);
});

test('자동 갱신 끄기 알림은 요청한 동작이 끝났다고 말한다', () => {
  const text = changeNotice('cancel', RENEWAL_DISABLED);
  assert.match(text, /자동 갱신을 껐습니다/);
});

test('이용 중단 알림은 서비스 이용을 중단했다고 말한다', () => {
  assert.match(changeNotice('stop', res({ ...ACTIVE_PROJECTED, service_status: 'SUSPENDED' })), /서비스 이용을 중단했습니다/);
});

test('알림은 그 알림을 낸 상태가 바뀌면(활성화 · 실패) 내린다', () => {
  const pending = res({ agreement: PENDING });
  assert.equal(noticeSignature(pending), noticeSignature(res({ agreement: PENDING })));
  assert.notEqual(noticeSignature(pending), noticeSignature(res({ agreement: FAILED })));
  assert.notEqual(noticeSignature(pending), noticeSignature(ACTIVE_PROJECTED));
});

/* ── 고객사 관리 목록 ── */

test('고객사 관리 목록: 원천 · 자동 갱신 여부 · 마지막 이용일을 말하고, 미등록은 덧붙이지 않는다', () => {
  const mock = { subscription: PAID, subscription_source: 'MOCK_MARKETPLACE' as const };
  assert.equal(subscriptionListNote(mock), '모의 Marketplace 계약 · 자동 갱신 · 2026-11-29까지');
  assert.equal(
    subscriptionListNote({ ...mock, subscription: { ...PAID, cancel_at_period_end: true } }),
    '모의 Marketplace 계약 · 자동 갱신 꺼짐 · 2026-11-29까지',
  );
  assert.equal(
    subscriptionListNote({ subscription: sub({ plan: 'Enterprise', period_end: '2026-12-31' }), subscription_source: 'MANUAL' }),
    '수동 등록 · 2026-12-31까지',
  );
  assert.equal(subscriptionListNote({ subscription: sub(), subscription_source: 'APP_FREE' }), '앱 기본 Free');
  assert.equal(subscriptionListNote({ subscription: null, subscription_source: null }), null);
});
