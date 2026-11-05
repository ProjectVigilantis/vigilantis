import assert from 'node:assert/strict';
import { test, type TestContext } from 'node:test';

import { createSubscriptionRefresher, type SubscriptionLoad } from './subscription-refresher.ts';
import { SUBSCRIPTION_POLL_MS } from './subscriptions.ts';
import type { CustomerSubscriptionResponse, MockAgreement } from '../types/api.ts';

// 기간 끝 2026-11-30 00시(KST) = 2026-11-29T15:00Z. 마지막 이용일 2026-11-29.
const TERM_END = '2026-11-29T15:00:00Z';

const ACTIVE: MockAgreement = {
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
};

/** 활성 계약이 투영까지 반영된 응답(자동 갱신 중) — 서버가 아직 처리할 것이 없는 상태다. */
const PROJECTED: CustomerSubscriptionResponse = {
  tenant_name: '학원',
  service_status: 'ACTIVE',
  subscription: {
    plan: 'Business', period_start: '2026-10-31', period_end: '2026-11-29', cancel_at_period_end: false,
    billing_anchor_day: 31, amount_minor: null, currency: null,
  },
  source: 'MOCK_MARKETPLACE',
  entitlement_ends_at: TERM_END,
  agreement: ACTIVE,
  next_renewal_date: '2026-11-30',
  expiry_date: null,
  mock_marketplace_enabled: true,
  can_manage: true,
  available_actions: ['CANCEL_RENEWAL', 'STOP'],
};

/** 구매 요청 직후 — 앱 기본 Free 그대로이고 계약은 활성화 대기다. */
const PENDING: CustomerSubscriptionResponse = {
  ...PROJECTED,
  subscription: { ...PROJECTED.subscription!, plan: 'Free', period_start: null, period_end: null, billing_anchor_day: null },
  source: 'APP_FREE',
  entitlement_ends_at: null,
  agreement: { ...ACTIVE, status: 'PENDING', activated_at: null, term_start: null, term_end: null },
  next_renewal_date: null,
  available_actions: ['STOP'],
};

/** 프로미스 콜백을 모두 돌린다. setImmediate 는 가짜 시계 밖이다. */
const settle = () => new Promise<void>((resolve) => setImmediate(resolve));

/** 생애주기 소유자를 가짜 시계 위에 세운다. 조회는 호출마다 테스트가 정한 응답을 돌려준다. */
function harness(t: TestContext, startAt: string, reply: () => Promise<CustomerSubscriptionResponse>) {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: Date.parse(startAt) });
  const loads: SubscriptionLoad[] = [];
  let reads = 0;
  let visible = true;
  const refresher = createSubscriptionRefresher({
    read: () => {
      reads += 1;
      return reply();
    },
    onChange: (load) => loads.push(load),
    isVisible: () => visible,
  });
  t.after(() => refresher.dispose());
  const last = () => loads.at(-1);
  /** 마지막 상태의 대기 판정. 응답 전이면 null. */
  const awaiting = () => {
    const load = last();
    return load?.kind === 'ok' ? load.awaiting : null;
  };
  const setVisible = (next: boolean) => {
    visible = next;
  };
  return { refresher, reads: () => reads, last, awaiting, setVisible };
}

/**
 * 기간 끝 1분 전에 연 화면 — 첫 조회는 바로 오고, 그 뒤 조회는 테스트가 풀 때까지 응답이 없다.
 * 기간 끝(15:00Z) 직전까지 시계를 돌려 둔다.
 */
async function openBeforeTermEnd(t: TestContext) {
  const late: ((data: CustomerSubscriptionResponse) => void)[] = [];
  let calls = 0;
  const h = harness(t, '2026-11-29T14:59:00Z', () =>
    ++calls === 1
      ? Promise.resolve(PROJECTED)
      : new Promise<CustomerSubscriptionResponse>((resolve) => late.push(resolve)),
  );
  h.refresher.start();
  await settle();
  assert.equal(h.reads(), 1);
  assert.equal(h.awaiting(), false);
  t.mock.timers.tick(59_999);
  await settle();
  return { h, late };
}

