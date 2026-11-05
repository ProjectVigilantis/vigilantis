// 고객 구독 화면의 조회 생애주기(T-16 · #528) — 첫 조회 · 다시 읽기 · 쓰기 응답 반영 · 정리를 한 곳이 소유한다.
// React 경계(components/subscriptions/subscription-section.tsx)는 이것을 마운트 동안 하나 만들고 상태만 그린다.
//
// - 조회는 한 번에 하나다(single-flight). 다시 읽기 주기는 앞 응답이 온 뒤부터 센다 — 응답이 주기보다 늦어도
//   다음 조회가 겹치거나 늦은 응답을 버리지 않는다. 늦은 응답을 버리는 것은 그 뒤에 더 새 값이 들어왔을 때뿐이다
//   (쓰기 응답 `accept` · 요청 무효 뒤 다시 읽기 `reload`).
// - 기다릴 것(lib/subscriptions.ts `awaitingServer`)은 응답을 받은 시각이 아니라 판정하는 지금 시각으로 본다.
//   기다릴 것이 없어도 서버가 준 다음 경계(권리 끝 · 계약 기간 끝)에 타이머를 걸어, 열어 둔 화면이 그 시각에
//   다시 판정하고 서버 값을 다시 읽게 한다. Free 전환 · 갱신을 화면이 만들지 않는다 — 바뀐 값은 서버 응답뿐이다.
// - 확인창이 열린 동안(`hold`)과 정리 뒤(`dispose`)에는 타이머가 없다. 탭이 가려져 있으면 그 차례를 건너뛴다.
// - 이 모듈의 다시 읽기는 화면 갱신일 뿐이다. 계약 처리(활성화 · 갱신 · 만료)는 서버가 정하고 앞당기지 않는다.

import { awaitingServer, nextEntitlementBoundary, SUBSCRIPTION_POLL_MS } from './subscriptions.ts';
import type { CustomerSubscriptionResponse } from '../types/api.ts';

export type SubscriptionLoad =
  | { kind: 'loading' }
  | { kind: 'error'; error: unknown }
  | {
      kind: 'ok';
      data: CustomerSubscriptionResponse;
      /** `data`를 받은 시각(ms). */
      checkedAt: number;
      /** 아래 판정(`awaiting` · 지난 권리)의 기준 시각(ms) — 경계 타이머가 다시 판정하면 `checkedAt`보다 뒤다. */
      asOf: number;
      /** 서버 처리를 기다리는 중이라 주기적으로 다시 읽는다. */
      awaiting: boolean;
      /** 그 뒤 다시 읽기의 실패. null = 없음. 화면은 `data`(마지막으로 읽은 값) 그대로다. */
      refreshError: unknown;
      /** 요청 무효(409 · 403) 뒤 다시 읽는 중 — `data`가 낡았으므로 새 쓰기를 열지 않는다. */
      reloading: boolean;
    };

export interface SubscriptionRefresherOptions {
  read: () => Promise<CustomerSubscriptionResponse>;
  onChange: (load: SubscriptionLoad) => void;
  /** 탭이 보이는가. 기본은 `document.visibilityState`다(문서가 없으면 보인다고 본다). */
  isVisible?: () => boolean;
}

export interface SubscriptionRefresher {
  /** 첫 조회. */
  start(): void;
  /** 첫 조회 실패 뒤 다시 시도 — 불러오는 중부터 다시 그린다. */
  retry(): void;
  /** 지금 다시 읽기(수동). 조회가 이미 진행 중이면 그 응답을 기다린다. */
  refresh(): void;
  /** 요청이 무효가 됐다(409 · 403) — 진행 중 조회를 버리고 바로 다시 읽으며 그동안 쓰기를 막는다. */
  reload(): void;
  /** 쓰기 성공 응답 — 그보다 먼저 시작한 조회의 응답이 이것을 덮지 않게 한다. */
  accept(data: CustomerSubscriptionResponse): void;
  /** 확인창이 열렸다(true) · 닫혔다(false). 열린 동안 자동으로 다시 읽지 않는다. */
  hold(held: boolean): void;
  /** 화면을 떠났다 — 타이머를 지우고 진행 중 응답을 버린다. */
  dispose(): void;
}

