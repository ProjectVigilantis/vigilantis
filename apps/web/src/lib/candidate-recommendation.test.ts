import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire, registerHooks } from 'node:module';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

import type {
  GuardDutyActivity,
  IncidentResponse,
  IsolationRecommendationBasis,
  IsolationRecommendationReason,
  RecommendationItem,
} from '../types/api.ts';

const sourceRoot = new URL('../', import.meta.url);
const require = createRequire(import.meta.url);
const nextEntries = new Map(['next/link', 'next/navigation'].map((specifier) => [
  specifier, pathToFileURL(require.resolve(specifier)).href,
]));
const compilerOptions: ts.CompilerOptions = {
  module: ts.ModuleKind.ESNext,
  moduleResolution: ts.ModuleResolutionKind.Bundler,
  jsx: ts.JsxEmit.ReactJSX,
  paths: { '@/*': [fileURLToPath(sourceRoot) + '*'] },
};

const activity: GuardDutyActivity = {
  resource_role: { status: 'RECORDED', value: 'ACTOR' },
  action_type: { status: 'RECORDED', value: 'NETWORK_CONNECTION' },
  connection_direction: { status: 'RECORDED', value: 'OUTBOUND' },
  remote_ip: { status: 'RECORDED', value: '203.0.113.42' },
  blocked: { status: 'RECORDED', value: false },
};
const basis: IsolationRecommendationBasis = {
  origin: 'SERVER_RULE',
  rule_id: 'PROTECTED_EC2_ISOLATION',
  rule_version: 1,
  classification: 'CONDITIONALLY_RECOMMENDED',
  reason_code: 'GUARDDUTY_SSH_BRUTE_FORCE_ACTOR',
  evaluated_at: '2026-11-13T01:02:03.123456Z',
};
const candidate: RecommendationItem = {
  runbook_id: 'RUNBOOK_EC2_ISOLATE',
  target_arn: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-candidate',
  display_parameters: {},
  ai_savings_estimate: null,
  evidence_ids: ['candidate-evidence'],
  recommendation_basis: basis,
  recommendation_evidence: {
    evidence_id: 'candidate-evidence',
    target_arn: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-candidate',
    finding_id: 'stored-finding',
    finding_type: 'UnauthorizedAccess:EC2/SSHBruteForce',
    resource_type: 'Instance',
    first_seen_at: '2026-11-12T01:00:00Z',
    last_seen_at: '2026-11-12T02:00:00Z',
    activity,
  },
};

