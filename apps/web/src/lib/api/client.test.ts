// 네트워크 실패 회귀 — `npm test`. BE 미기동 시 조회 화면 전부가 멈추던 원인(React Flight dev 직렬화)을 고정합니다.

import assert from 'node:assert/strict';
import { createServer } from 'node:net';
import { test } from 'node:test';

import {
  ApiError,
  cancelMyRenewal,
  changeUserRole,
  changeUserStatus,
  confirmPasswordReset,
  createReadApi,
  getAssets,
  getInquiries,
  getMe,
  getMySubscription,
  getSignupRequests,
  getTenantSettings,
  getUserEvents,
  inviteUser,
  purchaseMySubscription,
  requestPasswordReset,
  resendInvite,
  revokeUserSessions,
  stopMyService,
  submitSignupRequest,
  updateTenantSettings,
} from './client.ts';
import type {
  InquiriesResponse,
  SignupRequestAccepted,
  SignupRequestCreate,
  SignupRequestItem,
  SignupRequestsResponse,
  TenantSettings,
  UserListItem,
} from '../../types/api.ts';

interface Sent {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
}

/** fetch를 바꿔 끼워 보낸 요청을 기록하고 정해 둔 응답을 돌려준다. 테스트가 끝나면 되돌린다. */
function stubFetch(t: { after: (fn: () => void) => void }, respond: () => Response): Sent[] {
  const realFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = realFetch;
  });
  const sent: Sent[] = [];
  globalThis.fetch = async (input, init) => {
    sent.push({
      url: String(input),
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : null,
      headers: { ...(init?.headers as Record<string, string> | undefined) },
    });
    return respond();
  };
  return sent;
}

const noContent = () => new Response(null, { status: 204 });
const errorResponse = (status: number, code: string) => () =>
  new Response(JSON.stringify({ error: { code, message: 'm', request_id: 'r' } }), { status });

test('비밀번호 재설정 요청 · 확인은 입력값을 다듬지 않고 그대로 보낸다', async (t) => {
  const sent = stubFetch(t, noContent);
  await requestPasswordReset(' User@Example.com ');
  await confirmPasswordReset('User@Example.com', '012345', ' Aa1!aaaaaaaa ');
  assert.match(sent[0].url, /\/api\/v1\/auth\/password-reset$/);
  assert.equal(sent[0].method, 'POST');
  assert.deepEqual(sent[0].body, { email: ' User@Example.com ' });
  assert.match(sent[1].url, /\/api\/v1\/auth\/password-reset\/confirm$/);
  assert.deepEqual(sent[1].body, { email: 'User@Example.com', code: '012345', new_password: ' Aa1!aaaaaaaa ' });
});

test('도입 신청은 재시도 키를 Idempotency-Key 헤더로 싣는다(없으면 서버가 422로 거절)', async (t) => {
  const accepted: SignupRequestAccepted = { request_id: 'request-1' };
  const sent = stubFetch(t, () => new Response(JSON.stringify(accepted), { status: 202 }));
  const body: SignupRequestCreate = {
    company_name: 'Example Corp',
    contact_name: 'Kim',
    email: 'admin@example.invalid',
    phone: null,
    note: null,
  };
  const key = '7f9c2ba4-e88f-4d6e-9a1b-3c5d7e9f1a2b';
  const result = await submitSignupRequest(body, key);
  assert.match(sent[0].url, /\/api\/v1\/signup-requests$/);
  assert.equal(sent[0].method, 'POST');
  assert.deepEqual(sent[0].body, body);
  assert.equal(sent[0].headers['Idempotency-Key'], key);
  assert.equal(sent[0].headers['Content-Type'], 'application/json');
  assert.deepEqual(result, accepted);
});

test('도입 신청의 401은 로그인 이동 없이 ApiError로 올라온다(로그인 전 화면)', async (t) => {
  stubFetch(t, errorResponse(401, 'AUTH_REQUIRED'));
  await assert.rejects(
    submitSignupRequest(
      { company_name: 'c', contact_name: 'n', email: 'e@example.invalid', phone: null, note: null },
      '7f9c2ba4-e88f-4d6e-9a1b-3c5d7e9f1a2b',
    ),
    (error: unknown) => error instanceof ApiError && error.httpStatus === 401,
  );
});