/** setTimeout 이 받는 가장 긴 지연(약 24.8일). 더 먼 경계는 이 간격으로 다시 판정한다. */
const MAX_TIMER_MS = 2 ** 31 - 1;

const documentVisible = () => typeof document === 'undefined' || document.visibilityState === 'visible';

export function createSubscriptionRefresher(options: SubscriptionRefresherOptions): SubscriptionRefresher {
  const isVisible = options.isVisible ?? documentVisible;
  let load: SubscriptionLoad = { kind: 'loading' };
  /** 받아들일 조회의 번호. 올리면 진행 중 조회의 응답을 버린다. */
  let generation = 0;
  let inFlight = false;
  let disposed = false;
  let held = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const clearTimer = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };

  /** 상태를 바꾸고 알린 뒤 다음 할 일(다시 읽기 · 경계 타이머)을 건다. 판정은 지금 시각으로 한다. */
  const update = (next: SubscriptionLoad) => {
    clearTimer();
    const now = Date.now();
    load = next.kind === 'ok' ? { ...next, asOf: now, awaiting: awaitingServer(next.data, now) } : next;
    if (!disposed) options.onChange(load);
    arrange(now);
  };

  const arrange = (now: number) => {
    if (disposed || held || inFlight || load.kind !== 'ok' || load.reloading) return;
    if (load.awaiting) {
      timer = setTimeout(due, SUBSCRIPTION_POLL_MS);
      return;
    }
    const boundary = nextEntitlementBoundary(load.data, now);
    if (boundary !== null) timer = setTimeout(atBoundary, Math.min(boundary - now, MAX_TIMER_MS));
  };

  // 타이머 핸들은 clearTimer · arrange만 쓴다. 아래 콜백은 타이머로도 직접 호출로도 불리므로 핸들을 비우지 않는다 —
  // 직접 호출 앞에 arrange가 건 타이머를 비우면 취소할 수 없는 타이머가 남아 조회가 겹치거나 정리 뒤에 읽는다.

  /** 다시 읽을 차례. 탭이 가려져 있으면 건너뛰고 다음 차례를 건다. */
  const due = () => {
    if (isVisible()) fetch();
    else if (load.kind === 'ok') update(load);
  };

  /**
   * 서버가 준 경계에 닿았다(또는 가장 긴 지연이 지났다) — 지금 시각으로 다시 판정한 상태를 먼저 알리고(지난 권리
   * 안내가 다시 읽기 응답을 기다리지 않는다 · 서버 값은 그대로), 기다릴 것이 생겼으면 바로 읽는다.
   */
  const atBoundary = () => {
    if (load.kind !== 'ok') return;
    update(load);
    if (load.kind === 'ok' && load.awaiting) due();
  };

  const fetch = () => {
    clearTimer();
    const read = ++generation;
    inFlight = true;
    const current = () => !disposed && read === generation;
    options.read().then(
      (data) => {
        if (!current()) return;
        inFlight = false;
        const checkedAt = Date.now();
        update({ kind: 'ok', data, checkedAt, asOf: checkedAt, awaiting: false, refreshError: null, reloading: false });
      },
      (error: unknown) => {
        if (!current()) return;
        inFlight = false;
        update(load.kind === 'ok' ? { ...load, refreshError: error, reloading: false } : { kind: 'error', error });
      },
    );
  };

  return {
    start: fetch,
    retry() {
      update({ kind: 'loading' });
      fetch();
    },
    refresh() {
      if (!inFlight) fetch();
    },
    reload() {
      if (load.kind === 'ok') update({ ...load, reloading: true });
      fetch();
    },
    accept(data) {
      generation += 1;
      inFlight = false;
      const checkedAt = Date.now();
      update({ kind: 'ok', data, checkedAt, asOf: checkedAt, awaiting: false, refreshError: null, reloading: false });
    },
    hold(next) {
      held = next;
      clearTimer();
      if (load.kind === 'ok') update(load);
    },
    dispose() {
      disposed = true;
      generation += 1;
      clearTimer();
    },
  };
}