test('stored candidate judgement presentation', async (t) => {
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
        return {
          format: 'module',
          source: ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions }).outputText,
          shortCircuit: true,
        };
      }
      return nextLoad(url, context);
    },
  });
  try {
    const { CandidateRecommendation } = await import('../components/incidents/candidate-recommendation.tsx');
    const render = (recommendation: RecommendationItem = candidate) =>
      renderToStaticMarkup(createElement(CandidateRecommendation, { recommendation }));
    const withActivity = (next: GuardDutyActivity | null): RecommendationItem => ({
      ...candidate,
      recommendation_evidence: { ...candidate.recommendation_evidence!, activity: next },
    });

    await t.test('conditional proposal keeps its human condition beside its stored rule and evidence', () => {
      const html = render();
      assert.match(html, /조건부 추천/);
      assert.match(html, /승인 전 확인:[\s\S]*정당한 업무 또는 승인된 테스트 사유가 없는지/);
      assert.match(html, /조건부 격리 제안[\s\S]*침해 사실이나 즉시 격리 필요성을 증명하지 않습니다/);
      assert.match(html, /가드레일 통과는 제안을 허용하는 조건일 뿐/);
      assert.match(html, /SERVER_RULE/);
      assert.match(html, /PROTECTED_EC2_ISOLATION · v1/);
      assert.match(html, /2026-11-13T01:02:03.123456Z/);
      assert.match(html, /href="#candidate-evidence-RUNBOOK_EC2_ISOLATE-candidate-evidence"/);
      assert.match(html, /id="candidate-evidence-RUNBOOK_EC2_ISOLATE-candidate-evidence"/);
      for (const fact of ['stored-finding', 'UnauthorizedAccess:EC2/SSHBruteForce', 'Instance',
        'i-candidate', '2026-11-12T01:00:00Z', '2026-11-12T02:00:00Z',
        'ACTOR', 'NETWORK_CONNECTION', 'OUTBOUND', '203.0.113.42']) {
        assert.ok(html.includes(fact), `stored fact visible: ${fact}`);
      }
      assert.match(html, /기록된 대상 주소/);
      assert.match(html, /차단 기록 \(Blocked\)[\s\S]*false · 기록됨/);
      assert.match(html, /false이며 성공·안전을 뜻하지 않습니다/);
    });

    await t.test('manual rows show their stored reason even when snapshot facts look positive', () => {
      const reasons: [IsolationRecommendationReason, RegExp][] = [
        ['TARGET_ADDRESS_NOT_PROVIDED', /유효한 대상 주소가 제공되지/],
        ['RESOURCE_ROLE_TARGET', /이 자원은 TARGET/],
        ['RESOURCE_ROLE_UNKNOWN', /자원 역할을 인식할 수 없/],
        ['RESOURCE_ROLE_NOT_CAPTURED', /자원 역할을 확인할 수 없/],
        ['FINDING_TYPE_NOT_COVERED', /탐지 유형은[\s\S]*규칙의 대상이 아닙니다/],
        ['DIRECTION_NOT_CONFIRMED', /OUTBOUND 방향을 확인할 수 없/],
        ['DIRECTION_CONFLICT', /자원 역할과 연결 방향이[\s\S]*조건에 맞지 않/],
        ['EVIDENCE_TARGET_MISMATCH', /증거의 대상과[\s\S]*후보의 대상이 일치하지/],
      ];
      for (const [reason_code, reason] of reasons) {
        const html = render({
          ...candidate,
          recommendation_basis: { ...basis, classification: 'OPERATOR_JUDGEMENT_REQUIRED', reason_code },
        });
        assert.match(html, /관제자 판단 필요/);
        assert.match(html, reason);
        assert.match(html, /격리를 추천한다는 뜻이 아닙니다/);
        assert.ok(html.includes(reason_code));
        assert.doesNotMatch(html, /승인 전 확인:/);
      }
    });

    await t.test('legacy missing judgement is not reconstructed from a positive snapshot', () => {
      for (const recommendation_basis of [null, undefined]) {
        const html = render({ ...candidate, recommendation_basis });
        assert.match(html, /저장된 후보 판단 없음/);
        assert.match(html, /탐지 사실이나 인시던트 요약으로 추천 여부를 추정하지 않습니다/);
        assert.doesNotMatch(html, /조건부 추천|관제자 판단 필요|SERVER_RULE/);
        assert.match(html, /stored-finding/);
      }
      const html = render({
        runbook_id: 'RUNBOOK_EC2_ISOLATE', target_arn: candidate.target_arn,
        display_parameters: {}, ai_savings_estimate: null,
      });
      assert.match(html, /저장된 근거 ID 없음/);
      assert.match(html, /저장된 탐지 스냅샷 없음/);
    });

    await t.test('uncollected, absent, invalid and unrecognized facts remain distinguishable', () => {
      assert.match(render(withActivity(null)), /활동 사실[\s\S]*미수집/);
      const html = render(withActivity({
        ...activity,
        resource_role: { status: 'ABSENT' },
        action_type: { status: 'UNRECOGNIZED', source_value: '<NEW_ACTION>' },
        connection_direction: { status: 'RECORDED', value: 'UNKNOWN' },
        remote_ip: { status: 'INVALID' },
      }));
      assert.match(html, /자원 역할[\s\S]*원천 누락/);
      assert.match(html, /인식 불가[\s\S]*&lt;NEW_ACTION&gt;/);
      assert.match(html, /연결 방향[\s\S]*UNKNOWN · 기록됨/);
      assert.match(html, /원격 IP \(원문 필드\)[\s\S]*유효하지 않은 원천 값/);
      assert.match(html, /차단 기록 \(Blocked\)[\s\S]*false · 기록됨/);
      assert.doesNotMatch(html, /<NEW_ACTION>/);
    });

    await t.test('remote address semantics require the recorded role, action and direction together', () => {
      assert.match(render(withActivity({
        ...activity, resource_role: { status: 'RECORDED', value: 'TARGET' },
        connection_direction: { status: 'RECORDED', value: 'INBOUND' },
      })), /기록된 상대 주소/);
      for (const next of [
        { ...activity, resource_role: { status: 'ABSENT' } },
        { ...activity, action_type: { status: 'RECORDED', value: 'PORT_PROBE' } },
        { ...activity, connection_direction: { status: 'RECORDED', value: 'INBOUND' } },
        { ...activity, connection_direction: { status: 'RECORDED', value: 'UNKNOWN' } },
      ] satisfies GuardDutyActivity[]) {
        const html = render(withActivity(next));
        assert.match(html, /원격 IP \(원문 필드\)/);
        assert.doesNotMatch(html, /<dt[^>]*>기록된 (대상|상대) 주소/);
      }
    });

    await t.test('incident detail separates model summary and does not substitute incident-wide evidence', async () => {
      const { IncidentDetail } = await import('../components/incidents/incident-detail.tsx');
      const { AppRouterContext } = require('next/dist/shared/lib/app-router-context.shared-runtime');
      const incident: IncidentResponse = {
        incident_id: 'stored-candidate',
        title: '서버 격리 제안',
        subject_arn: 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-incident',
        category: 'SECOPS', status: 'AWAITING_APPROVAL',
        initial_risk_level: 'MEDIUM', reviewed_risk_level: null,
        response_mode: 'AGENT_WAIT', threat_context: null,
        analysis_result: { status: 'NO_ACTION_AVAILABLE', failure_reason: 'NO_ACTION_AVAILABLE', guardrail_rejections: [] },
        summary_lines: ['INCIDENT_MODEL_SUMMARY', 'INCIDENT_SECOND_LINE', 'INCIDENT_THIRD_LINE'],
        evidence_ids: ['incident-wide-evidence'],
        recommendations: [candidate], executions: [],
        resolution: null, resolution_note: null, resolved_at: null,
        auto_isolation_block: 'WAIT_NOT_STARTED',
        created_at: '2026-11-13T00:00:00Z', updated_at: '2026-11-13T00:00:00Z',
      };
      const router = {
        back() {}, forward() {}, refresh() {}, push() {}, replace() {}, prefetch() {},
        bfcacheId: 'stored-candidate',
      };
      const html = renderToStaticMarkup(createElement(AppRouterContext.Provider, { value: router },
        createElement(IncidentDetail, { incident, assets: [] })));
      assert.match(html, /인시던트 분석 요약[\s\S]*후보별 서버 규칙 판단과는 별개[\s\S]*INCIDENT_MODEL_SUMMARY/);
      const proposals = html.slice(html.indexOf('제안 조치'));
      assert.match(proposals, /조건부 추천[\s\S]*stored-finding/);
      assert.doesNotMatch(proposals, /INCIDENT_MODEL_SUMMARY|incident-wide-evidence|i-incident/);
    });
  } finally {
    hooks.deregister();
  }
});
