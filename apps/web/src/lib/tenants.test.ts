import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  TENANT_RESUMABLE_STATUSES,
  TENANT_STATUSES,
  TENANT_SUSPENDABLE_STATUSES,
  type TenantListItem,
} from '../types/api.ts';
import {
  DEFAULT_ACCESS_ROLE_NAME,
  TENANT_FORM_ADVANCED,
  hoursSince,
  isStaleTenantRequest,
  matchesTenantQuery,
  ONBOARDING_STACK_NAME,
  onboardingMessage,
  parseTenantForm,
  roleNameOf,
  parseVpcIds,
  splitList,
  summarizeTenants,
  TENANT_STATUS_LABELS,
  tenantActions,
  tenantRunFollowUp,
  verificationAges,
  verificationHint,
  type TenantForm,
} from './tenants.ts';

const NOW = Date.parse('2026-10-20T01:30:00Z');

function tenant(over: Partial<TenantListItem> & Pick<TenantListItem, 'tenant_id' | 'status'>): TenantListItem {
  return {
    name: over.tenant_id,
    aws_account_id: '123456789012',
    last_verification: null,
    asset_count: 0,
    open_incident_count: 0,
    ...over,
  };
}

const LIST: TenantListItem[] = [
  tenant({
    tenant_id: 'a', status: 'ACTIVE', asset_count: 48, open_incident_count: 3,
    last_verification: { result: 'OK', verified_at: '2026-10-20T00:12:00Z', reason_code: null },
  }),
  tenant({
    tenant_id: 'b', status: 'ACTIVE', asset_count: 31, open_incident_count: 1,
    last_verification: { result: 'READ_DENIED', verified_at: '2026-10-19T23:40:00Z', reason_code: 'UnauthorizedOperation' },
  }),
  tenant({ tenant_id: 'c', status: 'PENDING_CONNECTION' }),
  tenant({
    tenant_id: 'd', status: 'SUSPENDED', asset_count: 22,
    last_verification: { result: 'ASSUME_DENIED', verified_at: '2026-10-18T08:05:00Z', reason_code: 'AccessDenied' },
  }),
  tenant({
    tenant_id: 'e', status: 'ACTIVE', asset_count: 64, open_incident_count: 5,
    last_verification: { result: 'UNREACHABLE', verified_at: '2026-10-20T00:30:00Z', reason_code: 'EndpointConnectionError' },
  }),
];

test('정지는 실패가 아니라 의도한 상태라 중립 회색이다', () => {
  assert.equal(TENANT_STATUS_LABELS.SUSPENDED.tone, 'gray');
});

test('목록 요약: 자산은 관제 중 고객만 더하고, 확인 문제는 실패와 확인 불가를 가른다', () => {
  assert.deepEqual(summarizeTenants(LIST), {
    total: 5,
    byStatus: { PENDING_CONNECTION: 1, ACTIVE: 3, SUSPENDED: 1 },
    activeAssets: 143,
    openIncidents: 9,
    verificationProblems: { failed: 2, unreachable: 1 },
  });
});

test('빈 목록 요약은 전부 0이다', () => {
  const s = summarizeTenants([]);
  assert.equal(s.total, 0);
  assert.equal(s.activeAssets, 0);
  assert.deepEqual(s.verificationProblems, { failed: 0, unreachable: 0 });
});

test('지난 시간은 시간 단위이고, 미래 시각(시계 어긋남)은 0으로 자른다', () => {
  assert.equal(hoursSince('2026-10-19T23:30:00Z', NOW), 2);
  assert.equal(hoursSince('2026-10-20T02:00:00Z', NOW), 0);
});

test('연결 확인 경과는 기록 있는 고객만 최근 순으로', () => {
  const ages = verificationAges(LIST, NOW);
  assert.deepEqual(ages.map((a) => a.tenantId), ['e', 'a', 'b', 'd']);
  assert.equal(ages[0].hours, 1);
});

const FORM: TenantForm = {
  name: ' 학원 계정 ',
  awsAccountId: '123456789012',
  roleName: '',
  regions: 'ap-northeast-2',
  collectVpcIds: '',
};

test('상태별 동작: 정지 중에는 확인 대신 재개만 열린다', () => {
  assert.deepEqual(tenantActions('PENDING_CONNECTION'), { verify: true, suspend: false, resume: false, remove: true });
  assert.deepEqual(tenantActions('ACTIVE'), { verify: true, suspend: true, resume: false, remove: false });
  assert.deepEqual(tenantActions('SUSPENDED'), { verify: false, suspend: false, resume: true, remove: false });
});

test('목록은 쉼표 · 공백 · 줄바꿈으로 나누고 빈 칸을 버린다', () => {
  assert.deepEqual(splitList('ap-northeast-2, us-east-1\n\n us-west-2 ,'), ['ap-northeast-2', 'us-east-1', 'us-west-2']);
});