/** 지금 화면이 지난 권리를 알리되 서버가 준 요금제 · 기간을 그대로 두는지 본다. */
function assertLapsedWithServerData(load: SubscriptionLoad | undefined) {
  assert.equal(load?.kind, 'ok');
  if (load?.kind !== 'ok') return;
  assert.ok(load.asOf >= Date.parse(TERM_END), '지금 시각으로 다시 판정해 알린다');
  assert.equal(load.awaiting, true);
  assert.equal(load.data.subscription?.plan, 'Business', '서버가 바꾸기 전에는 서버 값 그대로다');
  assert.equal(load.data.subscription?.period_end, '2026-11-29');
}

test('열어 둔 화면에서 권리 끝 시각이 지나면 응답을 기다리기 전에 지난 권리를 알리고, 조회는 하나만 둔다', async (t) => {
  await t.test('늦은 경계 조회 동안 주기가 지나도 새 조회가 없고, 그 응답을 받아들인 뒤부터 다시 센다', async (t) => {
    const { h, late } = await openBeforeTermEnd(t);
    t.mock.timers.tick(1); // 기간 끝 시각에 닿는다
    await settle();
    assert.equal(h.reads(), 2, '기간 끝 시각에 서버 값을 다시 읽어야 한다');
    assertLapsedWithServerData(h.last());

    t.mock.timers.tick(SUBSCRIPTION_POLL_MS * 2); // 응답 전에 주기가 두 번 지난다
    await settle();
    assert.equal(h.reads(), 2, '경계 조회를 기다리는 동안 새 조회를 보내지 않는다');

    late[0](PROJECTED);
    await settle();
    const settledAt = Date.now();
    const accepted = h.last();
    assert.equal(accepted?.kind === 'ok' && accepted.checkedAt, settledAt, '늦게 온 경계 응답이 버려지지 않는다');
    assertLapsedWithServerData(accepted);

    t.mock.timers.tick(SUBSCRIPTION_POLL_MS - 1);
    await settle();
    assert.equal(h.reads(), 2, '다음 차례는 응답을 받은 뒤부터 센다');
    t.mock.timers.tick(1);
    await settle();
    assert.equal(h.reads(), 3);
  });

  await t.test('늦은 경계 조회 중에 화면을 떠나면 그 뒤 조회가 없다', async (t) => {
    const { h } = await openBeforeTermEnd(t);
    t.mock.timers.tick(1);
    await settle();
    assert.equal(h.reads(), 2);

    h.refresher.dispose();
    t.mock.timers.tick(SUBSCRIPTION_POLL_MS * 2);
    await settle();
    assert.equal(h.reads(), 2, '떠난 화면은 다시 읽지 않는다');
  });

  await t.test('탭이 가려진 채 경계에 닿으면 알리기만 하고, 보이게 된 뒤 차례에 한 번 읽는다', async (t) => {
    const { h } = await openBeforeTermEnd(t);
    h.setVisible(false);
    t.mock.timers.tick(1);
    await settle();
    assert.equal(h.reads(), 1, '가려진 탭에서는 그 차례를 건너뛴다');
    assertLapsedWithServerData(h.last());

    h.setVisible(true);
    t.mock.timers.tick(SUBSCRIPTION_POLL_MS);
    await settle();
    assert.equal(h.reads(), 2, '보이게 된 뒤 차례에 한 번만 읽는다');
  });
});

test('다시 읽기가 주기보다 오래 걸려도 다음 조회를 겹쳐 보내지 않고 그 응답을 받아들인다', async (t) => {
  const pending: { resolve: (data: CustomerSubscriptionResponse) => void }[] = [];
  const h = harness(t, '2026-11-01T00:00:00Z', () =>
    new Promise<CustomerSubscriptionResponse>((resolve) => pending.push({ resolve })),
  );
  h.refresher.start();
  pending[0].resolve(PENDING);
  await settle();
  assert.equal(h.awaiting(), true);

  t.mock.timers.tick(SUBSCRIPTION_POLL_MS); // 두 번째 조회 시작 — 응답이 늦다
  await settle();
  assert.equal(h.reads(), 2);
  t.mock.timers.tick(SUBSCRIPTION_POLL_MS * 2); // 응답 전에 주기가 두 번 더 지난다
  await settle();
  assert.equal(h.reads(), 2, '응답을 기다리는 동안 새 조회를 보내지 않는다');

  pending[1].resolve(PROJECTED);
  await settle();
  const load = h.last();
  assert.equal(load?.kind, 'ok');
  if (load?.kind !== 'ok') return;
  assert.equal(load.data.agreement?.status, 'ACTIVE', '늦게 온 성공 응답이 버려지지 않는다');
});
