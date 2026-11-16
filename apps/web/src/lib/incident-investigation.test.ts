// 인시던트 상세의 사고 조사 항목 — `npm test`. 상세와 감사 목록을 `execution_id`로 잇는 규칙(#622 코멘트)을 고정한다.
//
// 지키는 것은 넷이다. ① 감사 기록이 없는 실행의 요청 주체 · 대상을 다른 값(추천 · 인시던트 ARN)으로 채우지 않는다
// ② 원복 자식은 원 실행 아래에 묶는다 ③ 커서는 해석하지 않고 그대로 돌려준다 ④ AI 요약과 분석 결과를 다른 구역에 둔다.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire, registerHooks } from 'node:module';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

import {
  UNCONFIRMED,
  executionRows,
  latestResolution,
  loadIncidentAudit,
  startedByLabel,
  type IncidentAudit,
} from './incident-investigation.ts';
import type {
  AuditEventItem,
  ExecutionStartedEvent,
  ExecutionSummaryItem,
  IncidentResolvedEvent,
  IncidentResponse,
} from '../types/api.ts';

const T0 = '2026-11-16T00:00:00Z';

function execution(id: string, over: Partial<ExecutionSummaryItem> = {}): ExecutionSummaryItem {
  return {
    execution_id: id, runbook_id: 'RUNBOOK_EC2_ISOLATE', status: 'SUCCESS', available_recovery_runbook_ids: [],
    verification_hold: null, created_at: T0, finished_at: null, parent_execution_id: null, updated_at: T0, ...over,
  };
}

function started(executionId: string, over: Partial<ExecutionStartedEvent> = {}): ExecutionStartedEvent {
  return {
    event_id: `ev-${executionId}`, occurred_at: T0, incident_id: 'inc-1', tenant_id: null,
    actor: { kind: 'TENANT_USER', email: 'ops@example.com' },
    action: 'ACTION_EXECUTION_STARTED', trigger_source: 'USER_APPROVAL', execution_id: executionId,
    runbook_id: 'RUNBOOK_EC2_ISOLATE', target_arn: `arn:aws:ec2:ap-northeast-2:123456789012:instance/i-${executionId}`,
    ...over,
  };
}

function resolved(eventId: string, occurredAt: string, email: string): IncidentResolvedEvent {
  return {
    event_id: eventId, occurred_at: occurredAt, incident_id: 'inc-1', tenant_id: null,
    actor: { kind: 'TENANT_USER', email }, action: 'INCIDENT_RESOLVED', resolution: 'JUSTIFIED',
  };
}

test('커서는 받은 값 그대로 다음 쪽에 넘기고, null이면 멈춘다', async () => {
  const seen: (string | undefined)[] = [];
  const pages = [
    { items: [started('a')], next_cursor: 'c1==' },
    { items: [started('b')], next_cursor: null },
  ];
  const out = await loadIncidentAudit(async (cursor) => {
    seen.push(cursor);
    return pages[seen.length - 1];
  });
  assert.deepEqual(seen, [undefined, 'c1==']);
  assert.equal(out.items.length, 2);
  assert.equal(out.truncated, false);
});

test('쪽 상한에 닿으면 잘렸다고 알린다', async () => {
  let calls = 0;
  const out = await loadIncidentAudit(async () => {
    calls += 1;
    return { items: [started(`x${calls}`)], next_cursor: `c${calls}` };
  }, 3);
  assert.equal(calls, 3);
  assert.equal(out.truncated, true);
});

test('실행과 예약 기록을 execution_id로 잇고, 기록이 없는 실행은 null로 둔다', () => {
  const rows = executionRows([execution('a'), execution('b')], [started('b'), resolved('r', T0, 'x@example.com')]);
  assert.deepEqual(rows.map((r) => [r.execution.execution_id, r.started?.event_id ?? null]), [['a', null], ['b', 'ev-b']]);
});