test('정상 입력은 요청으로 바뀐다. 이름은 다듬고, 역할 이름을 비우면 ARN을 보내지 않는다', () => {
  const result = parseTenantForm(FORM);
  assert.ok(result.ok);
  assert.deepEqual(result.request, {
    name: '학원 계정',
    aws_account_id: '123456789012',
    regions: ['ap-northeast-2'],
    collect_vpc_ids: [],
  });
  assert.equal('access_role_arn' in result.request, false);
});

test('콘솔에서 하이픈째 붙여 넣은 계정 ID도 받는다', () => {
  const result = parseTenantForm({ ...FORM, awsAccountId: ' 1234-5678-9012 ' });
  assert.ok(result.ok);
  assert.equal(result.request.aws_account_id, '123456789012');
});

test('역할 이름은 계정 ID로 ARN을 만들어 보낸다. 기본 이름이면 보내지 않는다', () => {
  const custom = parseTenantForm({ ...FORM, roleName: 'ops/vigilantis-read' });
  assert.ok(custom.ok);
  assert.equal(custom.request.access_role_arn, 'arn:aws:iam::123456789012:role/ops/vigilantis-read');
  const fallback = parseTenantForm({ ...FORM, roleName: DEFAULT_ACCESS_ROLE_NAME });
  assert.ok(fallback.ok);
  assert.equal('access_role_arn' in fallback.request, false);
});

test('역할 칸에 ARN 전체를 넣으면 이름만 넣으라고 알린다', () => {
  const result = parseTenantForm({ ...FORM, roleName: 'arn:aws:iam::123456789012:role/vigilantis-access' });
  assert.equal(result.ok, false);
  assert.match(!result.ok ? result.errors.roleName ?? '' : '', /이름만/);
});

test('역할 이름에 ASCII 밖 글자나 빈 경로 마디가 있으면 거절한다', () => {
  for (const roleName of ['역할', 'ops//x', '/x', 'x/']) {
    const result = parseTenantForm({ ...FORM, roleName });
    assert.equal(result.ok, false, roleName);
  }
});

test('접어 둔 칸(리전 · VPC · 역할 이름)의 오류는 펼쳐서 보일 대상이다', () => {
  const result = parseTenantForm({ ...FORM, regions: 'seoul', roleName: '역할' });
  assert.equal(result.ok, false);
  if (!result.ok) {
    for (const key of Object.keys(result.errors)) {
      assert.ok(TENANT_FORM_ADVANCED.includes(key as keyof TenantForm), key);
    }
  }
});

test('계정 ID · 리전 · 이름 오류는 칸마다 따로 알린다', () => {
  const result = parseTenantForm({ ...FORM, name: '  ', awsAccountId: '12345', regions: 'seoul' });
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.deepEqual(Object.keys(result.errors).sort(), ['awsAccountId', 'name', 'regions']);
  }
});

test('리전 · VPC 중복은 서버가 422로 거절하므로 미리 막는다', () => {
  const result = parseTenantForm({ ...FORM, regions: 'ap-northeast-2 ap-northeast-2' });
  assert.equal(result.ok, false);
  assert.match(parseVpcIds('vpc-0abc vpc-0abc').error ?? '', /두 번/);
});

test('VPC 칸을 비우면 오류가 아니라 계정 안 VPC 전체다', () => {
  assert.deepEqual(parseVpcIds(''), { values: [], error: null });
  assert.match(parseVpcIds('vpc-XYZ').error ?? '', /형식/);
});

test('정지 · 재개 버튼은 계약 상수를 따른다(#507)', () => {
  for (const status of TENANT_STATUSES) {
    const actions = tenantActions(status);
    assert.equal(actions.suspend, TENANT_SUSPENDABLE_STATUSES.includes(status), status);
    assert.equal(actions.resume, TENANT_RESUMABLE_STATUSES.includes(status), status);
  }
});

// 아래 넷은 상세 화면 정지 확인창이 요청 결과마다 하는 일(tenantRunFollowUp)을 고정한다(#508).
// 확인창은 결과가 closeDialog면 닫히고, 아니면 열린 채 입력한 사유와 창 안의 오류를 유지한다.

test('정지 요청이 409(상태 충돌)이면 확인창을 닫고 다시 읽어, 정지 상태에서는 재개만 열린다(#503 리뷰)', () => {
  // 다른 관리자가 먼저 정지했다: 서버는 409 TENANT_STATE_CONFLICT, 화면은 다시 읽어 SUSPENDED를 받는다.
  assert.deepEqual(tenantRunFollowUp({ ok: false, code: 'TENANT_STATE_CONFLICT' }), { refresh: true, closeDialog: true });
  const after = tenantActions('SUSPENDED');
  assert.equal(after.suspend, false);
  assert.equal(after.resume, true);
});

