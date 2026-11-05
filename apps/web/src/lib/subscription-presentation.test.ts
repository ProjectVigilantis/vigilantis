import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire, registerHooks } from 'node:module';
import { after, test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

import * as subscriptions from './subscriptions.ts';
import type { CustomerSubscriptionResponse } from '../types/api.ts';

// node --test strips TS, but TSX and the application's @/ imports need the existing compiler.
const sourceRoot = new URL('../', import.meta.url);
const require = createRequire(import.meta.url);
// Node 24 also hooks require.resolve; resolve Next's CJS entry before registering ours.
const nextLinkUrl = pathToFileURL(require.resolve('next/link')).href;
const compilerOptions: ts.CompilerOptions = {
  module: ts.ModuleKind.ESNext,
  moduleResolution: ts.ModuleResolutionKind.Bundler,
  jsx: ts.JsxEmit.ReactJSX,
  paths: { '@/*': [fileURLToPath(sourceRoot) + '*'] },
};
const hooks = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'next/link' && context.parentURL?.startsWith(sourceRoot.href)) {
      return nextResolve(nextLinkUrl, context);
    }
    if (specifier.startsWith('@/') || (specifier.startsWith('.') && context.parentURL?.startsWith(sourceRoot.href))) {
      const resolved = ts.resolveModuleName(specifier, fileURLToPath(context.parentURL!), compilerOptions, ts.sys).resolvedModule;
      if (resolved !== undefined) return nextResolve(pathToFileURL(resolved.resolvedFileName).href, context);
    }
    return nextResolve(specifier, context);
  },
  load(url, context, nextLoad) {
    if (url.startsWith(sourceRoot.href) && url.endsWith('.tsx')) {
      const source = ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions }).outputText;
      return { format: 'module', source, shortCircuit: true };
    }
    return nextLoad(url, context);
  },
});
after(() => hooks.deregister());

const { SubscriptionFacts } = await import('../components/subscriptions/subscription-facts.tsx');
const { SubscriptionView } = await import('../components/subscriptions/subscription-view.tsx');
const { formatKst } = await import('./utils.ts');

const END = '2026-12-04T15:00:00Z';
const BEFORE = Date.parse('2026-12-01T00:00:00Z');
const AFTER = Date.parse('2026-12-05T00:00:00Z');
const data: CustomerSubscriptionResponse = {
  tenant_name: '구독 검증 고객사',
  service_status: 'ACTIVE',
  subscription: {
    plan: 'Business', period_start: '2026-11-05', period_end: '2026-12-04',
    cancel_at_period_end: false, billing_anchor_day: 5, amount_minor: null, currency: null,
  },
  source: 'MOCK_MARKETPLACE',
  entitlement_ends_at: END,
  agreement: {
    agreement_id: 'mock-presentation', source: 'MOCK_MARKETPLACE', plan: 'Business',
    status: 'ACTIVE', auto_renew: true, requested_at: '2026-11-04T14:00:00Z',
    activated_at: '2026-11-04T15:00:00Z', term_start: '2026-11-04T15:00:00Z',
    term_end: END, renewal_disabled_at: null, ended_at: null, failure_code: null,
  },
  next_renewal_date: '2026-12-05',
  expiry_date: null,
  mock_marketplace_enabled: true,
  can_manage: true,
  available_actions: ['CANCEL_RENEWAL', 'STOP'],
};

function render(data: CustomerSubscriptionResponse, asOf = BEFORE) {
  return renderToStaticMarkup(createElement(SubscriptionView, {
    data, asOf, checkedAt: BEFORE, awaiting: subscriptions.awaitingServer(data, asOf),
    refreshError: null, reloading: false,
    onChange() {}, onReload() {}, onRefresh() {}, onHold() {},
  }));
}