test('원복 자식은 원 실행 아래에 묶고, 원 실행이 목록에 없으면 맨 위 줄로 둔다', () => {
  const rows = executionRows(
    [
      execution('origin'),
      execution('child', { runbook_id: 'RUNBOOK_EC2_UNISOLATE', parent_execution_id: 'origin', status: 'FAILED' }),
      execution('orphan', { parent_execution_id: 'gone' }),
    ],
    [],
  );
  assert.deepEqual(rows.map((r) => r.execution.execution_id), ['origin', 'orphan']);
  assert.deepEqual(rows[0].recoveries.map((r) => [r.execution.execution_id, r.execution.status]), [['child', 'FAILED']]);
});

test('요청 주체 — 사람 승인은 이메일과 승인, 자동 발동은 시스템 사유, 고객사에 숨긴 운영자 이메일은 플랫폼 운영자', () => {
  assert.equal(startedByLabel(started('a')), 'ops@example.com 승인');
  assert.equal(
    startedByLabel(started('b', { actor: { kind: 'PLATFORM_USER', email: null } })),
    '플랫폼 운영자 승인',
  );
  assert.equal(
    startedByLabel(started('c', { actor: { kind: 'SYSTEM', email: null }, trigger_source: 'PRE_MITIGATION_0_5S' })),
    '시스템 · 선제 차단',
  );
});

test('종료 처리 기록은 가장 늦은 것을 고른다 — 재개 후 다시 종료한 건', () => {
  const audit: AuditEventItem[] = [
    resolved('r2', '2026-11-16T02:00:00Z', 'second@example.com'),
    resolved('r1', '2026-11-16T01:00:00Z', 'first@example.com'),
  ];
  assert.equal(latestResolution(audit)?.event_id, 'r2');
  assert.equal(latestResolution([started('a')]), null);
});