test('이미 정지 · 없는 고객도 요청이 무효라 확인창을 닫고 다시 읽는다', () => {
  for (const code of ['TENANT_SUSPENDED', 'TENANT_NOT_FOUND'] as const) {
    assert.equal(isStaleTenantRequest(code), true);
    assert.deepEqual(tenantRunFollowUp({ ok: false, code }), { refresh: true, closeDialog: true });
  }
});

test('503 같은 일시 오류는 확인창을 남기고 다시 읽지 않는다(입력한 정지 사유를 잃지 않고 재시도한다)', () => {
  // 응답 본문이 없는 503(code 없음)과 서버 내부 오류 코드 둘 다 같다.
  for (const code of [null, 'INTERNAL_ERROR'] as const) {
    assert.equal(isStaleTenantRequest(code), false);
    assert.deepEqual(tenantRunFollowUp({ ok: false, code }), { refresh: false, closeDialog: false });
  }
});

test('성공하면 닫고 다시 읽는다. 다른 동작이 진행 중이라 건너뛴 요청은 아무것도 바꾸지 않는다', () => {
  assert.deepEqual(tenantRunFollowUp({ ok: true }), { refresh: true, closeDialog: true });
  assert.deepEqual(tenantRunFollowUp(null), { refresh: false, closeDialog: false });
});

test('역할 접근 거절: 연결 대기에서만 계정 ID 확인과 삭제 · 재등록을 덧붙인다', () => {
  const pending = verificationHint('ASSUME_DENIED', 'PENDING_CONNECTION') ?? '';
  assert.match(pending, /신뢰 정책/);
  assert.match(pending, /계정 ID/);
  assert.match(pending, /지우고 새로 추가/);
  for (const status of ['ACTIVE', 'SUSPENDED'] as const) {
    const hint = verificationHint('ASSUME_DENIED', status) ?? '';
    assert.match(hint, /신뢰 정책/);
    assert.doesNotMatch(hint, /지우|삭제/);
  }
});

test('계정 불일치는 저장값 · 구현 불일치 점검을 안내하고 삭제는 안내하지 않는다(#507)', () => {
  for (const status of TENANT_STATUSES) {
    const hint = verificationHint('ACCOUNT_MISMATCH', status) ?? '';
    assert.match(hint, /점검/);
    assert.doesNotMatch(hint, /지우|삭제|잘못 넣/);
  }
});

test('역할 이름은 서버의 param_RoleName과 같게 ":role/" 뒤 전부다(경로 포함)', () => {
  assert.equal(roleNameOf('arn:aws:iam::123456789012:role/vigilantis-access'), 'vigilantis-access');
  assert.equal(roleNameOf('arn:aws:iam::123456789012:role/ops/vigilantis-access'), 'ops/vigilantis-access');
});

test('고객 안내문은 계정 · 첫 리전 스택 · 역할 이름 · 링크를 싣고 ExternalId를 따로 싣지 않는다', () => {
  const url = 'https://ap-northeast-2.console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?param_ExternalId=secret-value-123456';
  const message = onboardingMessage(
    {
      aws_account_id: '123456789012',
      access_role_arn: 'arn:aws:iam::123456789012:role/vigilantis-access',
      regions: ['ap-northeast-2', 'us-east-1'],
    },
    url,
  );
  assert.match(message, /123456789012/);
  assert.match(message, new RegExp(`ap-northeast-2 리전의 스택 ${ONBOARDING_STACK_NAME}`));
  assert.doesNotMatch(message, /us-east-1/);
  assert.match(message, /만들어지는 역할: vigilantis-access/);
  assert.equal(message.split('\n').at(-1), url);
  // 값은 링크 안에만 있다. 안내문 본문에 ExternalId를 따로 적어 퍼뜨리지 않는다.
  assert.equal(message.split('secret-value-123456').length - 1, 1);
});

test('고객사 검색: 낱말마다 이름 · 고객사 ID · AWS 계정 ID 중 한 곳에 들어 있으면 맞는다', () => {
  const t = { name: 'Nuri Soft 43', tenant_id: 'tnt-0043', aws_account_id: '111122223333' };
  assert.equal(matchesTenantQuery(t, ''), true);
  assert.equal(matchesTenantQuery(t, '  '), true);
  assert.equal(matchesTenantQuery(t, 'nuri'), true);
  assert.equal(matchesTenantQuery(t, 'TNT-00'), true);
  assert.equal(matchesTenantQuery(t, '2222'), true);
  assert.equal(matchesTenantQuery(t, 'soft 0043'), true);
  assert.equal(matchesTenantQuery(t, 'soft 9999'), false);
});