test('사용자 관리 조치의 경로 · 메서드 · 본문', async (t) => {
  const sent = stubFetch(t, noContent);
  await resendInvite('u/1');
  await changeUserRole('u/1', 'OPERATOR');
  await changeUserStatus('u/1', 'DISABLED');
  await revokeUserSessions('u/1');
  const path = (s: Sent) => new URL(s.url).pathname + new URL(s.url).search;
  assert.deepEqual(
    sent.map((s) => [s.method, path(s), s.body]),
    [
      ['POST', '/api/v1/users/u%2F1/invite/resend', {}],
      ['PUT', '/api/v1/users/u%2F1/role', { role: 'OPERATOR' }],
      ['PUT', '/api/v1/users/u%2F1/status', { status: 'DISABLED' }],
      ['POST', '/api/v1/users/u%2F1/sessions/revoke', {}],
    ],
  );
});

test('초대는 고객 ID · 복구 선택을 그대로 싣고 만든 고객사 사용자를 돌려준다', async (t) => {
  const tenantId = '123e4567-e89b-12d3-a456-426614174000';
  const created: UserListItem = {
    account_type: 'TENANT',
    tenant_id: tenantId,
    platform_role: null,
    user_id: 'u1',
    email: 'a@b.c',
    role: 'VIEWER',
    status: 'ACTIVE',
    last_login_at: null,
    permissions: [],
  };
  const sent = stubFetch(t, () => new Response(JSON.stringify(created), { status: 201 }));
  const result = await inviteUser({ email: 'a@b.c', role: 'VIEWER', tenant_id: tenantId, recover_existing: false });
  assert.deepEqual(result, created);
  assert.equal(sent[0].method, 'POST');
  assert.match(sent[0].url, /\/api\/v1\/users$/);
  assert.deepEqual(sent[0].body, { email: 'a@b.c', role: 'VIEWER', tenant_id: tenantId, recover_existing: false });
});

test('고객사 ADMIN의 초대는 tenant_id 없이 나간다', async (t) => {
  const sent = stubFetch(t, () => new Response('{}', { status: 201 }));
  await inviteUser({ email: 'a@b.c', role: 'OPERATOR', recover_existing: false });
  assert.deepEqual(sent[0].body, { email: 'a@b.c', role: 'OPERATOR', recover_existing: false });
});

test('고객 설정 조회 · 수정 — 경로 · 메서드 · 본문 · 쿠키 동봉 · 캐시 안 함', async (t) => {
  const realFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = realFetch; });
  const settings: TenantSettings = {
    tenant_id: 't/1',
    protected_tags: [],
    notifications: { slack_configured: true, email_address: null, email_status: 'UNSET', legacy_sns_configured: false },
    updated_at: '2026-10-30T00:00:00Z',
  };
  const sent: { path: string; init: RequestInit | undefined }[] = [];
  globalThis.fetch = async (url, init) => {
    sent.push({ path: new URL(String(url)).pathname, init });
    return new Response(JSON.stringify(settings), { status: 200 });
  };
  const body = {
    protected_tags: [{ key: 'Env', values: ['prod', ''] }],
    notifications: { slack_webhook_url: null },
  };
  assert.deepEqual(await getTenantSettings('t/1'), settings);
  assert.deepEqual(await updateTenantSettings('t/1', body), settings);

  assert.deepEqual(sent.map((s) => [s.init?.method ?? 'GET', s.path]), [
    ['GET', '/api/v1/tenants/t%2F1/settings'],
    ['PATCH', '/api/v1/tenants/t%2F1/settings'],
  ]);
  for (const { init } of sent) {
    assert.equal(init?.credentials, 'include');
    assert.equal(init?.cache, 'no-store');
  }
  assert.equal(sent[0].init?.body, undefined);
  assert.equal(new Headers(sent[1].init?.headers).get('Content-Type'), 'application/json');
  assert.deepEqual(JSON.parse(String(sent[1].init?.body)), body);
});

