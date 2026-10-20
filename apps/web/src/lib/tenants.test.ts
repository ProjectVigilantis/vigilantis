import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { TenantListItem } from '../types/api.ts';
import {
  hoursSince,
  isStaleTenantRequest,
  parseTenantForm,
  parseVpcIds,
  splitList,
  summarizeTenants,
  TENANT_STATUS_LABELS,
  tenantActions,
  verificationAges,
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
  accessRoleArn: '',
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

test('정상 입력은 요청으로 바뀐다. 이름은 다듬고, 역할 ARN을 비우면 보내지 않는다', () => {
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

test('역할 ARN의 계정이 계정 ID와 다르면 거절한다(서버 계약과 같은 조건)', () => {
  const result = parseTenantForm({ ...FORM, accessRoleArn: 'arn:aws:iam::999999999999:role/vigilantis-access' });
  assert.equal(result.ok, false);
  assert.match(!result.ok ? result.errors.accessRoleArn ?? '' : '', /계정/);
});

test('역할 이름에 ASCII 밖 글자가 있으면 거절한다', () => {
  const result = parseTenantForm({ ...FORM, accessRoleArn: 'arn:aws:iam::123456789012:role/역할' });
  assert.equal(result.ok, false);
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

test('정지 요청이 409(상태 충돌)이면 확인창을 닫고, 다시 읽은 정지 상태에서는 재개만 열린다(#503 리뷰)', () => {
  // 다른 관리자가 먼저 정지했다: 서버는 409 TENANT_STATE_CONFLICT, 화면은 다시 읽어 SUSPENDED를 받는다.
  assert.equal(isStaleTenantRequest('TENANT_STATE_CONFLICT'), true);
  const after = tenantActions('SUSPENDED');
  assert.equal(after.suspend, false);
  assert.equal(after.resume, true);
});

test('이미 정지 · 없는 고객도 요청이 무효라 확인창을 닫는다', () => {
  assert.equal(isStaleTenantRequest('TENANT_SUSPENDED'), true);
  assert.equal(isStaleTenantRequest('TENANT_NOT_FOUND'), true);
});

test('일시 오류는 확인창을 남긴다(입력한 정지 사유를 잃지 않게)', () => {
  assert.equal(isStaleTenantRequest('INTERNAL_ERROR'), false);
  assert.equal(isStaleTenantRequest(null), false);
});