test('상세 화면: 요청 주체 · 대상은 감사 기록에서만, 없으면 확인 안 됨 — 추천이나 인시던트 ARN으로 채우지 않는다', async () => {
  // node --test의 TSX · @/ 로딩은 proposal-buttons 테스트와 같은 컴파일러를 쓴다.
  const sourceRoot = new URL('../', import.meta.url);
  const require = createRequire(import.meta.url);
  const nextEntries = new Map(['next/link', 'next/navigation'].map((specifier) => [
    specifier, pathToFileURL(require.resolve(specifier)).href,
  ]));
  const { AppRouterContext } = require('next/dist/shared/lib/app-router-context.shared-runtime');
  const compilerOptions: ts.CompilerOptions = {
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    jsx: ts.JsxEmit.ReactJSX,
    paths: { '@/*': [fileURLToPath(sourceRoot) + '*'] },
  };
  const hooks = registerHooks({
    resolve(specifier, context, nextResolve) {
      if (nextEntries.has(specifier) && context.parentURL?.startsWith(sourceRoot.href)) {
        return nextResolve(nextEntries.get(specifier)!, context);
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
  try {
    const { IncidentDetail } = await import('../components/incidents/incident-detail.tsx');
    const subjectArn = 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-subject';
    const recommendationArn = 'arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-recommended';
    const detail: IncidentResponse = {
      incident_id: 'inc-1', title: 'SSH 무차별 대입', subject_arn: subjectArn, category: 'SECOPS',
      status: 'RESOLVED', initial_risk_level: 'HIGH', reviewed_risk_level: null, response_mode: 'PRE_MITIGATION_0_5S',
      threat_context: { event_type: 'SSH_BRUTE_FORCE', source_ip: '203.0.113.7' },
      analysis_result: {
        status: 'PROPOSALS_GENERATED',
        guardrail_rejections: [{ runbook_id: 'RUNBOOK_EC2_ISOLATE', failed_step: 'ARN_MATCH', reason_code: 'ARN_TARGET_NOT_MANAGED' }],
        failure_reason: null,
      },
      summary_lines: ['AI 서술 1', 'AI 서술 2', 'AI 서술 3'],
      evidence_ids: ['ev-1'],
      recommendations: [{
        runbook_id: 'RUNBOOK_NACL_ADD_DENY', target_arn: recommendationArn, display_parameters: {}, ai_savings_estimate: null,
      }],
      executions: [
        execution('audited', { finished_at: '2026-11-16T00:00:05Z' }),
        execution('legacy', {
          status: 'UNVERIFIED',
          verification_hold: { reason_code: 'PRECHECK_AWS_ERROR', attempts: 3, first_failed_at: T0, last_failed_at: T0 },
        }),
      ],
      resolution: 'JUSTIFIED', resolution_note: null, resolved_at: T0, auto_isolation_block: null,
      created_at: T0, updated_at: T0,
    };
    const router = {
      back() {}, forward() {}, refresh() {}, push() {}, replace() {}, prefetch() {},
      bfcacheId: 'investigation',
    };
    const render = (audit: IncidentAudit, incident: IncidentResponse = detail) =>
      renderToStaticMarkup(createElement(AppRouterContext.Provider, { value: router },
        createElement(IncidentDetail, { incident, audit, assets: [] })));
    const audit: IncidentAudit = {
      kind: 'ready',
      items: [started('audited'), resolved('r', T0, 'closer@example.com')],
      truncated: false,
    };
    // 종료 기록 구역은 남은 제안이 없을 때만 선다.
    const closed: IncidentResponse = { ...detail, recommendations: [] };

    const ready = render(audit);
    // 위험도 안내문에도 「수행된 조치」가 나오므로 구역 제목으로 자른다.
    const executions = ready.slice(ready.indexOf('>수행된 조치</h2>'), ready.indexOf('>제안 조치</h2>'));
    assert.ok(executions.length > 0);
    assert.match(executions, /ops@example\.com 승인/);
    assert.match(executions, /instance\/i-audited/);
    assert.match(executions, new RegExp(UNCONFIRMED));
    // 감사 기록이 없는 실행(legacy)의 대상을 추천 · 인시던트 ARN으로 채우지 않는다.
    assert.doesNotMatch(executions, /acl-recommended|i-subject/);
    assert.match(executions, /PRECHECK_AWS_ERROR/);
    assert.match(render(audit, closed), /종료 처리[\s\S]*closer@example\.com/);
    assert.match(ready, /관측된 출발지[\s\S]*203\.0\.113\.7/);
    // 분석 결과(서버 계산)가 AI 요약보다 앞 구역에 따로 선다.
    assert.ok(ready.indexOf('분석 결과') < ready.indexOf('판단 근거 (AI 요약)'));
    assert.match(ready, /③ ARN 대조 거절/);
    // 세션을 아직 모르면 내려받기는 비활성 버튼이다(링크가 아니다).
    assert.doesNotMatch(ready, /evidence\/export/);
    assert.match(ready, /Markdown 내려받기/);

    const failed = render({ kind: 'failed' }, closed);
    assert.match(failed, /감사 기록을 읽지 못해/);
    assert.doesNotMatch(failed, /ops@example\.com|closer@example\.com/);
    assert.match(failed, new RegExp(`종료 처리[\\s\\S]*${UNCONFIRMED}`));

    // 실행이 없으면 「수행된 조치」 구역이 없다. 조회 실패 · 잘림 안내는 종료 기록 구역에 남아야 한다(PR #647 리뷰).
    const noExecutions: IncidentResponse = { ...closed, executions: [] };
    const failedNoExec = render({ kind: 'failed' }, noExecutions);
    assert.doesNotMatch(failedNoExec, />수행된 조치<\/h2>/);
    assert.match(failedNoExec, /감사 기록을 읽지 못해 종료 처리자를 확인하지 못했습니다/);
    const truncatedNoExec = render({ kind: 'ready', items: [], truncated: true }, noExecutions);
    assert.match(truncatedNoExec, /감사 기록이 많아 최근 0건만 읽었습니다/);
    // 온전히 읽었는데 종료 기록이 없으면(감사 기록 도입 전 종료) 안내 없이 확인 안 됨만 쓴다.
    const legacyNoExec = render({ kind: 'ready', items: [], truncated: false }, noExecutions);
    assert.match(legacyNoExec, new RegExp(`종료 처리[\\s\\S]*${UNCONFIRMED}`));
    assert.doesNotMatch(legacyNoExec, /감사 기록을 읽지 못해|감사 기록이 많아/);
    // 처리자를 찾았으면 잘렸어도 그 칸 안내는 붙이지 않는다.
    const foundNoExec = render({ kind: 'ready', items: [resolved('r', T0, 'closer@example.com')], truncated: true }, noExecutions);
    assert.doesNotMatch(foundNoExec, /감사 기록이 많아/);
  } finally {
    hooks.deregister();
  }
});