test('고객 설정 서버 조회는 세션 쿠키를 싣고, 403은 로그인 처리 없이 오류로 올린다', async (t) => {
  const realFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = realFetch; });
  globalThis.fetch = async (url, init) => {
    assert.equal(new URL(String(url)).pathname, '/api/v1/tenants/t1/settings');
    assert.equal(new Headers(init?.headers).get('Cookie'), '__Host-vg_session=test-session');
    return errorResponse(403, 'AUTH_FORBIDDEN')();
  };
  let authRequired = 0;
  const api = createReadApi({ headers: { Cookie: '__Host-vg_session=test-session' }, onAuthRequired: () => (authRequired += 1) });
  await assert.rejects(api.getTenantSettings('t1'), (error: unknown) => error instanceof ApiError && error.code === 'AUTH_FORBIDDEN');
  assert.equal(authRequired, 0);
});

test('이력 조회는 상한 50을 싣는다', async (t) => {
  const sent = stubFetch(t, () => new Response(JSON.stringify({ items: [] }), { status: 200 }));
  assert.deepEqual(await getUserEvents('u1'), { items: [] });
  assert.equal(sent[0].method, 'GET');
  assert.match(sent[0].url, /\/api\/v1\/users\/u1\/events\?limit=50$/);
});

test('신청 목록은 페이지 질의를 보내고 서버의 판정 스냅샷과 페이지 정보를 소비한다', async (t) => {
  const pending: SignupRequestItem = {
    request_id: 'request-new',
    company_name: 'Customer',
    contact_name: 'Requester',
    email: 'requester@example.invalid',
    phone: null,
    note: null,
    status: 'NEW',
    rejected_reason: null,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
    decision: null,
  };
  const response: SignupRequestsResponse = {
    items: [
      pending,
      {
        ...pending,
        request_id: 'request-approved',
        status: 'APPROVED',
        updated_at: '2026-11-02T00:00:00Z',
        decision: {
          actor_user_id: 'admin-1',
          actor_email: 'historical-admin@example.invalid',
          decided_at: '2026-11-02T00:00:00Z',
          outcome: 'APPROVED',
          rejected_reason: null,
        },
      },
      {
        ...pending,
        request_id: 'request-rejected',
        status: 'REJECTED',
        rejected_reason: 'Duplicate request',
        updated_at: '2026-11-02T01:00:00Z',
        decision: {
          actor_user_id: 'admin-2',
          actor_email: 'former-admin@example.invalid',
          decided_at: '2026-11-02T01:00:00Z',
          outcome: 'REJECTED',
          rejected_reason: 'Duplicate request',
        },
      },
    ],
    page: 2,
    page_size: 50,
    total: 53,
    total_pages: 2,
  };
  const sent = stubFetch(t, () => new Response(JSON.stringify(response), { status: 200 }));
  const result: SignupRequestsResponse = await getSignupRequests({ page: 2, page_size: 50 });
  assert.match(sent[0].url, /\/api\/v1\/signup-requests\?page=2&page_size=50$/);
  assert.equal(sent[0].method, 'GET');
  assert.deepEqual(result, response);
  assert.equal(result.items[0].decision, null);
  assert.equal(result.items[1].decision?.actor_email, 'historical-admin@example.invalid');
  assert.equal(result.items[2].decision?.rejected_reason, 'Duplicate request');
});

test('문의 목록은 페이지 질의를 보내고 기존 문의 필드와 페이지 정보를 소비한다', async (t) => {
  const response: InquiriesResponse = {
    items: [{
      inquiry_id: 'inquiry-1',
      tenant_id: 'tenant-1',
      tenant_name: 'Customer',
      author_email: 'viewer@example.invalid',
      author_role: 'VIEWER',
      category: 'OUTAGE',
      title: 'Unavailable dashboard',
      body: 'The dashboard does not load.',
      occurred_at: '2026-11-01T00:00:00Z',
      page_path: null,
      status: 'IN_PROGRESS',
      created_at: '2026-11-01T00:01:00Z',
      updated_at: '2026-11-02T00:00:00Z',
    }],
    page: 3,
    page_size: 20,
    total: 41,
    total_pages: 3,
  };
  const sent = stubFetch(t, () => new Response(JSON.stringify(response), { status: 200 }));
  const api = createReadApi({});
  const result: InquiriesResponse = await api.getInquiries({ page: 3, page_size: 20 });
  assert.match(sent[0].url, /\/api\/v1\/inquiries\?page=3&page_size=20$/);
  assert.equal(sent[0].method, 'GET');
  assert.deepEqual(result, response);
  assert.equal(result.items[0].author_role, 'VIEWER');
  assert.equal(result.items[0].page_path, null);
});

