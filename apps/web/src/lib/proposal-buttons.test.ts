// 제안 조치 버튼 규칙 회귀 — `npm test`. 인시던트 분류로 문구를 가르던 자리를 되살리면 여기서 막힌다(#363).
//
// 지키는 것은 셋이다. ① T2 7단계 「원클릭 해제」에서 `승인하고 차단`이 나오지 않는다(핵심 컷이
// 동작과 반대로 말하던 자리) ② 파괴적 2종의 문구가 삭제로 읽힌다 ③ `차단 안 함`은 차단 제안에만 붙는다.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { DESTRUCTIVE_RUNBOOK_IDS } from './enum-labels.ts';
import {
  MIXED_APPROVE_LABEL,
  RUNBOOK_ACTION_KINDS,
  proposalActionKind,
  proposalButtons,
} from './proposal-buttons.ts';
import { ROLLBACK_RUNBOOK_IDS, RUNBOOK_IDS } from '../types/api.ts';
import type { AiRecommendableRunbookId, IncidentResponse, ResponseMode } from '../types/api.ts';

type Buttons = Pick<IncidentResponse, 'recommendations' | 'response_mode'>;

/** 문구·반려 판정이 보는 필드는 둘뿐이라(`recommendations`·`response_mode`) 나머지는 세우지 않는다. */
const incident = (
  runbookIds: AiRecommendableRunbookId[],
  responseMode: ResponseMode | null = null,
): Buttons => ({
  recommendations: runbookIds.map((runbook_id) => ({
    runbook_id,
    target_arn: 'arn:aws:ec2:ap-northeast-2:123456789012:network-acl/acl-0a1b2c3d',
    display_parameters: {},
    ai_savings_estimate: null,
  })),
  response_mode: responseMode,
});

test('해제 후보(NACL_RESTORE)에 차단 문구가 붙지 않는다 — T2 7단계 핵심 컷', () => {
  const { approveLabel } = proposalButtons(incident(['RUNBOOK_NACL_RESTORE'], 'AGENT_WAIT'));
  assert.equal(approveLabel, '승인하고 해제');
  assert.notEqual(approveLabel, '승인하고 차단');
});

test('삭제 후보 2종의 문구는 삭제로 읽힌다', () => {
  assert.equal(
    proposalButtons(incident(['RUNBOOK_SG_DELETE_ISOLATED'])).approveLabel,
    '승인하고 삭제',
  );
  assert.equal(
    proposalButtons(incident(['RUNBOOK_EBS_DELETE_UNATTACHED'])).approveLabel,
    '승인하고 삭제',
  );
});

test('차단 후보는 종전 문구를 그대로 쓴다', () => {
  assert.equal(proposalButtons(incident(['RUNBOOK_NACL_ADD_DENY'])).approveLabel, '승인하고 차단');
  assert.equal(proposalButtons(incident(['RUNBOOK_EC2_ISOLATE'])).approveLabel, '승인하고 차단');
});

test('조정 계열(FinOps 2종)은 중립 문구다', () => {
  assert.equal(proposalButtons(incident(['RUNBOOK_EC2_RIGHTSIZING'])).approveLabel, '이 조치 실행');
  assert.equal(
    proposalButtons(incident(['RUNBOOK_EC2_ENABLE_AUTOSCALING'])).approveLabel,
    '이 조치 실행',
  );
});

test('계열이 섞이면 중립 문구 — 버튼 하나가 후보 전부를 실행하기 때문이다', () => {
  const mixed = incident(['RUNBOOK_NACL_ADD_DENY', 'RUNBOOK_NACL_RESTORE']);
  assert.equal(proposalActionKind(mixed.recommendations), null);
  assert.equal(proposalButtons(mixed).approveLabel, MIXED_APPROVE_LABEL);
  assert.equal(proposalButtons(mixed).canReject, false);
});

test('후보 0건은 계열을 지어내지 않는다 — 호출부가 버튼을 만들지 않는 자리다', () => {
  assert.equal(proposalActionKind([]), null);
  assert.equal(proposalButtons(incident([], 'AGENT_WAIT')).canReject, false);
});

test('`차단 안 함`은 차단 후보 + AGENT_WAIT에서만 붙는다', () => {
  assert.equal(proposalButtons(incident(['RUNBOOK_NACL_ADD_DENY'], 'AGENT_WAIT')).canReject, true);
  // 해제·삭제 후보에 붙으면 누르지 않은 차단을 되돌리겠다는 말이 된다.
  assert.equal(proposalButtons(incident(['RUNBOOK_NACL_RESTORE'], 'AGENT_WAIT')).canReject, false);
  assert.equal(
    proposalButtons(incident(['RUNBOOK_SG_DELETE_ISOLATED'], 'AGENT_WAIT')).canReject,
    false,
  );
  // 실행 전 상태가 아니면 반려할 것이 없다.
  assert.equal(
    proposalButtons(incident(['RUNBOOK_NACL_ADD_DENY'], 'PRE_MITIGATION_0_5S')).canReject,
    false,
  );
  assert.equal(proposalButtons(incident(['RUNBOOK_EC2_RIGHTSIZING'], null)).canReject, false);
});

test('DELETE 계열과 파괴적 런북 목록이 같은 집합이다 — ACT-001 경고와 버튼이 갈리지 않게', () => {
  const deleteKind = Object.entries(RUNBOOK_ACTION_KINDS)
    .filter(([, kind]) => kind === 'DELETE')
    .map(([id]) => id)
    .sort();
  assert.deepEqual(deleteKind, [...DESTRUCTIVE_RUNBOOK_IDS].sort());
});

test('AI 추천 가능 7종 전부에 계열이 있고, 롤백 3종은 없다', () => {
  const recommendable = RUNBOOK_IDS.filter(
    (id) => !(ROLLBACK_RUNBOOK_IDS as readonly string[]).includes(id),
  ).sort();
  assert.deepEqual(Object.keys(RUNBOOK_ACTION_KINDS).sort(), recommendable);
});