test('customer summary keeps inclusive use dates and stale status, while standalone facts keep exact expiry and warnings', () => {
  const props = {
    subscription: data.subscription, source: data.source, entitlementEndsAt: END, asOf: AFTER,
  };
  const summary = renderToStaticMarkup(createElement(SubscriptionFacts, { ...props, presentation: 'summary' }));
  assert.match(summary, /2026-11-05 ~ 2026-12-04/);
  assert.match(summary, /직전 기간 기록/);
  assert.doesNotMatch(summary, /권리 끝 시각|반영 시점이 정해져 있지 않습니다/);

  const standalone = renderToStaticMarkup(createElement(SubscriptionFacts, props));
  assert.match(standalone, /권리 끝 시각/);
  assert.match(standalone, /직전 기간의 기록/);
  assert.match(standalone, /반영 시점이 정해져 있지 않습니다/);
});

test('waiting customer screen explains processing uncertainty once and retains the stale-record indicator', () => {
  const html = render(data, AFTER);
  assert.equal(html.split('반영 시점이 정해져 있지 않습니다').length - 1, 1);
  assert.match(html, /직전 기간 기록/);
  assert.match(html, /실제 AWS 구매\/청구 없음/);
  assert.match(html, /요금 · 혜택 · SLA는 확정 전입니다/);
});

test('renewal-off consequences have one owner and summary dates do not turn last-use day into the expiry boundary', () => {
  const off = {
    ...data, subscription: { ...data.subscription!, cancel_at_period_end: true },
    agreement: { ...data.agreement!, auto_renew: false, renewal_disabled_at: '2026-11-10T00:00:00Z' },
    next_renewal_date: null, expiry_date: '2026-12-05', available_actions: ['STOP'] as const,
  };
  const html = render({ ...off, available_actions: [...off.available_actions] });
  assert.equal(html.split('서버가 만료를 처리하면 앱 기본 Free로 돌아갑니다').length - 1, 1);
  assert.match(html, /2026-11-05 ~ 2026-12-04/);
  assert.match(html, /만료 · 앱 기본 Free\(예정\).*?2026-12-05/);
});

test('contract details distinguish a stale projection boundary from the renewed contract boundary', () => {
  const html = render({ ...data, agreement: { ...data.agreement!, term_end: '2027-01-04T15:00:00Z' } }, AFTER);
  assert.match(html, /현재 구독 기록의 권리 끝 시각/);
  assert.match(html, /2026\. 12\. 5\. 00:00:00 KST/);
  assert.match(html, /2027\. 1\. 5\. 00:00:00 KST/);
});

test('subscription clock uses midnight 00 in KST without changing the default formatter', () => {
  assert.equal(formatKst(END, '24h'), '2026. 12. 5. 00:00:00 KST');
  assert.match(formatKst(END), /12:00:00 KST$/);
  assert.equal(formatKst(null, '24h'), '-');
});

test('stop advice offers separate renewal cancellation only when the server opens that action', () => {
  const allowed = subscriptions.stopDialogNote(data);
  const closed = subscriptions.stopDialogNote({ ...data, available_actions: ['STOP'] });
  assert.match(allowed, /\[자동 갱신 끄기\]를 따로 요청하세요/);
  assert.doesNotMatch(closed, /\[자동 갱신 끄기\]/);
  for (const text of [allowed, closed]) {
    assert.match(text, /구독 계약을 해지하지 않습니다/);
    assert.match(text, /중앙 관리계정 또는 해당 고객사가 배정된 관리자 직원계정/);
  }
});

test('stopped screen owns one resume destination without hiding the employee permission reason', () => {
  const stopped = { ...data, service_status: 'SUSPENDED' as const, available_actions: ['CANCEL_RENEWAL'] as CustomerSubscriptionResponse['available_actions'] };
  const html = render(stopped);
  assert.equal(html.split('재개는 중앙 관리계정 또는').length - 1, 1);
  assert.match(html, /구독 계약을 해지하지 않습니다/);
  assert.match(html, /계약의 갱신 · 만료는 중단 중에도 그대로 진행됩니다/);
  const employee = render({ ...stopped, can_manage: false, available_actions: [] });
  assert.match(employee, /구독 변경은 고객사 중앙계정만 요청할 수 있습니다/);
});