for (const [path, getPage] of [
  ['/signup-requests', getSignupRequests],
  ['/inquiries', getInquiries],
] as const) {
  test(`${path} — 제공한 페이지 값만 보내고 빈 결과의 total_pages=0을 유지한다`, async (t) => {
    const cases = [
      [undefined, '', 1, 20],
      [{}, '', 1, 20],
      [{ page: 4 }, '?page=4', 4, 20],
      [{ page_size: 100 }, '?page_size=100', 1, 100],
      [{ page: 1, page_size: undefined }, '?page=1', 1, 20],
    ] as const;
    let response = { items: [], page: 1, page_size: 20, total: 0, total_pages: 0 };
    const sent = stubFetch(t, () => new Response(JSON.stringify(response), { status: 200 }));
    for (const [pagination, , page, pageSize] of cases) {
      response = { items: [], page, page_size: pageSize, total: 0, total_pages: 0 };
      const result = await getPage(pagination);
      assert.deepEqual(result, response);
    }
    assert.deepEqual(
      sent.map((request) => [request.method, new URL(request.url).pathname + new URL(request.url).search, request.body]),
      cases.map(([, query]) => ['GET', `/api/v1${path}${query}`, null]),
    );
  });
}

test('사용자 목록 — 401만 로그인 처리를 부르고, 403은 오류로만 올린다', async (t) => {
  let respond = errorResponse(401, 'AUTH_REQUIRED');
  stubFetch(t, () => respond());
  let authRequired = 0;
  const api = createReadApi({ onAuthRequired: () => (authRequired += 1) });

  await assert.rejects(api.getUsers(), (error: unknown) => error instanceof ApiError && error.code === 'AUTH_REQUIRED');
  assert.equal(authRequired, 1);

  respond = errorResponse(403, 'AUTH_FORBIDDEN');
  await assert.rejects(api.getUsers(), (error: unknown) => error instanceof ApiError && error.code === 'AUTH_FORBIDDEN');
  assert.equal(authRequired, 1);
});

test('서버 계정 조회는 세션 헤더를 전달하고 캐시하지 않는다', async (t) => {
  const realFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = realFetch; });
  const user = {
    account_type: 'TENANT', tenant_id: 'tenant-1', platform_role: null, role: 'VIEWER', assigned_tenant_ids: [],
    user_id: 'user-1', email: 'viewer@example.invalid',
    session: { absolute_expires_at: '2026-10-27T00:00:00Z', idle_expires_at: '2026-10-26T17:00:00Z' },
  };
  let calls = 0;
  globalThis.fetch = async (url, init) => {
    calls += 1;
    assert.equal(new URL(String(url)).pathname, '/api/v1/auth/me');
    assert.equal(new Headers(init?.headers).get('Cookie'), '__Host-vg_session=test-session');
    assert.equal(init?.cache, 'no-store');
    return new Response(JSON.stringify(user), { status: 200 });
  };
  const api = createReadApi({ headers: { Cookie: '__Host-vg_session=test-session' } });
  assert.deepEqual(await api.getMe(), user);
  assert.equal(calls, 1);
});

test('계정 조회 실패 — SSR 401은 redirect를 전파하고 503은 계정 없음으로 바꾸지 않는다', async (t) => {
  let respond = errorResponse(401, 'AUTH_REQUIRED');
  stubFetch(t, () => respond());
  const redirect = new Error('server redirect');
  let redirects = 0;
  const api = createReadApi({ onAuthRequired: () => { redirects += 1; throw redirect; } });
  await assert.rejects(api.getMe(), (error) => error === redirect);
  assert.equal(redirects, 1);
  respond = errorResponse(503, 'INTERNAL_ERROR');
  await assert.rejects(api.getMe(), (error: unknown) => error instanceof ApiError && error.httpStatus === 503);
  assert.equal(redirects, 1);
});

test('브라우저 계정 조회는 기존 401 null 계약을 유지한다', async (t) => {
  stubFetch(t, errorResponse(401, 'AUTH_REQUIRED'));
  assert.equal(await getMe(), null);
});

const SUBSCRIPTION = {
  tenant_name: '학원',
  service_status: 'ACTIVE',
  subscription: null,
  source: null,
  entitlement_ends_at: null,
  agreement: null,
  next_renewal_date: null,
  expiry_date: null,
  mock_marketplace_enabled: true,
  can_manage: true,
  available_actions: [],
};

test('자기 구독: 조회 · 구매 요청 · 갱신 끄기 · 중단의 경로 · 메서드 · 본문이고, tenant_id는 싣지 않는다', async (t) => {
  const sent = stubFetch(t, () => new Response(JSON.stringify(SUBSCRIPTION), { status: 200 }));
  assert.deepEqual(await getMySubscription(), SUBSCRIPTION);
  assert.deepEqual(await purchaseMySubscription('Business'), SUBSCRIPTION);
  await cancelMyRenewal();
  await stopMyService('계약 종료');
  const path = (s: Sent) => new URL(s.url).pathname + new URL(s.url).search;
  assert.deepEqual(
    sent.map((s) => [s.method, path(s), s.body]),
    [
      ['GET', '/api/v1/me/subscription', null],
      ['POST', '/api/v1/me/subscription/purchase', { plan: 'Business' }],
      ['POST', '/api/v1/me/subscription/cancel', null],
      ['POST', '/api/v1/me/subscription/stop', { reason: '계약 종료' }],
    ],
  );
  assert.equal(JSON.stringify(sent).includes('tenant_id'), false);
});

test('자기 구독 조회 실패는 Free · 미등록 응답으로 바뀌지 않고 오류로 올라간다', async (t) => {
  let respond = errorResponse(500, 'INTERNAL_ERROR');
  stubFetch(t, () => respond());
  let authRequired = 0;
  const api = createReadApi({ onAuthRequired: () => (authRequired += 1) });

  await assert.rejects(api.getMySubscription(), (error: unknown) => error instanceof ApiError && error.httpStatus === 500);
  assert.equal(authRequired, 0);

  respond = errorResponse(401, 'AUTH_REQUIRED');
  await assert.rejects(api.getMySubscription(), (error: unknown) => error instanceof ApiError && error.code === 'AUTH_REQUIRED');
  assert.equal(authRequired, 1);
});

test('구독 쓰기의 403은 오류로 올라간다(화면이 버튼을 숨겼어도 판정은 서버다)', async (t) => {
  stubFetch(t, errorResponse(403, 'AUTH_FORBIDDEN'));
  await assert.rejects(purchaseMySubscription('Enterprise'), (error: unknown) => error instanceof ApiError && error.code === 'AUTH_FORBIDDEN');
});

/** 방금 비운 포트 — 연결하면 실제 Node 연결 거부 오류(`cause.code = ECONNREFUSED`)가 난다. */
async function closedPort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address !== null && typeof address === 'object');
  await new Promise<void>((resolve) => server.close(() => resolve()));
  return address.port;
}

test('연결 거부는 원인 코드를 문구에 남기고, 앱 코드에서 새로 만든 오류로 올린다', async (t) => {
  const saved = process.env.NEXT_PUBLIC_API_BASE_URL;
  t.after(() => {
    if (saved === undefined) delete process.env.NEXT_PUBLIC_API_BASE_URL;
    else process.env.NEXT_PUBLIC_API_BASE_URL = saved;
  });
  process.env.NEXT_PUBLIC_API_BASE_URL = `http://127.0.0.1:${await closedPort()}`;

  await assert.rejects(getAssets(), (error: unknown) => {
    assert.ok(error instanceof Error);
    assert.equal(error.message, '요청이 실패했습니다 (네트워크 오류: ECONNREFUSED)');
    // cause가 붙으면 스택이 `node:` 내부 프레임뿐인 원래 오류가 서버 컴포넌트 prop 직렬화로 그대로 넘어간다.
    assert.equal('cause' in error, false);
    // 앱 코드 프레임이 남아야 dev 직렬화가 깨지지 않는다 — fetch 오류를 되던지지 않고 새로 만들었다는 증거.
    assert.match(String(error.stack), /client\.ts/);
    return true;
  });
});

test('원인 코드가 없는 실패(브라우저의 Failed to fetch)는 코드 없이 접는다', async (t) => {
  const realFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = realFetch;
  });
  globalThis.fetch = () => Promise.reject(new TypeError('Failed to fetch'));

  await assert.rejects(getAssets(), {
    message: '요청이 실패했습니다 (네트워크 오류)',
  });
});
